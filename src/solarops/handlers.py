"""AWS Lambda entry points.

    EventBridge (5 min)  -> poll_handler -> SQS -> ingest_handler -> PostgreSQL
                                                      \\-> S3 raw archive
    EventBridge (daily)  -> registry_handler
    EventBridge (15 min) -> weather_handler
    EventBridge (daily)  -> retention_handler
    API Gateway          -> solarops.api.handler (read-only API + copilot)
"""

import json
import logging

import boto3
import psycopg

from . import config, db, nemweb, pipeline

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_conn: psycopg.Connection | None = None


def get_conn() -> psycopg.Connection:
    """Reuse one connection per warm Lambda container."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = db.connect(config.database_url())
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
    return {"queued": len(files)}


def ingest_handler(event, context):
    """SQS consumer. Reports partial batch failures so only failed files are retried."""
    conn = get_conn()
    failures = []
    for record in event.get("Records", []):
        try:
            file_name = json.loads(record["body"])["file"]
            pipeline.process_file(conn, file_name, archive=s3_archive)
        except Exception:
            logger.exception("failed message %s", record.get("messageId"))
            if not conn.closed:
                conn.rollback()
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def registry_handler(event, context):
    return {"units": pipeline.sync_registry(get_conn())}


def weather_handler(event, context):
    return {"observations": pipeline.sync_weather(get_conn())}


def retention_handler(event, context):
    return db.prune(get_conn(), config.retention_days())
