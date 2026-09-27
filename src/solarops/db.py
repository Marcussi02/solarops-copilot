"""PostgreSQL access: migrations and idempotent writes."""

from collections.abc import Iterable
from datetime import datetime
from importlib import resources

import psycopg

from .dispatch import RegionPrice, UnitDispatch
from .nemweb import Reading
from .observability import tracer
from .registry import SolarUnit
from .weather import Site, WeatherObs


def connect(database_url: str) -> psycopg.Connection:
    # UTC sessions: timestamps render identically everywhere and hourly buckets are UTC.
    try:
        return psycopg.connect(
            database_url, autocommit=False, connect_timeout=10, options="-c TimeZone=UTC"
        )
    except psycopg.ProgrammingError:
        # Parse errors quote the connection string, which contains the password.
        raise ValueError("DATABASE_URL is not a valid PostgreSQL connection URL") from None


def connect_configured() -> psycopg.Connection:
    """Connect using DATABASE_URL (env or SSM). On failure, forget the cached secret so
    a warm Lambda container re-reads it next time: a rotated or corrected password then
    takes effect without a redeploy."""
    from . import config

    try:
        return connect(config.database_url())
    except Exception:
        config.database_url.cache_clear()
        raise


def connect_read_only() -> psycopg.Connection:
    """A session for serving reads: READ ONLY transactions and a 5-second statement
    timeout, so no caller (API, copilot, MCP client) can write or run away."""
    conn = connect_configured()
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.execute("SET statement_timeout = '5s'")
    conn.commit()
    return conn


# ---------- migrations ----------

def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any SQL files in solarops/sql not yet recorded. Returns applied names."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        applied = []
        files = sorted(
            (f for f in resources.files("solarops.sql").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for sql_file in files:
            if sql_file.name in done:
                continue
            conn.execute(sql_file.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (sql_file.name,))
            applied.append(sql_file.name)
    return applied


# ---------- registry ----------

@tracer.capture_method(capture_response=False)
def upsert_units(conn: psycopg.Connection, units: Iterable[SolarUnit]) -> int:
    rows = [
        (u.duid, u.facility_code, u.facility_name, u.region, u.latitude, u.longitude, u.capacity_mw)
        for u in units
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO solar_units
                (duid, facility_code, facility_name, region, latitude, longitude, capacity_mw)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (duid) DO UPDATE SET
                facility_code = EXCLUDED.facility_code,
                facility_name = EXCLUDED.facility_name,
                region        = EXCLUDED.region,
                latitude      = EXCLUDED.latitude,
                longitude     = EXCLUDED.longitude,
                capacity_mw   = EXCLUDED.capacity_mw,
                updated_at    = now()
            """,
            rows,
        )
    return len(rows)


def known_duids(conn: psycopg.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT duid FROM solar_units")}


def facility_sites(conn: psycopg.Connection) -> list[Site]:
    rows = conn.execute(
        """
        SELECT facility_code, avg(latitude), avg(longitude)
        FROM solar_units GROUP BY facility_code ORDER BY facility_code
        """
    ).fetchall()
    return [Site(code, lat, lon) for code, lat, lon in rows]


# ---------- SCADA ingest ----------

def processed_files(conn: psycopg.Connection, names: list[str]) -> set[str]:
    if not names:
        return set()
    rows = conn.execute(
        "SELECT file_name FROM ingested_files WHERE file_name = ANY(%s)", (names,)
    )
    return {row[0] for row in rows}


@tracer.capture_method(capture_response=False)
def ingest_file(
    conn: psycopg.Connection,
    file_name: str,
    interval_end: datetime,
    readings: list[Reading],
    solar_duids: set[str],
) -> int:
    """Store solar readings and record the file, atomically. Returns solar rows written.

    Safe to call repeatedly for the same file (at-least-once delivery): readings
    upsert on (duid, interval_end) and the ledger row is only inserted once.
    """
    solar = [(r.duid, r.interval_end, r.mw) for r in readings if r.duid in solar_duids]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO scada_readings (duid, interval_end, mw) VALUES (%s, %s, %s)
            ON CONFLICT (duid, interval_end) DO UPDATE SET mw = EXCLUDED.mw
            """,
            solar,
        )
        record_file(cur, file_name, interval_end, len(readings), len(solar))
    return len(solar)


def record_file(
    cur: psycopg.Cursor, file_name: str, interval_end: datetime, total: int, kept: int
) -> None:
    cur.execute(
        """
        INSERT INTO ingested_files (file_name, interval_end, rows_total, rows_solar)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (file_name) DO NOTHING
        """,
        (file_name, interval_end, total, kept),
    )


# ---------- dispatch outcomes (curtailment) ----------

@tracer.capture_method(capture_response=False)
def ingest_prices(
    conn: psycopg.Connection, file_name: str, interval_end: datetime, prices: list[RegionPrice]
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO region_prices (region, interval_end, rrp) VALUES (%s, %s, %s)
            ON CONFLICT (region, interval_end) DO UPDATE SET rrp = EXCLUDED.rrp
            """,
            [(p.region, p.interval_end, p.rrp) for p in prices],
        )
        record_file(cur, file_name, interval_end, len(prices), len(prices))
    return len(prices)


@tracer.capture_method(capture_response=False)
def ingest_unit_dispatch(
    conn: psycopg.Connection, file_name: str, day: datetime, rows: list[UnitDispatch]
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO unit_dispatch
                (duid, interval_end, total_cleared, availability, uigf, semidispatch_cap)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (duid, interval_end) DO UPDATE SET
                total_cleared    = EXCLUDED.total_cleared,
                availability     = EXCLUDED.availability,
                uigf             = EXCLUDED.uigf,
                semidispatch_cap = EXCLUDED.semidispatch_cap
            """,
            [
                (r.duid, r.interval_end, r.total_cleared, r.availability, r.uigf,
                 r.semidispatch_cap)
                for r in rows
            ],
        )
        record_file(cur, file_name, day, len(rows), len(rows))
    return len(rows)


# ---------- weather ----------

@tracer.capture_method(capture_response=False)
def upsert_weather(conn: psycopg.Connection, observations: Iterable[WeatherObs]) -> int:
    rows = [
        (o.facility_code, o.observed_at, o.ghi_wm2, o.temp_c, o.cloud_cover_pct)
        for o in observations
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO weather_obs (facility_code, observed_at, ghi_wm2, temp_c, cloud_cover_pct)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (facility_code, observed_at) DO UPDATE SET
                ghi_wm2 = EXCLUDED.ghi_wm2,
                temp_c = EXCLUDED.temp_c,
                cloud_cover_pct = EXCLUDED.cloud_cover_pct
            """,
            rows,
        )
    return len(rows)


# ---------- reads ----------

@tracer.capture_method(capture_response=False)
def status(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM solar_units),
               (SELECT count(DISTINCT facility_code) FROM solar_units),
               (SELECT count(*) FROM ingested_files),
               (SELECT count(*) FROM scada_readings),
               (SELECT max(interval_end) FROM scada_readings),
               (SELECT max(observed_at) FROM weather_obs)
        """
    ).fetchone()
    conn.commit()
    keys = ("units", "facilities", "files", "readings", "latest_interval", "latest_weather")
    return dict(zip(keys, row, strict=True))


def underperformers(
    conn: psycopg.Connection, threshold: float = 0.6, limit: int = 10
) -> list[dict]:
    """Facilities below `threshold` of expected output at the latest interval."""
    from . import queries

    return queries.underperformers(conn, threshold, limit)


# ---------- retention ----------

@tracer.capture_method(capture_response=False)
def prune(conn: psycopg.Connection, days: int) -> dict:
    """Delete telemetry older than `days`. Keeps a free-tier database within its quota.

    The ingest ledger can be pruned too: NEMWeb only lists roughly the last two days
    of files, so anything older can never be offered for ingestion again.
    """
    cutoff = "now() - make_interval(days => %s)"
    deleted = {}
    with conn.transaction():
        for table, column in (
            ("scada_readings", "interval_end"),
            ("weather_obs", "observed_at"),
            ("region_prices", "interval_end"),
            ("unit_dispatch", "interval_end"),
            ("ingested_files", "interval_end"),
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE {column} < {cutoff}", (days,))
            deleted[table] = cur.rowcount
    return deleted
