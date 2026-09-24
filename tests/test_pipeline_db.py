"""Integration tests against a real PostgreSQL (TEST_DATABASE_URL)."""

from datetime import UTC, datetime

from conftest import UNITS, make_scada_zip
from solarops import db, nemweb, pipeline
from solarops.weather import WeatherObs

FILE = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
ROWS = [
    ("2026/09/25 12:00:00", "TESTSF1", 40.0),
    ("2026/09/25 12:00:00", "TESTSF2", 20.0),
    ("2026/09/25 12:00:00", "OTHERSF1", 150.0),
    ("2026/09/25 12:00:00", "COAL1", 650.0),  # not solar: must be dropped
]


def count(conn, table):
    value = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    conn.commit()
    return value


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == []


def test_sync_registry_upserts(conn):
    assert pipeline.sync_registry(conn, fetch=lambda: UNITS) == 3
    assert pipeline.sync_registry(conn, fetch=lambda: UNITS) == 3
    assert count(conn, "solar_units") == 3


def test_process_file_keeps_solar_only_and_is_idempotent(seeded):
    downloads = []

    def fake_download(name):
        downloads.append(name)
        return make_scada_zip(ROWS)

    first = pipeline.process_file(seeded, FILE, download=fake_download)
    assert first["rows"] == 4 and first["solar_rows"] == 3

    second = pipeline.process_file(seeded, FILE, download=fake_download)
    assert second["skipped"] is True
    assert downloads == [FILE]  # the ledger prevents a second download
    assert count(seeded, "scada_readings") == 3
    assert count(seeded, "ingested_files") == 1


def test_process_file_refuses_when_registry_empty(conn):
    import pytest

    with pytest.raises(pipeline.RegistryEmptyError):
        pipeline.process_file(conn, FILE, download=lambda n: make_scada_zip(ROWS))
    assert count(conn, "ingested_files") == 0  # not recorded, so it will be retried


def test_redelivery_race_does_not_duplicate(seeded):
    """Two workers ingesting the same file: upserts keep one row per key."""
    readings = nemweb.parse_scada(make_scada_zip(ROWS))
    interval = nemweb.file_interval(FILE)
    duids = db.known_duids(seeded)
    db.ingest_file(seeded, FILE, interval, readings, duids)
    db.ingest_file(seeded, FILE, interval, readings, duids)
    assert count(seeded, "scada_readings") == 3
    assert count(seeded, "ingested_files") == 1


def test_pending_files_excludes_processed_and_limits(seeded):
    listing = [
        f"PUBLIC_DISPATCHSCADA_2026092512{m:02d}_000000000000000{i}.zip"
        for i, m in enumerate([0, 5, 10])
    ]
    pipeline.process_file(seeded, listing[0], download=lambda n: make_scada_zip(ROWS))
    assert pipeline.pending_files(seeded, listing, max_files=3) == listing[1:]
    assert pipeline.pending_files(seeded, listing, max_files=1) == listing[2:]


def test_performance_view_flags_underperformer(seeded):
    pipeline.process_file(seeded, FILE, download=lambda n: make_scada_zip(ROWS))
    # 12:00 NEM time = 02:00 UTC. Clear sky at both farms.
    obs_time = datetime(2026, 9, 25, 1, 45, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [
            WeatherObs("TESTSF", obs_time, 1000.0, 25.0, 0.0),
            WeatherObs("OTHERSF", obs_time, 1000.0, 25.0, 0.0),
        ],
    )
    rows = db.underperformers(seeded, threshold=0.6)
    # TESTSF: 60 MW actual vs 150 * 0.8 = 120 expected -> 0.5 (flagged)
    # OTHERSF: 150 MW actual vs 160 expected -> 0.94 (fine)
    assert [r["facility_code"] for r in rows] == ["TESTSF"]
    assert float(rows[0]["performance_index"]) == 0.5
    assert float(rows[0]["expected_mw"]) == 120.0


def test_sync_weather_uses_one_site_per_facility(seeded):
    seen = []
    at = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)

    def fake_fetch(sites):
        seen.extend(s.facility_code for s in sites)
        return [WeatherObs(s.facility_code, at, 700.0, 20.0, 5.0) for s in sites]

    assert pipeline.sync_weather(seeded, fetch=fake_fetch) == 2
    assert sorted(seen) == ["OTHERSF", "TESTSF"]
