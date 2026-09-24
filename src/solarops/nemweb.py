"""AEMO NEMWeb client: 5-minute SCADA output for every generating unit in the NEM.

Files are published every 5 minutes at
https://nemweb.com.au/Reports/Current/Dispatch_SCADA/ as zipped AEMO "CSV" reports.
Each report mixes record types: C (comment), I (column header), D (data).
"""

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .http import get_bytes

BASE_URL = "https://nemweb.com.au"
SCADA_DIR = "/Reports/Current/Dispatch_SCADA/"

# NEM time is AEST (UTC+10) all year round, with no daylight saving.
NEM_TZ = timezone(timedelta(hours=10))

FILE_RE = re.compile(r"PUBLIC_DISPATCHSCADA_(\d{12})_\d+\.zip", re.IGNORECASE)
TABLE = ("DISPATCH", "UNIT_SCADA")


@dataclass(frozen=True)
class Reading:
    duid: str
    interval_end: datetime
    mw: float


def list_files(listing_html: str | None = None) -> list[str]:
    """Return available SCADA file names, oldest first, without duplicates."""
    if listing_html is None:
        listing_html = get_bytes(BASE_URL + SCADA_DIR).decode("utf-8", "replace")
    names = {m.group(0) for m in FILE_RE.finditer(listing_html)}
    return sorted(names)


def file_interval(file_name: str) -> datetime:
    """Interval-end timestamp encoded in the file name (NEM time)."""
    match = FILE_RE.fullmatch(file_name)
    if not match:
        raise ValueError(f"not a Dispatch_SCADA file name: {file_name}")
    return datetime.strptime(match.group(1), "%Y%m%d%H%M").replace(tzinfo=NEM_TZ)


def download(file_name: str) -> bytes:
    return get_bytes(f"{BASE_URL}{SCADA_DIR}{file_name}")


def parse_scada(zip_bytes: bytes) -> list[Reading]:
    """Extract UNIT_SCADA rows from a zipped AEMO report."""
    readings: list[Reading] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for member in archive.namelist():
            if not member.upper().endswith(".CSV"):
                continue
            text = archive.read(member).decode("utf-8", "replace")
            readings.extend(_parse_report(text))
    return readings


def _parse_report(text: str) -> list[Reading]:
    header: list[str] | None = None
    out: list[Reading] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3 or tuple(row[1:3]) != TABLE:
            continue
        if row[0] == "I":
            header = row
        elif row[0] == "D" and header:
            record = dict(zip(header, row, strict=False))
            try:
                out.append(
                    Reading(
                        duid=record["DUID"].strip(),
                        interval_end=_parse_nem_time(record["SETTLEMENTDATE"]),
                        mw=float(record["SCADAVALUE"]),
                    )
                )
            except (KeyError, ValueError):
                continue  # skip malformed rows rather than failing the whole file
    return out


def _parse_nem_time(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM_TZ)
