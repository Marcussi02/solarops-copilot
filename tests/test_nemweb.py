from datetime import datetime

import pytest

from conftest import make_scada_zip
from solarops import nemweb


def test_parse_scada_reads_unit_rows_in_nem_time():
    data = make_scada_zip(
        [("2026/09/25 12:00:00", "TESTSF1", 87.5), ("2026/09/25 12:00:00", "COAL1", 650)]
    )
    readings = nemweb.parse_scada(data)

    assert [r.duid for r in readings] == ["TESTSF1", "COAL1"]
    first = readings[0]
    assert first.mw == 87.5
    assert first.interval_end == datetime(2026, 9, 25, 12, 0, tzinfo=nemweb.NEM_TZ)
    assert first.interval_end.utcoffset().total_seconds() == 10 * 3600


def test_parse_scada_skips_other_tables_and_bad_rows():
    extra = [
        "I,DISPATCH,OTHER_TABLE,1,A,B",
        "D,DISPATCH,OTHER_TABLE,1,x,y",
        'D,DISPATCH,UNIT_SCADA,1,"not a date",BAD1,1,"x"',
        'D,DISPATCH,UNIT_SCADA,1,"2026/09/25 12:00:00",BAD2,notanumber,"x"',
    ]
    readings = nemweb.parse_scada(make_scada_zip([("2026/09/25 12:00:00", "OK1", 1)], extra))
    assert [r.duid for r in readings] == ["OK1"]


def test_list_files_dedupes_and_sorts():
    base = "/Reports/CURRENT/Dispatch_SCADA/"
    html = f"""
    <a HREF="{base}PUBLIC_DISPATCHSCADA_202609251205_0000000000000002.zip">x</a>
    <a HREF="{base}PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip">y</a>
    PUBLIC_DISPATCHSCADA_202609251205_0000000000000002.zip
    """
    assert nemweb.list_files(html) == [
        "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip",
        "PUBLIC_DISPATCHSCADA_202609251205_0000000000000002.zip",
    ]


def test_file_interval():
    ts = nemweb.file_interval("PUBLIC_DISPATCHSCADA_202609251205_0000000000000002.zip")
    assert ts == datetime(2026, 9, 25, 12, 5, tzinfo=nemweb.NEM_TZ)
    with pytest.raises(ValueError):
        nemweb.file_interval("something_else.zip")
