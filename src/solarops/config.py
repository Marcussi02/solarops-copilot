"""Runtime configuration from environment variables."""

import os
from functools import lru_cache


def _secret(name: str, required: bool = True) -> str | None:
    """Read NAME from the environment (local) or from SSM via NAME_PARAM (AWS)."""
    value = os.environ.get(name)
    if value:
        return value
    param = os.environ.get(f"{name}_PARAM")
    if not param:
        if required:
            raise RuntimeError(f"set {name} or {name}_PARAM")
        return None
    import boto3

    resp = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)
    return resp["Parameter"]["Value"]


@lru_cache(maxsize=1)
def database_url() -> str:
    return _secret("DATABASE_URL")


@lru_cache(maxsize=1)
def openelectricity_api_key() -> str | None:
    return _secret("OPENELECTRICITY_API_KEY", required=False)


def raw_bucket() -> str | None:
    return os.environ.get("RAW_BUCKET")


def queue_url() -> str | None:
    return os.environ.get("QUEUE_URL")


def max_files_per_poll() -> int:
    return int(os.environ.get("MAX_FILES_PER_POLL", "36"))
