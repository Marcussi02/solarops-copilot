"""MCP server: the same bounded tools over the Model Context Protocol."""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from solarops import mcp_server  # noqa: E402
from solarops.copilot import tools  # noqa: E402


def _run(connect, fn):
    async def go():
        server = mcp_server.build_server(connect=connect)
        async with create_connected_server_and_client_session(server) as client:
            return await fn(client)

    return asyncio.run(go())


def _no_db():
    raise AssertionError("this call must not need the database")


def test_lists_the_copilot_tool_catalogue():
    listed = _run(_no_db, lambda c: c.list_tools())
    assert {t.name for t in listed.tools} == set(tools.TOOLS)
    hours = next(t for t in listed.tools if t.name == "fleet_summary").inputSchema
    assert hours["properties"]["hours"]["maximum"] == 168


def test_knowledge_search_works_without_a_database():
    res = _run(_no_db, lambda c: c.call_tool("search_docs", {"query": "What is a DUID?"}))
    body = json.loads(res.content[0].text)
    assert not res.isError and body["results"][0]["id"] == "nem-data#duids-and-facilities"


@pytest.mark.parametrize(
    ("name", "args"),
    [("fleet_summary", {"hours": 10_000}), ("underperformers", {"sql": "select 1"}),
     ("drop_table", {})],
)
def test_invalid_calls_are_rejected_not_executed(name, args):
    """Rejected by the JSON schema or the pydantic bounds, before any query runs."""
    res = _run(_no_db, lambda c: c.call_tool(name, args))
    assert res.isError


def test_data_tools_query_postgres(seeded):
    res = _run(lambda: seeded, lambda c: c.call_tool("find_facilities", {"region": "NSW1"}))
    body = json.loads(res.content[0].text)
    assert not res.isError and body["region"] == "NSW1" and "facilities" in body
