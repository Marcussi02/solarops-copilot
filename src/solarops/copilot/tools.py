"""The copilot's tool catalogue: the only actions a model may take.

Each tool has a pydantic argument model with hard bounds. Arguments proposed by a
model are validated here before any query runs; anything invalid raises
``ToolError`` and the agent falls back to the deterministic router.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import queries

Region = Literal["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]


class ToolError(ValueError):
    """Unknown tool, or arguments that fail validation."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnderperformersArgs(_Args):
    threshold: float = Field(
        queries.DEFAULT_THRESHOLD,
        ge=0.05,
        le=1.5,
        description="Flag farms whose actual/expected output ratio is below this (0.6 = 60%)",
    )
    limit: int = Field(10, ge=1, le=25, description="Maximum number of farms to return")
    region: Region | None = Field(None, description="NEM region code, or null for all regions")


class FacilityArgs(_Args):
    facility: str = Field(
        min_length=2, max_length=80, description="Facility code or part of the farm's name"
    )
    hours: int = Field(24, ge=1, le=168, description="Look-back window in hours")


class FleetArgs(_Args):
    hours: int = Field(24, ge=1, le=168, description="Look-back window in hours")
    region: Region | None = Field(None, description="NEM region code, or null for all regions")


class FindArgs(_Args):
    region: Region | None = Field(None, description="NEM region code, or null for all regions")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    run: Callable[[psycopg.Connection, _Args], dict]


def _underperformers(conn, a: UnderperformersArgs) -> dict:
    rows = queries.underperformers(conn, a.threshold, a.limit, a.region)
    return {"threshold": a.threshold, "region": a.region, "farms": rows}


def _facility(conn, a: FacilityArgs) -> dict:
    match = queries.find_facility(conn, a.facility)
    if match is None:
        return {"facility": None, "searched_for": a.facility}
    report = queries.facility_report(conn, match["facility_code"], a.hours)
    return {"facility": match, "hours": a.hours, **report}


def _fleet(conn, a: FleetArgs) -> dict:
    regions = queries.fleet_summary(conn, a.hours, a.region)
    return {"hours": a.hours, "region": a.region, "regions": regions}


def _find(conn, a: FindArgs) -> dict:
    rows = queries.facilities(conn, a.region)
    return {"region": a.region, "count": len(rows), "facilities": rows[:50]}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "underperformers",
            "Solar farms producing well below what current irradiance says they should, "
            "at the latest 5-minute interval. Use for faults, issues, low output, worst farms.",
            UnderperformersArgs,
            _underperformers,
        ),
        Tool(
            "facility_performance",
            "Energy, peak output and performance index for ONE named solar farm over a window.",
            FacilityArgs,
            _facility,
        ),
        Tool(
            "fleet_summary",
            "Energy (MWh) and average performance per NEM region over a window. Use for "
            "totals, overall generation, and comparing regions.",
            FleetArgs,
            _fleet,
        ),
        Tool(
            "find_facilities",
            "List solar farms (name, region, capacity), optionally for one region.",
            FindArgs,
            _find,
        ),
    )
}


def tool_specs() -> list[dict]:
    """JSON-schema descriptions in the shape most tool-calling APIs expect."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.args_model.model_json_schema(),
        }
        for t in TOOLS.values()
    ]


def validate(name: str, args: dict | None) -> BaseModel:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        return tool.args_model.model_validate(args or {})
    except ValidationError as exc:
        raise ToolError(f"invalid arguments for {name}: {exc.errors(include_url=False)}") from exc


def run_tool(conn: psycopg.Connection, name: str, args: dict | None) -> dict:
    parsed = validate(name, args)
    return jsonable(TOOLS[name].run(conn, parsed))


def jsonable(value):
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
