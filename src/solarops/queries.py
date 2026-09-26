"""Read-only query catalogue shared by the API and the copilot.

Every query here is fixed, parameterised SQL. The copilot decides *which* query to
run and with which (validated, bounded) arguments. It never writes SQL itself.

Time windows are measured back from the latest ingested interval rather than
``now()``, so answers stay meaningful when ingestion is lagging and tests are
deterministic.
"""

from datetime import datetime
from decimal import Decimal

import psycopg

# Performance index below this counts as underperforming.
DEFAULT_THRESHOLD = 0.6


def _rows(conn: psycopg.Connection, sql: str, params: dict | tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, (_plain(v) for v in row), strict=True)) for row in cur.fetchall()]
    conn.commit()
    return rows


def _plain(value):
    """Decimal -> float so results serialise cleanly to JSON."""
    return float(value) if isinstance(value, Decimal) else value


def latest_interval(conn: psycopg.Connection) -> datetime | None:
    value = conn.execute("SELECT max(interval_end) FROM scada_readings").fetchone()[0]
    conn.commit()
    return value


def facilities(conn: psycopg.Connection, region: str | None = None) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT facility_code, max(facility_name) AS facility_name, max(region) AS region,
               round(sum(capacity_mw)::numeric, 1) AS capacity_mw, count(*) AS units,
               round(avg(latitude)::numeric, 4) AS latitude,
               round(avg(longitude)::numeric, 4) AS longitude
        FROM solar_units
        WHERE %(region)s::text IS NULL OR region = %(region)s
        GROUP BY facility_code
        ORDER BY max(facility_name)
        """,
        {"region": region},
    )


def facility_names(conn: psycopg.Connection) -> list[tuple[str, str]]:
    return [(r["facility_code"], r["facility_name"]) for r in facilities(conn)]


def find_facility(conn: psycopg.Connection, text: str) -> dict | None:
    """Resolve a facility by exact code, else by the shortest name containing `text`."""
    text = text.strip()
    rows = _rows(
        conn,
        """
        SELECT facility_code, max(facility_name) AS facility_name, max(region) AS region,
               round(sum(capacity_mw)::numeric, 1) AS capacity_mw
        FROM solar_units
        WHERE lower(facility_code) = lower(%(t)s) OR facility_name ILIKE %(like)s
        GROUP BY facility_code
        ORDER BY bool_or(lower(facility_code) = lower(%(t)s)) DESC,
                 length(max(facility_name))
        LIMIT 1
        """,
        {"t": text, "like": f"%{text}%"},
    )
    return rows[0] if rows else None


def underperformers(
    conn: psycopg.Connection,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 10,
    region: str | None = None,
) -> list[dict]:
    """Facilities below `threshold` of weather-expected output at the latest interval."""
    return _rows(
        conn,
        """
        SELECT facility_code, facility_name, region, interval_end,
               round(actual_mw::numeric, 1)   AS actual_mw,
               round(expected_mw::numeric, 1) AS expected_mw,
               round(performance_index::numeric, 2) AS performance_index
        FROM facility_performance
        WHERE interval_end = (SELECT max(interval_end) FROM scada_readings)
          AND performance_index < %(threshold)s
          AND (%(region)s::text IS NULL OR region = %(region)s)
        ORDER BY performance_index
        LIMIT %(limit)s
        """,
        {"threshold": threshold, "limit": limit, "region": region},
    )


_WINDOW = """
    interval_end > (SELECT max(interval_end) FROM scada_readings)
                   - make_interval(hours => %(hours)s)
"""


def facility_report(conn: psycopg.Connection, facility_code: str, hours: int) -> dict:
    """Summary plus hourly profile for one facility over the last `hours`."""
    params = {"code": facility_code, "hours": hours}
    where = f"facility_code = %(code)s AND {_WINDOW}"
    summary = _rows(
        conn,
        f"""
        SELECT count(*) AS intervals,
               round((sum(actual_mw) * 5 / 60)::numeric, 1) AS energy_mwh,
               round(max(actual_mw)::numeric, 1) AS peak_mw,
               round(avg(performance_index)::numeric, 2) AS avg_performance_index,
               round(min(performance_index)::numeric, 2) AS min_performance_index,
               max(interval_end) AS latest_interval
        FROM facility_performance
        WHERE {where}
        """,
        params,
    )[0]
    hourly = _rows(
        conn,
        f"""
        SELECT date_trunc('hour', interval_end) AS hour,
               round(avg(actual_mw)::numeric, 1) AS avg_mw,
               round(avg(expected_mw)::numeric, 1) AS expected_mw,
               round(avg(performance_index)::numeric, 2) AS performance_index
        FROM facility_performance
        WHERE {where}
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    )
    return {"summary": summary, "hourly": hourly}


def fleet_summary(conn: psycopg.Connection, hours: int, region: str | None = None) -> list[dict]:
    """Per-region energy and average performance over the last `hours`."""
    return _rows(
        conn,
        f"""
        WITH win AS (
            SELECT * FROM facility_performance
            WHERE {_WINDOW}
              AND (%(region)s::text IS NULL OR region = %(region)s)
        ),
        latest AS (
            SELECT region, count(*) FILTER (WHERE performance_index < %(threshold)s) AS n
            FROM win
            WHERE interval_end = (SELECT max(interval_end) FROM scada_readings)
            GROUP BY region
        )
        SELECT w.region,
               count(DISTINCT w.facility_code) AS facilities,
               round((sum(w.actual_mw) * 5 / 60)::numeric, 1) AS energy_mwh,
               round(avg(w.performance_index)::numeric, 2) AS avg_performance_index,
               coalesce(max(l.n), 0) AS underperforming_now
        FROM win w
        LEFT JOIN latest l USING (region)
        GROUP BY w.region
        ORDER BY energy_mwh DESC
        """,
        {"hours": hours, "region": region, "threshold": DEFAULT_THRESHOLD},
    )
