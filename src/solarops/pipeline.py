"""Pipeline steps shared by the Lambda handlers and the local CLI.

Dependencies (downloads, archive) are injectable so every step is testable offline.
"""

import logging
from collections.abc import Callable

import psycopg

from . import db, dispatch, nemweb, registry, weather

logger = logging.getLogger(__name__)

Archive = Callable[[str, bytes], None]


class RegistryEmptyError(RuntimeError):
    """No solar units loaded yet: ingesting now would silently drop every reading."""


def sync_registry(conn: psycopg.Connection, fetch=None) -> int:
    if fetch is None:
        from . import config

        def fetch():
            return registry.fetch_solar_units(config.openelectricity_api_key())

    units = fetch()
    if not units:
        raise RuntimeError("registry returned no solar units; refusing to continue")
    return db.upsert_units(conn, units)


def pending_files(
    conn: psycopg.Connection, listing: list[str], max_files: int = 36
) -> list[str]:
    """Most recent `max_files` files that have not been ingested yet, oldest first."""
    recent = listing[-max_files:]
    done = db.processed_files(conn, recent)
    conn.commit()
    return [name for name in recent if name not in done]


def process_file(
    conn: psycopg.Connection,
    file_name: str,
    download: Callable[[str], bytes] = nemweb.download,
    archive: Archive | None = None,
) -> dict:
    already = db.processed_files(conn, [file_name])
    solar_duids = db.known_duids(conn)
    conn.commit()
    if already:
        return {"file": file_name, "skipped": True}
    if not solar_duids:
        # Fail loudly so SQS retries later instead of recording the file as done.
        raise RegistryEmptyError("solar_units is empty; run the registry sync first")
    data = download(file_name)
    if archive:
        archive(file_name, data)
    readings = nemweb.parse_scada(data)
    solar_rows = db.ingest_file(
        conn, file_name, nemweb.file_interval(file_name), readings, solar_duids
    )
    logger.info("ingested %s rows=%d solar=%d", file_name, len(readings), solar_rows)
    return {"file": file_name, "rows": len(readings), "solar_rows": solar_rows}


def sync_weather(conn: psycopg.Connection, fetch=weather.fetch_current) -> int:
    sites = db.facility_sites(conn)
    conn.commit()
    if not sites:
        return 0
    return db.upsert_weather(conn, fetch(sites))


def backfill_weather(conn: psycopg.Connection, hours: int, fetch=None) -> int:
    """Upsert hourly weather for the last `hours`, filling any gaps in the 15-min polls."""
    sites = db.facility_sites(conn)
    conn.commit()
    if not sites:
        return 0
    if fetch is None:
        def fetch(sites):
            return weather.fetch_recent(sites, hours)
    return db.upsert_weather(conn, fetch(sites))


def sync_prices(
    conn: psycopg.Connection, listing: list[str] | None = None, max_files: int = 12, download=None
) -> int:
    """Load regional prices for recent 5-minute intervals not yet recorded."""
    listing = dispatch.list_price_files() if listing is None else listing
    download = download or dispatch.download_price_file
    stored = 0
    for name in pending_files(conn, listing, max_files):
        prices = dispatch.parse_prices(download(name))
        stored += db.ingest_prices(conn, name, dispatch.price_file_interval(name), prices)
    return stored


def sync_unit_dispatch(
    conn: psycopg.Connection, listing: list[str] | None = None, max_files: int = 2, download=None
) -> dict:
    """Load yesterday's per-unit dispatch outcomes (published once a day)."""
    listing = dispatch.list_next_day_files() if listing is None else listing
    download = download or dispatch.download_next_day_file
    duids = db.known_duids(conn)
    conn.commit()
    if not duids:
        raise RegistryEmptyError("solar_units is empty; run the registry sync first")
    loaded = {}
    for name in pending_files(conn, listing, max_files):
        rows = dispatch.parse_unit_dispatch(download(name), duids)
        loaded[name] = db.ingest_unit_dispatch(conn, name, dispatch.next_day_file_date(name), rows)
        logger.info("dispatch %s solar rows=%d", name, loaded[name])
    return loaded
