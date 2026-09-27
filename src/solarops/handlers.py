"""AWS Lambda entry points.

    EventBridge (5 min)  -> poll_handler -> SQS -> ingest_handler -> PostgreSQL
                                                      \\-> S3 raw archive
    EventBridge (daily)  -> registry_handler
    EventBridge (15 min) -> weather_handler
    EventBridge (6 h)    -> dispatch_handler (next-day dispatch outcomes, curtailment)
    EventBridge (daily)  -> retention_handler
    API Gateway          -> solarops.api.handler (read-only API + copilot)
"""

import json
from datetime import UTC, datetime

import boto3
import psycopg

from . import config, db, nemweb, pipeline, queries
from .observability import MetricUnit, emit, entry_point, lambda_logger

logger = lambda_logger()

_conn: psycopg.Connection | None = None


def get_conn() -> psycopg.Connection:
    """Reuse one connection per warm Lambda container."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = db.connect_configured()
        db.migrate(_conn)
    return _conn


def s3_archive(file_name: str, data: bytes) -> None:
    bucket = config.raw_bucket()
    if not bucket:
        return
    day = nemweb.file_interval(file_name).date().isoformat()
    boto3.client("s3").put_object(
        Bucket=bucket, Key=f"raw/dispatch_scada/date={day}/{file_name}", Body=data
    )


def record_freshness(conn: psycopg.Connection) -> None:
    """IngestLagMinutes (drives the staleness alarm) and CurtailedFarms, every poll."""
    latest = queries.latest_interval(conn)
    if latest is not None:
        lag = (datetime.now(UTC) - latest).total_seconds() / 60
        emit("IngestLagMinutes", round(lag, 1), MetricUnit.NoUnit)
    emit("CurtailedFarms", len(queries.curtailed_farms(conn, limit=1000)), MetricUnit.Count)


@entry_point()
def poll_handler(event, context):
    conn = get_conn()
    files = pipeline.pending_files(conn, nemweb.list_files(), config.max_files_per_poll())
    sqs = boto3.client("sqs")
    for start in range(0, len(files), 10):  # SQS batch limit
        batch = files[start : start + 10]
        sqs.send_message_batch(
            QueueUrl=config.queue_url(),
            Entries=[
                {"Id": str(i), "MessageBody": json.dumps({"file": name})}
                for i, name in enumerate(batch)
            ],
        )
    logger.info("queued %d files", len(files))
    try:  # prices are an enrichment: never let them block SCADA ingestion
        prices = pipeline.sync_prices(conn)
    except Exception:
        logger.exception("price sync failed")
        if not conn.closed:
            conn.rollback()
        prices = None
    try:  # metrics are best-effort too
        record_freshness(conn)
    except Exception:
        logger.exception("freshness metrics failed")
        if not conn.closed:
            conn.rollback()
    return {"queued": len(files), "prices": prices}


@entry_point()
def ingest_handler(event, context):
    """SQS consumer. Reports partial batch failures so only failed files are retried.
    Each message's id is the correlation id for the logs written while processing it."""
    conn = get_conn()
    failures = []
    rows = 0
    for record in event.get("Records", []):
        logger.set_correlation_id(record.get("messageId"))
        try:
            file_name = json.loads(record["body"])["file"]
            rows += pipeline.process_file(conn, file_name, archive=s3_archive).get(
                "solar_rows", 0
            )
        except Exception:
            logger.exception("failed message %s", record.get("messageId"))
            if not conn.closed:
                conn.rollback()
            failures.append({"itemIdentifier": record["messageId"]})
    logger.set_correlation_id(None)
    emit("RowsIngested", rows, MetricUnit.Count)
    return {"batchItemFailures": failures}


@entry_point()
def registry_handler(event, context):
    return {"units": pipeline.sync_registry(get_conn())}


@entry_point()
def weather_handler(event, context):
    conn = get_conn()
    current = pipeline.sync_weather(conn)
    # Re-read the last few hours too: heals gaps from missed or failed polls.
    backfilled = pipeline.backfill_weather(conn, hours=3)
    return {"observations": current, "backfilled": backfilled}


@entry_point()
def dispatch_handler(event, context):
    return {"loaded": pipeline.sync_unit_dispatch(get_conn())}


@entry_point()
def retention_handler(event, context):
    return db.prune(get_conn(), config.retention_days())
