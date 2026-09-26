import io
import os
import zipfile

import pytest

from solarops import db
from solarops.registry import SolarUnit

HEADER = "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED"


def make_scada_zip(rows: list[tuple[str, str, float]], extra_lines: list[str] = ()) -> bytes:
    """Build a zipped AEMO-format report. rows = (settlement 'YYYY/MM/DD HH:MM:SS', duid, mw)."""
    lines = [
        "C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/09/25,12:00:05,0,DISPATCHSCADA,0",
        HEADER,
    ]
    lines += [f'D,DISPATCH,UNIT_SCADA,1,"{ts}",{duid},{mw},"{ts}"' for ts, duid, mw in rows]
    lines += list(extra_lines)
    lines.append('C,"END OF REPORT",4')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.CSV", "\n".join(lines))
    return buf.getvalue()


UNITS = [
    SolarUnit("TESTSF1", "TESTSF", "Test Solar Farm", "NSW1", -33.0, 147.0, 100.0),
    SolarUnit("TESTSF2", "TESTSF", "Test Solar Farm", "NSW1", -33.0, 147.0, 50.0),
    SolarUnit("OTHERSF1", "OTHERSF", "Other Solar Farm", "QLD1", -27.0, 152.0, 200.0),
]


@pytest.fixture
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    connection = db.connect(url)
    with connection.transaction():
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn):
    db.upsert_units(conn, UNITS)
    return conn
