"""HTTP API: FastAPI app, served on Lambda behind API Gateway via Mangum.

    GET  /health                       liveness + data freshness (public)
    GET  /v1/status                    pipeline counters
    GET  /v1/facilities                solar farms, optional ?region=
    GET  /v1/facilities/{code}         one farm: energy, peak, performance, hourly profile
    GET  /v1/underperformers           farms below expected output right now
    GET  /v1/fleet                     energy and performance per region
    POST /v1/ask                       natural-language question -> grounded answer

/v1 routes need an `x-api-key` header when API_KEY is configured. Rate limiting is
enforced by API Gateway throttling (see template.yaml), and read endpoints are cached
in-process for CACHE_TTL_SECONDS because the data only changes every 5 minutes.
The database session is READ ONLY with a statement timeout.
"""

import hmac
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response
from mangum import Mangum
from pydantic import BaseModel, Field

from . import config, db, queries
from .copilot import agent
from .copilot.tools import Region

app = FastAPI(
    title="SolarOps Copilot API",
    version="0.2.0",
    description="Live performance of Australian utility-scale solar farms (AEMO NEM data).",
)

# ---------- infrastructure ----------

_conn: psycopg.Connection | None = None


def _open_read_only() -> psycopg.Connection:
    conn = db.connect(config.database_url())
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.execute("SET statement_timeout = '5s'")
    conn.commit()
    return conn


def get_conn() -> Iterator[psycopg.Connection]:
    """One read-only connection per warm Lambda container, rolled back on error."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = _open_read_only()
    try:
        yield _conn
    except Exception:
        if not _conn.closed:
            _conn.rollback()
        raise


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


class TTLCache:
    def __init__(self, ttl_seconds: float, maxsize: int = 256):
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self._items: dict[tuple, tuple[float, object]] = {}

    def get_or_set(self, key: tuple, compute: Callable[[], object]):
        now = time.monotonic()
        hit = self._items.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        value = compute()
        if len(self._items) >= self.maxsize:
            self._items.pop(next(iter(self._items)))
        self._items[key] = (now, value)
        return value

    def clear(self) -> None:
        self._items.clear()


cache = TTLCache(float(os.environ.get("CACHE_TTL_SECONDS", "60")))


def cached(response: Response, key: tuple, compute: Callable[[], object]):
    response.headers["Cache-Control"] = f"public, max-age={int(cache.ttl)}"
    return cache.get_or_set(key, compute)


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = config.api_key()
    if expected is None:
        return  # local development: auth disabled
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


# ---------- routes ----------


@app.get("/health", tags=["ops"])
def health(conn: Conn):
    try:
        latest = queries.latest_interval(conn)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    lag = None if latest is None else round((datetime.now(UTC) - latest).total_seconds() / 60)
    return {"status": "ok", "latest_interval": latest, "data_lag_minutes": lag}


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


@v1.get("/status", tags=["data"])
def status(conn: Conn, response: Response):
    return cached(response, ("status",), lambda: db.status(conn))


@v1.get("/facilities", tags=["data"])
def list_facilities(conn: Conn, response: Response, region: Region | None = None):
    return cached(response, ("facilities", region), lambda: queries.facilities(conn, region))


@v1.get("/facilities/{code}", tags=["data"])
def facility(
    code: str,
    conn: Conn,
    response: Response,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
):
    match = queries.find_facility(conn, code)
    if match is None or match["facility_code"].lower() != code.lower():
        raise HTTPException(status_code=404, detail=f"unknown facility {code!r}")
    report = cached(
        response,
        ("facility", match["facility_code"], hours),
        lambda: queries.facility_report(conn, match["facility_code"], hours),
    )
    return {"facility": match, "hours": hours, **report}


@v1.get("/underperformers", tags=["data"])
def underperformers(
    conn: Conn,
    response: Response,
    threshold: Annotated[float, Query(ge=0.05, le=1.5)] = queries.DEFAULT_THRESHOLD,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    region: Region | None = None,
):
    return cached(
        response,
        ("under", threshold, limit, region),
        lambda: queries.underperformers(conn, threshold, limit, region),
    )


@v1.get("/fleet", tags=["data"])
def fleet(
    conn: Conn,
    response: Response,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    region: Region | None = None,
):
    return cached(
        response, ("fleet", hours, region), lambda: queries.fleet_summary(conn, hours, region)
    )


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=agent.MAX_QUESTION_CHARS)


@v1.post("/ask", tags=["copilot"])
def ask(body: AskRequest, conn: Conn):
    return agent.ask(conn, body.question).to_dict()


app.include_router(v1)

handler = Mangum(app, lifespan="off")
