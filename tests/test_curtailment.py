"""Curtailment-aware scoring: dispatch parsing, the v3 view's status, and the copilot."""

import io
import zipfile
from datetime import UTC, datetime

import pytest

from conftest import make_scada_zip
from solarops import db, dispatch, pipeline, queries
from solarops.copilot import agent, router
from solarops.weather import WeatherObs

SCADA = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
PRICE = "PUBLIC_DISPATCHIS_202609251200_0000000000000002.zip"
NEXT_DAY = "PUBLIC_NEXT_DAY_DISPATCH_20260925_0000000000000003.zip"
TS = "2026/09/25 12:00:00"  # NEM time == 02:00 UTC


def _zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("REPORT.CSV", "\n".join(lines))
    return buf.getvalue()


def price_zip(prices: dict[str, float], intervention: str = "0") -> bytes:
    lines = ["I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP"]
    lines += [f'D,DISPATCH,PRICE,5,"{TS}",1,{r},1,{intervention},{p}' for r, p in prices.items()]
    lines.append("I,DISPATCH,REGIONSUM,1,SETTLEMENTDATE,REGIONID")  # other tables are ignored
    lines.append(f'D,DISPATCH,REGIONSUM,1,"{TS}",NSW1')
    return _zip(lines)


def unit_zip(rows: list[tuple]) -> bytes:
    """rows = (duid, total_cleared, availability, uigf, cap)."""
    lines = [
        "I,DISPATCH,UNIT_SOLUTION,5,SETTLEMENTDATE,DUID,INTERVENTION,DISPATCHMODE,INITIALMW,"
        "TOTALCLEARED,AVAILABILITY,SEMIDISPATCHCAP,UIGF"
    ]
    lines += [
        f'D,DISPATCH,UNIT_SOLUTION,5,"{TS}",{d},0,0,0,{tc},{av},{int(cap)},{ug}'
        for d, tc, av, ug, cap in rows
    ]
    lines.append(f'D,DISPATCH,UNIT_SOLUTION,5,"{TS}",COALPS1,0,0,0,500,500,0,')  # not solar
    return _zip(lines)


# ---------- parsing ----------


def test_parse_prices_skips_intervention_runs():
    assert [p.rrp for p in dispatch.parse_prices(price_zip({"NSW1": -40.5}))] == [-40.5]
    assert dispatch.parse_prices(price_zip({"NSW1": 10}, intervention="1")) == []


def test_parse_unit_dispatch_keeps_solar_units_only():
    rows = dispatch.parse_unit_dispatch(unit_zip([("TESTSF1", 20, 90, 90, True)]), {"TESTSF1"})
    assert len(rows) == 1 and rows[0].semidispatch_cap and rows[0].uigf == 90
    assert rows[0].interval_end == datetime(2026, 9, 25, 2, 0, tzinfo=UTC)


def test_file_names_parse():
    assert dispatch.price_file_interval(PRICE) == datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    assert dispatch.next_day_file_date(NEXT_DAY).date().isoformat() == "2026-09-25"
    assert dispatch.list_price_files(f'<a href="x/{PRICE}">{PRICE}</a>') == [PRICE]


# ---------- scoring (real Postgres) ----------


@pytest.fixture
def low_output(seeded):
    """Both farms produce 20% of expectation in full sun at the same interval."""
    rows = [(TS, "TESTSF1", 16.0), (TS, "TESTSF2", 8.0), (TS, "OTHERSF1", 32.0)]
    pipeline.process_file(seeded, SCADA, download=lambda n: make_scada_zip(rows))
    at = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [WeatherObs("TESTSF", at, 1000.0, 25.0, 0.0), WeatherObs("OTHERSF", at, 1000.0, 25.0, 0.0)],
    )
    return seeded


def _status(conn) -> dict:
    rows = conn.execute("SELECT facility_code, status FROM facility_performance").fetchall()
    conn.commit()
    return dict(rows)


def test_without_dispatch_data_low_output_is_underperforming(low_output):
    assert _status(low_output) == {"TESTSF": "underperforming", "OTHERSF": "underperforming"}


def test_negative_price_marks_likely_curtailed_in_real_time(low_output):
    pipeline.sync_prices(
        low_output, [PRICE], download=lambda n: price_zip({"NSW1": -35.0, "QLD1": 60.0})
    )
    assert _status(low_output) == {"TESTSF": "likely_curtailed", "OTHERSF": "underperforming"}
    assert [f["facility_code"] for f in queries.underperformers(low_output)] == ["OTHERSF"]


def test_next_day_dispatch_confirms_or_overrules(low_output):
    pipeline.sync_prices(low_output, [PRICE], download=lambda n: price_zip({"NSW1": -35.0}))
    units = [
        ("TESTSF1", 16, 80, 80, False),  # not capped: the negative-price hint is overruled
        ("TESTSF2", 8, 40, 40, False),
        ("OTHERSF1", 32, 160, 160, True),  # capped at 32 MW while 160 MW was available
    ]
    loaded = pipeline.sync_unit_dispatch(low_output, [NEXT_DAY], download=lambda n: unit_zip(units))
    assert loaded == {NEXT_DAY: 3}
    assert _status(low_output) == {"TESTSF": "underperforming", "OTHERSF": "curtailed"}
    curtailed = queries.curtailed_farms(low_output)
    assert curtailed[0]["facility_code"] == "OTHERSF" and curtailed[0]["curtailed_mw"] == 128.0


def test_dispatch_sync_is_idempotent(low_output):
    units = [("OTHERSF1", 32, 160, 160, True)]
    for _ in range(2):
        pipeline.sync_unit_dispatch(low_output, [NEXT_DAY], download=lambda n: unit_zip(units))
    count = low_output.execute("SELECT count(*) FROM unit_dispatch").fetchone()[0]
    low_output.commit()
    assert count == 1


def test_copilot_separates_curtailment_from_faults(low_output):
    pipeline.sync_unit_dispatch(
        low_output, [NEXT_DAY], download=lambda n: unit_zip([("OTHERSF1", 32, 160, 160, True)])
    )
    rules = router.RuleBasedProvider()
    under = agent.ask(low_output, "Which farms are underperforming right now?", rules)
    assert [f["facility_code"] for f in under.data["farms"]] == ["TESTSF"]
    assert "1 curtailed farm(s) were excluded" in under.answer
    curt = agent.ask(low_output, "Which farms are being curtailed right now?", rules)
    assert curt.tool == "curtailed_farms" and "128.0 MW held back" in curt.answer


def test_retention_prunes_dispatch_tables(low_output):
    pipeline.sync_prices(low_output, [PRICE], download=lambda n: price_zip({"NSW1": 1.0}))
    deleted = db.prune(low_output, 1)
    assert deleted["region_prices"] == 1 and "unit_dispatch" in deleted
