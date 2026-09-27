"""Tracing, structured logs and custom metrics (AWS Lambda Powertools).

    tracer   X-Ray subsegments around handlers, DB queries and model calls
    emit()   one CloudWatch metric as an EMF log line (namespace SolarOps)
    lambda_logger() / entry_point()   JSON logs with a correlation id, for Lambda handlers

Everything here is inert outside Lambda: the tracer disables itself, and metrics are
only written when running in Lambda, so the CLI and the MCP server (whose stdio
transport owns stdout) behave exactly as before. Logs never contain secrets: the
formatter masks connection-string credentials, API keys and bearer tokens.
"""

import os
import re
import sys
from collections.abc import Callable
from functools import lru_cache

from aws_lambda_powertools import Logger, Tracer, single_metric
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter
from aws_lambda_powertools.metrics import MetricUnit

SERVICE = "solarops"
NAMESPACE = "SolarOps"

# Auto-trace AWS SDK calls (Bedrock, SQS, S3, SSM) and HTTP via urllib (OpenAI, NEMWeb,
# Open-Meteo). Postgres is traced explicitly around each query function instead of by
# patching psycopg, which would wrap every connection in a DB-API proxy.
tracer = Tracer(service=SERVICE, patch_modules=("botocore", "httplib"))

__all__ = ["MetricUnit", "emit", "entry_point", "lambda_logger", "redact", "tracer"]


# ---------- metrics ----------


def metrics_enabled() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME")) and os.environ.get(
        "POWERTOOLS_METRICS_DISABLED", "false"
    ).lower() not in ("1", "true")


def emit(name: str, value: float, unit: MetricUnit = MetricUnit.Count, **dimensions: str) -> None:
    """Publish one metric now, as its own EMF record.

    Each record carries only its own dimensions, so a per-route or per-reason metric
    never leaks its dimension onto the other metrics written in the same invocation.
    Keep dimension values low-cardinality (route templates, exception types)."""
    if not metrics_enabled():
        return
    with single_metric(
        name=name,
        unit=unit,
        value=value,
        namespace=NAMESPACE,
        default_dimensions={"service": SERVICE},
    ) as metric:
        for key, dim in dimensions.items():
            metric.add_dimension(name=key, value=str(dim)[:100])


# ---------- logs ----------

_SECRETS = [
    # user:password@ in connection URLs (postgresql://, https://, ...)
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE), r"\1***@"),
    # key=value / "key": "value" pairs, including libpq keyword DSNs
    (
        re.compile(
            r"(x-api-key|api[_-]?key|authorization|password|secret|token)"
            r"(\\?[\"']?\s*[:=]\s*\\?[\"']?)(bearer\s+)?[^\s,;&'\"\\}]+",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(LambdaPowertoolsFormatter):
    """Powertools JSON formatter that masks secrets in the final line, so messages,
    exception tracebacks and extra keys are all covered."""

    def serialize(self, log: dict) -> str:
        return redact(super().serialize(log))


@lru_cache(maxsize=1)
def lambda_logger() -> Logger:
    """JSON logger named "solarops": the solarops.* module loggers propagate into it,
    so their records gain the same correlation id. Writes to stderr, which Lambda
    ships to CloudWatch, leaving stdout to EMF metrics."""
    return Logger(service=SERVICE, stream=sys.stderr, logger_formatter=RedactingFormatter())


def entry_point(correlation_id_path: str | None = None) -> Callable:
    """Decorate a Lambda handler: X-Ray segment + JSON logs with request context.

    The incoming event is never logged (it carries the x-api-key header), and
    responses are not attached to traces (they can be large)."""
    logger = lambda_logger()

    def decorate(handler: Callable) -> Callable:
        traced = tracer.capture_lambda_handler(handler, capture_response=False)
        return logger.inject_lambda_context(
            traced, correlation_id_path=correlation_id_path, log_event=False, clear_state=True
        )

    return decorate
