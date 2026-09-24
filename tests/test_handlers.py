import json

import boto3
import pytest
from moto import mock_aws

from conftest import make_scada_zip
from solarops import handlers, pipeline

ROWS = [("2026/09/25 12:00:00", "TESTSF1", 40.0)]


@pytest.fixture
def aws_env(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)


def test_ingest_handler_reports_only_failed_messages(seeded, monkeypatch):
    monkeypatch.setattr(handlers, "get_conn", lambda: seeded)
    good = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
    bad = "PUBLIC_DISPATCHSCADA_202609251205_0000000000000002.zip"
    real_process = pipeline.process_file

    def fake_process(conn, name, archive=None):
        if name == bad:
            raise RuntimeError("download failed")
        return real_process(conn, name, download=lambda n: make_scada_zip(ROWS))

    monkeypatch.setattr(pipeline, "process_file", fake_process)
    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps({"file": good})},
            {"messageId": "m2", "body": json.dumps({"file": bad})},
            {"messageId": "m3", "body": "not json"},
        ]
    }
    result = handlers.ingest_handler(event, None)
    assert result == {
        "batchItemFailures": [{"itemIdentifier": "m2"}, {"itemIdentifier": "m3"}]
    }


def test_poll_handler_queues_pending_files_in_batches(seeded, monkeypatch, aws_env):
    files = [
        f"PUBLIC_DISPATCHSCADA_20260925{h:02d}{m:02d}_00000000000{h:02d}{m:02d}.zip"
        for h in range(10, 12)
        for m in range(0, 60, 5)
    ]  # 24 files
    monkeypatch.setattr(handlers, "get_conn", lambda: seeded)
    monkeypatch.setattr(handlers.nemweb, "list_files", lambda: files)
    with mock_aws():
        queue_url = boto3.client("sqs").create_queue(QueueName="ingest")["QueueUrl"]
        monkeypatch.setenv("QUEUE_URL", queue_url)
        monkeypatch.setenv("MAX_FILES_PER_POLL", "20")

        assert handlers.poll_handler({}, None) == {"queued": 20}

        attrs = boto3.client("sqs").get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        assert attrs["Attributes"]["ApproximateNumberOfMessages"] == "20"


def test_s3_archive_uses_date_partition(monkeypatch, aws_env):
    name = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
    with mock_aws():
        boto3.client("s3").create_bucket(
            Bucket="raw", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        monkeypatch.setenv("RAW_BUCKET", "raw")
        handlers.s3_archive(name, b"zipbytes")
        key = f"raw/dispatch_scada/date=2026-09-25/{name}"
        obj = boto3.client("s3").get_object(Bucket="raw", Key=key)
        assert obj["Body"].read() == b"zipbytes"
