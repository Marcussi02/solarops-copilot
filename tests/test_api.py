"""HTTP API against a real Postgres: auth, validation, caching and the copilot route."""

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
KEY = {"x-api-key": "test-key"}


@pytest.fixture
def client(seeded, monkeypatch):
    pipeline.process_file(seeded, FILE, download=lambda n: make_scada_zip(ROWS))
    at = datetime(2026, 9, 25, 1, 45, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [WeatherObs("TESTSF", at, 1000.0, 25.0, 0.0), WeatherObs("OTHERSF", at, 1000.0, 25.0, 0.0)],
    )
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    config.api_key.cache_clear()
    api.cache.clear()
    api.app.dependency_overrides[api.get_conn] = lambda: seeded
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    config.api_key.cache_clear()


def test_health_is_public_and_reports_freshness(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["latest_interval"].startswith("2026-09-25T02:00:00")


def test_v1_requires_api_key(client):
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"x-api-key": "wrong"}).status_code == 401
    assert client.get("/v1/status", headers=KEY).json()["units"] == 3


def test_underperformers_filters_and_validates(client):
    rows = client.get("/v1/underperformers", headers=KEY).json()
    assert [r["facility_code"] for r in rows] == ["TESTSF"]
    assert client.get("/v1/underperformers?region=QLD1", headers=KEY).json() == []
    assert client.get("/v1/underperformers?threshold=9", headers=KEY).status_code == 422
    assert client.get("/v1/underperformers?region=MARS", headers=KEY).status_code == 422


def test_facility_detail_and_404(client):
    body = client.get("/v1/facilities/OTHERSF?hours=6", headers=KEY).json()
    assert body["facility"]["facility_name"] == "Other Solar Farm"
    assert body["summary"]["peak_mw"] == 150.0
    assert len(body["hourly"]) == 1
    assert client.get("/v1/facilities/NOPE", headers=KEY).status_code == 404


def test_read_endpoints_are_cached(client, seeded):
    first = client.get("/v1/fleet", headers=KEY)
    assert first.headers["cache-control"].startswith("public, max-age=")
    seeded.execute("DELETE FROM scada_readings")
    seeded.commit()
    assert client.get("/v1/fleet", headers=KEY).json() == first.json()  # served from cache
    api.cache.clear()
    assert client.get("/v1/fleet", headers=KEY).json() == []


def test_ask_returns_grounded_answer_with_trace(client):
    resp = client.post("/v1/ask", json={"question": "Which farms are underperforming?"},
                       headers=KEY)
    body = resp.json()
    assert resp.status_code == 200
    assert body["tool"] == "underperformers" and body["provider"] == "rules"
    assert body["data"]["farms"][0]["facility_code"] == "TESTSF"
    assert "Test Solar Farm" in body["answer"]


def test_ask_rejects_empty_and_oversized_questions(client):
    assert client.post("/v1/ask", json={"question": ""}, headers=KEY).status_code == 422
    assert client.post("/v1/ask", json={"question": "x" * 600}, headers=KEY).status_code == 422


def test_openapi_documents_every_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/v1/ask", "/v1/fleet", "/v1/underperformers"} <= set(paths)


def test_ttl_cache_evicts_oldest_when_full():
    cache = api.TTLCache(ttl_seconds=60, maxsize=2)
    for key in ("a", "b", "c"):
        cache.get_or_set((key,), lambda k=key: k)
    assert cache.get_or_set(("a",), lambda: "recomputed") == "recomputed"
