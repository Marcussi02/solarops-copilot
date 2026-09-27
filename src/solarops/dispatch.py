"""AEMO dispatch outcomes: what explains low output that is not an equipment fault.

Two NEMWeb reports, because no single one is both timely and complete:

- DispatchIS (every 5 minutes, ~20 KB): regional prices. A negative price is a strong
  hint that semi-scheduled solar is being dispatched down, available in real time.
- Next_Day_Dispatch (daily, ~9 MB zipped): per-unit dispatch targets (TOTALCLEARED),
  the unconstrained forecast of what each unit could have produced (UIGF) and the
  semi-dispatch cap flag. This is the ground truth for curtailment, one day late.

Scores are therefore provisional in real time ("likely curtailed" from prices) and
corrected when the next-day file lands ("curtailed", with MW lost).
"""

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from .http import get_bytes
from .nemweb import BASE_URL, NEM_TZ

PRICE_DIR = "/Reports/Current/DispatchIS_Reports/"
NEXT_DAY_DIR = "/Reports/Current/Next_Day_Dispatch/"
PRICE_RE = re.compile(r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip", re.IGNORECASE)
NEXT_DAY_RE = re.compile(r"PUBLIC_NEXT_DAY_DISPATCH_(\d{8})_\d+\.zip", re.IGNORECASE)


@dataclass(frozen=True)
class RegionPrice:
    region: str
    interval_end: datetime
    rrp: float


@dataclass(frozen=True)
class UnitDispatch:
    duid: str
    interval_end: datetime
    total_cleared: float
    availability: float | None
    uigf: float | None
    semidispatch_cap: bool


def _listing(directory: str, pattern: re.Pattern, html: str | None) -> list[str]:
    if html is None:
        html = get_bytes(BASE_URL + directory).decode("utf-8", "replace")
    return sorted({m.group(0) for m in pattern.finditer(html)})


def list_price_files(html: str | None = None) -> list[str]:
    return _listing(PRICE_DIR, PRICE_RE, html)


def list_next_day_files(html: str | None = None) -> list[str]:
    return _listing(NEXT_DAY_DIR, NEXT_DAY_RE, html)


def price_file_interval(name: str) -> datetime:
    return datetime.strptime(PRICE_RE.fullmatch(name).group(1), "%Y%m%d%H%M").replace(
        tzinfo=NEM_TZ
    )


def next_day_file_date(name: str) -> datetime:
    return datetime.strptime(NEXT_DAY_RE.fullmatch(name).group(1), "%Y%m%d").replace(
        tzinfo=NEM_TZ
    )


def download_price_file(name: str) -> bytes:
    return get_bytes(BASE_URL + PRICE_DIR + name)


def download_next_day_file(name: str) -> bytes:
    return get_bytes(BASE_URL + NEXT_DAY_DIR + name, timeout=120)


def _rows(zip_bytes: bytes, table: tuple[str, str]) -> Iterator[dict]:
    """Stream one table's data rows out of a zipped AEMO report as dicts.

    Reports interleave several tables, each introduced by its own "I" header row.
    The next-day file is over 100 MB unzipped, so it is read line by line.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for member in archive.namelist():
            if not member.upper().endswith(".CSV"):
                continue
            with archive.open(member) as raw:
                header: list[str] | None = None
                for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace")):
                    if len(row) < 3 or (row[1], row[2]) != table:
                        continue
                    if row[0] == "I":
                        header = row
                    elif row[0] == "D" and header:
                        yield dict(zip(header, row, strict=False))


def _time(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM_TZ)


def _num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_prices(zip_bytes: bytes) -> list[RegionPrice]:
    out = []
    for r in _rows(zip_bytes, ("DISPATCH", "PRICE")):
        if r.get("INTERVENTION", "0").strip() != "0":
            continue  # intervention pricing runs are not the market outcome
        rrp = _num(r.get("RRP"))
        if rrp is None:
            continue
        out.append(RegionPrice(r["REGIONID"].strip(), _time(r["SETTLEMENTDATE"]), rrp))
    return out


def parse_unit_dispatch(zip_bytes: bytes, duids: Iterable[str]) -> list[UnitDispatch]:
    """Solar units' dispatch outcomes from a next-day report (other units are skipped)."""
    wanted = set(duids)
    out = []
    for r in _rows(zip_bytes, ("DISPATCH", "UNIT_SOLUTION")):
        duid = r.get("DUID", "").strip()
        if duid not in wanted or r.get("INTERVENTION", "0").strip() != "0":
            continue
        cleared = _num(r.get("TOTALCLEARED"))
        if cleared is None:
            continue
        out.append(
            UnitDispatch(
                duid=duid,
                interval_end=_time(r["SETTLEMENTDATE"]),
                total_cleared=cleared,
                availability=_num(r.get("AVAILABILITY")),
                uigf=_num(r.get("UIGF")),
                semidispatch_cap=r.get("SEMIDISPATCHCAP", "0").strip() == "1",
            )
        )
    return out
