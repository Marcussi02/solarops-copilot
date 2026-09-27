"""Public dashboard: unauthenticated aggregate routes, caching, and the HTML page."""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import make_scada_zip
from solarops import api, config, db, pipeline
from solarops.weather import WeatherObs

FILE = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
ROWS = [
    ("2026/09/25 12:00:00", "TESTSF1", 40.0),
    ("2026/09/25 12:00:00", "TESTSF2", 20.0),
    ("2026/09/25 12:00:00", "OTHERSF1", 150.0),
]
PUBLIC = ["/public/status", "/public/fleet", "/public/underperformers",
          "/public/curtailed"]


@pytest.fixture
def client(seeded, monkeypatch):
    pipeline.process_file(seeded, FILE, download=lambda n: make_scada_zip(ROWS))
    at = datetime(2026, 9, 25, 1, 45, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [WeatherObs("TESTSF", at, 1000.0, 25.0, 0.0), WeatherObs("OTHERSF", at, 1000.0, 25.0, 0.0)],
    )
    monkeypatch.setenv("API_KEY", "test-key")
    config.api_key.cache_clear()
    api.cache.clear()
    api.public_cache.clear()
    api.app.dependency_overrides[api.get_conn] = lambda: seeded
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    config.api_key.cache_clear()


@pytest.mark.parametrize("path", PUBLIC)
def test_public_routes_need_no_key_and_cache_for_five_minutes(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_v1_still_requires_the_key(client):
    for path in ("/v1/status", "/v1/fleet", "/v1/underperformers", "/v1/curtailed"):
        assert client.get(path).status_code == 401, path
    assert client.get("/v1/fleet", headers={"x-api-key": "test-key"}).status_code == 200


def test_public_data_is_aggregate_and_reuses_the_catalogue(client):
    status = client.get("/public/status").json()
    assert status["facilities"] == 2 and status["units"] == 3
    assert status["latest_interval"].startswith("2026-09-25T02:00:00")
    assert status["data_lag_minutes"] > 0
    assert "files" not in status and "readings" not in status  # pipeline internals stay private

    fleet = client.get("/public/fleet").json()
    assert {r["region"] for r in fleet} == {"NSW1", "QLD1"}
    assert client.get("/public/underperformers").json()[0]["facility_code"] == "TESTSF"
    assert client.get("/public/curtailed").json() == []


def test_public_ignores_client_parameters(client):
    # Fixed windows and limits: callers cannot widen the query or ask for more rows.
    assert client.get("/public/fleet?hours=999").status_code == 200
    assert client.get("/public/underperformers?limit=50000").status_code == 200
    params = client.get("/openapi.json").json()["paths"]["/public/fleet"]["get"]
    assert not params.get("parameters")


def test_public_fleet_query_string_cannot_bypass_the_cache(client, seeded):
    first = client.get("/public/fleet").json()
    assert first
    seeded.execute("DELETE FROM scada_readings")
    seeded.commit()
    for path in ("/public/fleet?hours=1", "/public/fleet?hours=168", "/public/fleet?x=1"):
        assert client.get(path).json() == first, path  # one cache entry for every variant


def test_public_server_cache_holds_for_its_ttl(client, seeded):
    first = client.get("/public/fleet").json()
    seeded.execute("DELETE FROM scada_readings")
    seeded.commit()
    assert client.get("/public/fleet").json() == first  # served from the 300 s cache
    assert api.public_cache.ttl == 300
    api.public_cache.clear()
    assert client.get("/public/fleet").json() == []


def test_public_cache_is_independent_of_the_v1_cache(client, monkeypatch):
    monkeypatch.setattr(api.cache, "ttl", 1)
    assert client.get("/v1/fleet", headers={"x-api-key": "test-key"}).headers[
        "cache-control"] == "public, max-age=1"
    assert client.get("/public/fleet").headers["cache-control"] == "public, max-age=300"


def test_dashboard_is_a_self_contained_html_page(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "public, max-age=300"
    html = resp.text
    assert "<title>SolarOps live fleet</title>" in html
    for path in ("public/status", '"public/fleet"', "public/underperformers",
                 "public/curtailed"):
        assert path in html
    assert "prefers-color-scheme: dark" in html and 'name="viewport"' in html
    # No CDNs: nothing is loaded from another origin.
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="(https?:)?//', html)
    assert "@import" not in html and "fetch(\"http" not in html


def test_public_routes_are_documented(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/public/status", "/public/fleet", "/public/underperformers",
            "/public/curtailed", "/dashboard"} <= paths
