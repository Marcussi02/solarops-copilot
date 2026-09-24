from datetime import UTC, datetime

import pytest

from solarops import weather
from solarops.performance import expected_mw, performance_index


def current(ghi, time="2026-09-25T02:15"):
    return {
        "current": {
            "time": time,
            "shortwave_radiation": ghi,
            "temperature_2m": 21.5,
            "cloud_cover": 10,
        }
    }


def test_fetch_current_handles_multi_location_list_and_chunks(monkeypatch):
    monkeypatch.setattr(weather, "CHUNK_SIZE", 2)
    sites = [weather.Site(f"F{i}", -30.0 - i, 145.0) for i in range(3)]
    calls = []

    def fake_fetch(url):
        calls.append(url)
        n = url.split("latitude=")[1].split("&")[0].count("%2C") + 1
        return [current(800)] * n if n > 1 else current(800)  # single site returns a dict

    obs = weather.fetch_current(sites, fetch=fake_fetch)
    assert len(calls) == 2
    assert [o.facility_code for o in obs] == ["F0", "F1", "F2"]
    assert obs[0].observed_at == datetime(2026, 9, 25, 2, 15, tzinfo=UTC)
    assert obs[0].ghi_wm2 == 800


def test_fetch_current_rejects_mismatched_results():
    sites = [weather.Site("A", -30, 145), weather.Site("B", -31, 146)]
    with pytest.raises(ValueError):
        weather.fetch_current(sites, fetch=lambda url: [current(500)])


def test_expected_output_scales_with_irradiance_and_caps_at_capacity():
    assert expected_mw(100, 1000) == pytest.approx(80)
    assert expected_mw(100, 500) == pytest.approx(40)
    assert expected_mw(100, 1500, performance_ratio=1.0) == 100
    assert expected_mw(100, -5) == 0
    assert expected_mw(100, None) is None


def test_performance_index_ignores_low_light():
    assert performance_index(60, 80, 100) == pytest.approx(0.75)
    assert performance_index(1, 2, 100) is None  # expected < 5% of capacity
    assert performance_index(-1, 80, 100) == 0
