"""HTTP API: FastAPI app, served on Lambda behind API Gateway via Mangum.

    GET  /health                       liveness + data freshness (public)
    GET  /dashboard                    live fleet dashboard, one self-contained HTML page (public)
    GET  /public/status                data freshness and farm count (public)
    GET  /public/fleet                 energy and performance per region, last 24 h (public)
    GET  /public/underperformers       farms below expected output right now (public)
    GET  /public/curtailed             farms held back by dispatch caps or prices (public)
    GET  /v1/status                    pipeline counters
    GET  /v1/facilities                solar farms, optional ?region=
    GET  /v1/facilities/{code}         one farm: energy, peak, performance, hourly profile
    GET  /v1/underperformers           farms below expected output right now
    GET  /v1/curtailed                 farms held back by dispatch caps or negative prices
    GET  /v1/fleet                     energy and performance per region
    GET  /v1/docs                      knowledge base documents and sections
    GET  /v1/docs/search?q=            retrieve cited passages from the knowledge base
    POST /v1/ask                       natural-language question -> grounded answer

/v1 routes need an `x-api-key` header when API_KEY is configured. /public routes are
unauthenticated: read-only aggregates from the same query catalogue, with fixed
windows and limits (no client input reaches the query or the cache key), cached for
5 minutes in-process and by clients (Cache-Control), and throttled harder per route
at API Gateway. Rate limiting is enforced by API Gateway throttling
(see template.yaml), and read endpoints are cached in-process for CACHE_TTL_SECONDS
because the data only changes every 5 minutes.
The database session is READ ONLY with a statement timeout.

Every request publishes ApiLatencyMs by route template (never the raw path, so the
dimension stays bounded), and each Lambda invocation is traced in X-Ray and logged
as JSON with the API Gateway request id as the correlation id.
"""

import hmac
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from importlib import resources
from typing import Annotated

import psycopg
from aws_lambda_powertools.logging import correlation_paths
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel, Field

from . import config, db, queries
from .copilot import agent
from .copilot.tools import Region
from .observability import MetricUnit, emit, entry_point
from .rag import get_retriever

app = FastAPI(
    title="SolarOps Copilot API",
    version="0.3.0",
    description="Live performance of Australian utility-scale solar farms (AEMO NEM data).",
)

# ---------- infrastructure ----------

_conn: psycopg.Connection | None = None


def get_conn() -> Iterator[psycopg.Connection]:
    """One read-only connection per warm Lambda container, rolled back on error."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = db.connect_read_only()
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


def cached(response: Response, key: tuple, compute: Callable[[], object], store=None):
    store = store or cache
    response.headers["Cache-Control"] = f"public, max-age={int(store.ttl)}"
    return store.get_or_set(key, compute)


# Public data is refreshed at most every 5 minutes, whatever CACHE_TTL_SECONDS says.
PUBLIC_TTL_SECONDS = 300
PUBLIC_LIMIT = 20
PUBLIC_FLEET_HOURS = 24
public_cache = TTLCache(PUBLIC_TTL_SECONDS, maxsize=64)


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = config.api_key()
    if expected is None:
        return  # local development: auth disabled
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="missing or invalid x-api-key")


@app.middleware("http")
async def record_latency(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    emit(
        "ApiLatencyMs",
        round((time.perf_counter() - started) * 1000, 1),
        MetricUnit.Milliseconds,
        route=getattr(route, "path", "unmatched"),
    )
    return response


# ---------- routes ----------


def data_lag_minutes(latest: datetime | None) -> int | None:
    return None if latest is None else round((datetime.now(UTC) - latest).total_seconds() / 60)


@app.get("/health", tags=["ops"])
def health(conn: Conn):
    try:
        latest = queries.latest_interval(conn)
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    lag = data_lag_minutes(latest)
    return {"status": "ok", "latest_interval": latest, "data_lag_minutes": lag}


public = APIRouter(prefix="/public", tags=["public"])


@public.get("/status")
def public_status(conn: Conn, response: Response):
    def build():
        counts = db.status(conn)
        return {
            "latest_interval": counts["latest_interval"],
            "latest_weather": counts["latest_weather"],
            "facilities": counts["facilities"],
            "units": counts["units"],
            "generated_at": datetime.now(UTC),
        }

    body = cached(response, ("public_status",), build, public_cache)
    # Lag is computed per request so a cached body never understates staleness.
    return {**body, "data_lag_minutes": data_lag_minutes(body["latest_interval"])}


@public.get("/fleet")
def public_fleet(conn: Conn, response: Response):
    """Fixed 24-hour window: one cache entry, so query strings can't bypass the cache."""
    return cached(
        response,
        ("public_fleet",),
        lambda: queries.fleet_summary(conn, PUBLIC_FLEET_HOURS),
        public_cache,
    )


@public.get("/underperformers")
def public_underperformers(conn: Conn, response: Response):
    return cached(
        response,
        ("public_under",),
        lambda: queries.underperformers(conn, queries.DEFAULT_THRESHOLD, PUBLIC_LIMIT),
        public_cache,
    )


@public.get("/curtailed")
def public_curtailed(conn: Conn, response: Response):
    return cached(
        response,
        ("public_curtailed",),
        lambda: queries.curtailed_farms(conn, PUBLIC_LIMIT),
        public_cache,
    )


DASHBOARD_HTML = resources.files("solarops").joinpath("static/dashboard.html").read_text()


@app.get("/dashboard", response_class=HTMLResponse, tags=["public"])
def dashboard():
    return HTMLResponse(
        DASHBOARD_HTML, headers={"Cache-Control": f"public, max-age={PUBLIC_TTL_SECONDS}"}
    )


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


@v1.get("/curtailed", tags=["data"])
def curtailed(
    conn: Conn,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    region: Region | None = None,
):
    return cached(
        response, ("curtailed", limit, region), lambda: queries.curtailed_farms(conn, limit, region)
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


@v1.get("/docs", tags=["knowledge"])
def list_docs(response: Response):
    def build():
        retriever = get_retriever()
        docs: dict[str, dict] = {}
        for c in retriever.chunks:
            docs.setdefault(c.doc, {"id": c.doc, "title": c.title, "sections": []})
            docs[c.doc]["sections"].append({"id": c.id, "section": c.section})
        return {"corpus_version": retriever.version, "documents": list(docs.values())}

    return cached(response, ("docs",), build)


@v1.get("/docs/search", tags=["knowledge"])
def search_docs(
    response: Response,
    q: Annotated[str, Query(min_length=3, max_length=300)],
    k: Annotated[int, Query(ge=1, le=8)] = 4,
):
    def build():
        retriever = get_retriever()
        hits = retriever.search(q, k)
        return {
            "query": q,
            "corpus_version": retriever.version,
            "results": [h.to_dict(n) for n, h in enumerate(hits, start=1)],
        }

    return cached(response, ("docs_search", q, k), build)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=agent.MAX_QUESTION_CHARS)


@v1.post("/ask", tags=["copilot"])
def ask(body: AskRequest, conn: Conn):
    return agent.ask(conn, body.question).to_dict()


app.include_router(public)
app.include_router(v1)

_mangum = Mangum(app, lifespan="off")


@entry_point(correlation_id_path=correlation_paths.API_GATEWAY_HTTP)
def handler(event, context):
    return _mangum(event, context)
