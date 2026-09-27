"""Observability: EMF metrics are emitted with the right names and dimensions, stay off
outside Lambda, and JSON logs carry a correlation id but never secrets."""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import make_scada_zip
from solarops import api, config, db, handlers, observability, pipeline
from solarops.copilot import agent
from solarops.copilot.llm import BedrockProvider, OpenAIProvider, cost_usd, price_per_mtok
from solarops.copilot.types import ProviderError, ToolCall
from solarops.weather import WeatherObs

FILE = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
ROWS = [
    ("2026/09/25 12:00:00", "TESTSF1", 40.0),
    ("2026/09/25 12:00:00", "TESTSF2", 20.0),
    ("2026/09/25 12:00:00", "OTHERSF1", 150.0),
]
SECRET_URL = "postgresql://postgres:hunter2-secret@db.example.com:5432/postgres"


@pytest.fixture
def in_lambda(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    monkeypatch.delenv("POWERTOOLS_METRICS_DISABLED", raising=False)


def emf(capsys) -> dict[str, list[dict]]:
    """Captured EMF records, keyed by metric name."""
    found: dict[str, list[dict]] = {}
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith('{"_aws"'):
            continue
        record = json.loads(line)
        (directive,) = record["_aws"]["CloudWatchMetrics"]
        assert directive["Namespace"] == "SolarOps"
        for metric in directive["Metrics"]:
            found.setdefault(metric["Name"], []).append(
                {"value": record[metric["Name"]], "unit": metric["Unit"],
                 "dimensions": {d: record[d] for d in directive["Dimensions"][0]}}
            )
    return found


@pytest.fixture
def logs():
    """JSON lines written through the Lambda logger (and the solarops.* loggers)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(observability.lambda_logger().registered_formatter)
    target = logging.getLogger("solarops")
    target.addHandler(handler)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    target.removeHandler(handler)


@pytest.fixture
def loaded(seeded):
    pipeline.process_file(seeded, FILE, download=lambda n: make_scada_zip(ROWS))
    at = datetime(2026, 9, 25, 1, 45, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [WeatherObs("TESTSF", at, 1000.0, 25.0, 0.0), WeatherObs("OTHERSF", at, 1000.0, 25.0, 0.0)],
    )
    return seeded


class UsageBedrock:
    """Bedrock client stub that reports token usage like the Converse API does."""

    def converse(self, **kwargs):
        usage = {"inputTokens": 1000, "outputTokens": 200}
        if "toolConfig" in kwargs:
            tool = {"name": "fleet_summary", "input": {"hours": 24}}
            return {"output": {"message": {"content": [{"toolUse": tool}]}}, "usage": usage}
        return {"output": {"message": {"content": [{"text": "Fleet ok."}]}}, "usage": usage}


class BrokenProvider:
    name = "broken"

    def choose_tool(self, question, specs, context):
        raise ProviderError(f"upstream said: could not connect to {SECRET_URL}")

    def summarise(self, question, call, result):
        raise AssertionError("not reached")


# ---------- emit ----------


def test_metrics_are_off_outside_lambda(capsys, monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    observability.emit("RowsIngested", 5)
    assert capsys.readouterr().out == ""  # stdout belongs to the MCP stdio transport


def test_emit_writes_one_emf_record_with_its_own_dimensions(capsys, in_lambda):
    observability.emit("ApiLatencyMs", 12.5, observability.MetricUnit.Milliseconds, route="/x")
    observability.emit("RowsIngested", 7)
    metrics = emf(capsys)
    assert metrics["ApiLatencyMs"] == [
        {"value": [12.5], "unit": "Milliseconds",
         "dimensions": {"service": "solarops", "route": "/x"}}
    ]
    assert metrics["RowsIngested"][0]["dimensions"] == {"service": "solarops"}


def test_metrics_can_be_disabled(capsys, in_lambda, monkeypatch):
    monkeypatch.setenv("POWERTOOLS_METRICS_DISABLED", "true")
    observability.emit("RowsIngested", 1)
    assert capsys.readouterr().out == ""


# ---------- token usage and cost ----------


def test_providers_count_reported_tokens():
    bedrock = BedrockProvider(model_id="apac.amazon.nova-lite-v1:0", client=UsageBedrock())
    call = bedrock.choose_tool("fleet", [], {})
    bedrock.summarise("fleet", call, {})
    assert (bedrock.usage.input_tokens, bedrock.usage.output_tokens) == (2000, 400)

    def post(body):
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5}}

    openai = OpenAIProvider(model="gpt-4o-mini", api_key="k", post=post)
    openai.summarise("q", ToolCall("fleet_summary", {}), {})
    assert (openai.usage.input_tokens, openai.usage.output_tokens) == (30, 5)


def test_cost_uses_list_prices_and_env_overrides(monkeypatch):
    monkeypatch.delenv("LLM_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)
    # Cross-region inference profiles are priced as the base model.
    assert price_per_mtok("apac.amazon.nova-lite-v1:0") == price_per_mtok("amazon.nova-lite-v1:0")
    assert cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert cost_usd("unknown-model", 5000, 5000) == 0.0
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "1")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "2")
    assert cost_usd("unknown-model", 1_000_000, 1_000_000) == pytest.approx(3.0)


# ---------- copilot ----------


def test_copilot_emits_latency_tokens_and_cost(loaded, capsys, in_lambda, monkeypatch):
    monkeypatch.delenv("LLM_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)
    provider = BedrockProvider(model_id="amazon.nova-lite-v1:0", client=UsageBedrock())
    answer = agent.ask(loaded, "How did the fleet do today?", provider=provider)
    assert answer.provider == "bedrock" and answer.fallback_reason is None

    metrics = emf(capsys)
    assert metrics["CopilotLatencyMs"][0]["value"] == [float(answer.latency_ms)]
    assert metrics["LlmInputTokens"][0]["value"] == [2000.0]
    assert metrics["LlmOutputTokens"][0]["value"] == [400.0]
    assert metrics["LlmCostUsd"][0]["value"][0] == pytest.approx(
        (2000 * 0.06 + 400 * 0.24) / 1_000_000
    )
    assert "CopilotFallbacks" not in metrics


def test_rules_provider_reports_zero_tokens_and_cost(loaded, capsys, in_lambda, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    agent.ask(loaded, "Which farms are underperforming?")
    metrics = emf(capsys)
    for name in ("LlmInputTokens", "LlmOutputTokens", "LlmCostUsd"):
        assert metrics[name][0]["value"] == [0.0], name


def test_fallback_counted_by_reason(loaded, capsys, in_lambda):
    answer = agent.ask(loaded, "Which farms are underperforming?", provider=BrokenProvider())
    assert answer.provider == "rules"
    (fallback,) = emf(capsys)["CopilotFallbacks"]
    assert fallback["value"] == [1.0]
    assert fallback["dimensions"] == {"service": "solarops", "reason": "ProviderError"}


# ---------- API ----------


@pytest.fixture
def client(loaded, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    config.api_key.cache_clear()
    api.cache.clear()
    api.app.dependency_overrides[api.get_conn] = lambda: loaded
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()
    config.api_key.cache_clear()


def test_api_latency_uses_route_template_not_raw_path(client, capsys, in_lambda):
    assert client.get("/v1/facilities/TESTSF", headers={"x-api-key": "test-key"}).is_success
    client.get("/no/such/path")
    routes = [m["dimensions"]["route"] for m in emf(capsys)["ApiLatencyMs"]]
    assert routes == ["/v1/facilities/{code}", "unmatched"]


@pytest.fixture
def event_loop_set():
    """Mangum calls asyncio.get_event_loop(); a fresh Lambda process has one, but an
    earlier asyncio.run() in this test session leaves the main thread without it."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    asyncio.set_event_loop(None)
    loop.close()


def http_event(path: str, body: dict | None = None) -> dict:
    """Minimal API Gateway HTTP API (payload v2) event."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"x-api-key": "test-key", "content-type": "application/json",
                    "host": "api.example.com"},
        "requestContext": {
            "requestId": "req-abc-123",
            "stage": "$default",
            "http": {"method": "POST" if body else "GET", "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "203.0.113.1", "userAgent": "pytest"},
        },
        "body": json.dumps(body) if body else None,
        "isBase64Encoded": False,
    }


def test_lambda_logs_are_json_with_correlation_id_and_no_secrets(
    client, logs, monkeypatch, lambda_context, event_loop_set
):
    monkeypatch.setattr(agent, "get_provider", lambda: BrokenProvider())
    resp = api.handler(http_event("/v1/ask", {"question": "Which farms are underperforming?"}),
                       lambda_context)
    assert resp["statusCode"] == 200

    lines = logs()
    fallback = next(line for line in lines if "fell back" in line["message"])
    assert fallback["correlation_id"] == "req-abc-123"
    assert fallback["function_request_id"] == "req-123"
    assert fallback["service"] == "solarops" and fallback["level"] == "WARNING"
    text = json.dumps(lines)
    assert "hunter2-secret" not in text and "test-key" not in text
    assert "postgresql://***@db.example.com" in text  # the useful part survives


# ---------- ingestion handlers ----------


def test_ingest_emits_rows_and_logs_message_id_without_secrets(
    seeded, monkeypatch, capsys, in_lambda, logs, lambda_context
):
    monkeypatch.setattr(handlers, "get_conn", lambda: seeded)
    real_process = pipeline.process_file

    def process(conn, name, archive=None):
        if name == "bad.zip":
            raise RuntimeError(f"cannot reach {SECRET_URL}")
        return real_process(conn, name, download=lambda n: make_scada_zip(ROWS))

    monkeypatch.setattr(pipeline, "process_file", process)
    event = {"Records": [
        {"messageId": "m1", "body": json.dumps({"file": FILE})},
        {"messageId": "m2", "body": json.dumps({"file": "bad.zip"})},
    ]}
    assert handlers.ingest_handler(event, lambda_context) == {
        "batchItemFailures": [{"itemIdentifier": "m2"}]
    }
    assert emf(capsys)["RowsIngested"][0]["value"] == [3.0]

    (failed,) = [line for line in logs() if line["level"] == "ERROR"]
    assert failed["correlation_id"] == "m2"
    assert "hunter2-secret" not in json.dumps(failed)


def test_poll_emits_ingest_lag_and_curtailed_farms(
    loaded, monkeypatch, capsys, in_lambda, lambda_context
):
    monkeypatch.setattr(handlers, "get_conn", lambda: loaded)
    monkeypatch.setattr(handlers.nemweb, "list_files", lambda: [])
    monkeypatch.setattr(handlers.pipeline, "sync_prices", lambda conn: 0)
    monkeypatch.setattr(handlers.boto3, "client", lambda name: None)
    handlers.poll_handler({}, lambda_context)

    metrics = emf(capsys)
    latest = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    expected_lag = (datetime.now(UTC) - latest).total_seconds() / 60
    assert metrics["IngestLagMinutes"][0]["value"][0] == pytest.approx(expected_lag, abs=1)
    assert metrics["CurtailedFarms"][0]["value"] == [0.0]


# ---------- redaction ----------


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        (SECRET_URL, "hunter2-secret"),
        ('{"x-api-key": "abc123def456"}', "abc123def456"),
        ("headers x-api-key=abc123def456 sent", "abc123def456"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("host=db user=postgres password=hunter2 dbname=x", "hunter2"),
        ("OPENAI key sk-proj-ABCDEFGHIJKLMNOPQRSTUV was rejected", "ABCDEFGHIJKLMNOPQRSTUV"),
    ],
)
def test_redact_masks_secrets(raw, leaked):
    assert leaked not in observability.redact(raw)


def test_redact_keeps_ordinary_text():
    text = 'password authentication failed for user "postgres"; input_tokens: 120'
    assert observability.redact(text) == text
