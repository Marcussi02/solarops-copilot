"""Irradiance and weather at each solar farm from Open-Meteo (no API key).

Open-Meteo accepts comma-separated coordinates and returns a list of results,
so all farms are fetched in a handful of requests.

- `fetch_current`: 15-minute "current" conditions, polled every 15 minutes.
- `fetch_recent`: hourly history for the last few hours, used to backfill gaps
  (a missed poll, a first deploy) so past intervals can still be scored.
"""

import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .http import get_json

BASE_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = ("shortwave_radiation", "temperature_2m", "cloud_cover")
CHUNK_SIZE = 50


@dataclass(frozen=True)
class Site:
    facility_code: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class WeatherObs:
    facility_code: str
    observed_at: datetime
    ghi_wm2: float | None
    temp_c: float | None
    cloud_cover_pct: float | None


def build_url(sites: list[Site]) -> str:
    params = {
        "latitude": ",".join(f"{s.latitude:.4f}" for s in sites),
        "longitude": ",".join(f"{s.longitude:.4f}" for s in sites),
        "current": ",".join(CURRENT_FIELDS),
        "timezone": "GMT",
    }
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def build_recent_url(sites: list[Site], past_hours: int) -> str:
    params = {
        "latitude": ",".join(f"{s.latitude:.4f}" for s in sites),
        "longitude": ",".join(f"{s.longitude:.4f}" for s in sites),
        "hourly": ",".join(CURRENT_FIELDS),
        "past_hours": past_hours,
        "forecast_hours": 1,
        "timezone": "GMT",
    }
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def fetch_recent(
    sites: Iterable[Site],
    past_hours: int = 3,
    fetch: Callable[[str], object] = get_json,
    now: datetime | None = None,
) -> list[WeatherObs]:
    """Hourly observations for the last `past_hours`.

    Open-Meteo's hourly irradiance is the mean over the *preceding* hour, so each
    value is stored at the middle of that hour (timestamp - 30 min). Anything that
    has not happened yet is dropped.
    """
    now = now or datetime.now(UTC)
    sites = list(sites)
    observations: list[WeatherObs] = []
    for start in range(0, len(sites), CHUNK_SIZE):
        chunk = sites[start : start + CHUNK_SIZE]
        payload = fetch(build_recent_url(chunk, past_hours))
        results = payload if isinstance(payload, list) else [payload]
        if len(results) != len(chunk):
            raise ValueError(f"expected {len(chunk)} results, got {len(results)}")
        for site, result in zip(chunk, results, strict=True):
            hourly = result.get("hourly") or {}
            columns = [hourly.get(f, []) for f in CURRENT_FIELDS]
            for stamp, ghi, temp, cloud in zip(hourly.get("time", []), *columns, strict=False):
                end = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
                if end > now:
                    continue
                observations.append(
                    WeatherObs(site.facility_code, end - timedelta(minutes=30), ghi, temp, cloud)
                )
    return observations


def fetch_current(
    sites: Iterable[Site], fetch: Callable[[str], object] = get_json
) -> list[WeatherObs]:
    sites = list(sites)
    observations: list[WeatherObs] = []
    for start in range(0, len(sites), CHUNK_SIZE):
        chunk = sites[start : start + CHUNK_SIZE]
        payload = fetch(build_url(chunk))
        results = payload if isinstance(payload, list) else [payload]
        if len(results) != len(chunk):
            raise ValueError(f"expected {len(chunk)} results, got {len(results)}")
        for site, result in zip(chunk, results, strict=True):
            observations.append(_to_obs(site, result))
    return observations


def _to_obs(site: Site, result: dict) -> WeatherObs:
    current = result.get("current") or {}
    observed = datetime.fromisoformat(current["time"]).replace(tzinfo=UTC)
    return WeatherObs(
        facility_code=site.facility_code,
        observed_at=observed,
        ghi_wm2=current.get("shortwave_radiation"),
        temp_c=current.get("temperature_2m"),
        cloud_cover_pct=current.get("cloud_cover"),
    )
