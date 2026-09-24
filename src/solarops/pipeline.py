"""Pipeline steps shared by the Lambda handlers and the local CLI.

Dependencies (downloads, archive) are injectable so every step is testable offline.
"""

import logging
from collections.abc import Callable

import psycopg

from . import db, nemweb, registry, weather

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
