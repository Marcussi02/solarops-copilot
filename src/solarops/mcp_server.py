"""Model Context Protocol server: the copilot's tool catalogue for any MCP client.

    python -m solarops.mcp_server        # stdio transport, reads DATABASE_URL

MCP clients are language models themselves, so this exposes the same bounded tools
the copilot uses (not the copilot) and lets the client do the reasoning. There is
one tool registry behind three interfaces, REST, copilot and MCP, so validation,
limits and the read-only session are identical everywhere.
"""

import json
import logging

import psycopg
from mcp import types
from mcp.server.lowlevel import Server

from . import db
from .copilot import tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Live performance of Australian utility-scale solar farms from AEMO NEM data. "
    "Performance index = actual / weather-expected output; below 0.6 is flagged. "
    "Use search_docs for definitions, causes and troubleshooting guidance, and cite it."
)


def build_server(connect=db.connect_read_only) -> Server:
    server = Server("solarops", instructions=INSTRUCTIONS)
    state: dict[str, psycopg.Connection | None] = {"conn": None}

    def conn() -> psycopg.Connection:
        c = state["conn"]
        if c is None or c.closed or c.broken:
            c = state["conn"] = connect()
        return c

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=s["name"], description=s["description"], inputSchema=s["input_schema"])
            for s in tools.tool_specs()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        try:
            if name == "search_docs":  # knowledge base only, no database needed
                result = tools.run_tool(None, name, arguments)
            else:
                c = conn()
                try:
                    result = tools.run_tool(c, name, arguments)
                finally:
                    c.rollback()  # end the read-only transaction either way
        except tools.ToolError as exc:
            raise ValueError(str(exc)) from exc  # reported to the client as isError
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    logging.basicConfig(level=logging.WARNING)
    server = build_server()

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
