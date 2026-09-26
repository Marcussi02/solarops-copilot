"""Deterministic question router and answer templates.

This is the copilot's floor: it needs no model, no API key and no network, so CI,
local demos and LLM outages all still get a correct, grounded answer. LLM
providers sit on top of it and fall back to it whenever they fail.
"""

import re

from .types import ToolCall

_REGIONS = [
    (r"\bnsw\b|new south wales", "NSW1"),
    (r"\bqld\b|queensland", "QLD1"),
    (r"\bvic\b|victoria", "VIC1"),
    (r"\bsa\b|south australia", "SA1"),
    (r"\btas\b|tasmania", "TAS1"),
]
_UNDER = re.compile(
    r"underperform|under-perform|below|worst|lagging|not producing|low output|"
    r"problem|issue|fault|losing|struggling|poorly|behind"
)
_LIST = re.compile(
    r"\blist\b|which (solar )?farms|what (solar )?farms|how many (solar )?farms|"
    r"show (me )?(the |all )?(solar )?farms|\bfind\b"
)
_NAME_NOISE = re.compile(r"\b(solar|farm|park|power|station|plant|hub|project|sf|pv)\b")


def parse_hours(q: str, default: int = 24) -> int:
    if m := re.search(r"(\d+)\s*(hours?|hrs?|h)\b", q):
        return max(1, min(168, int(m.group(1))))
    if m := re.search(r"(\d+)\s*days?\b", q):
        return max(1, min(168, int(m.group(1)) * 24))
    if re.search(r"\bweek\b", q):
        return 168
    if re.search(r"\b(last|past|this) hour\b", q):
        return 1
    return default


def parse_region(q: str) -> str | None:
    for pattern, code in _REGIONS:
        if re.search(pattern, q):
            return code
    return None


def parse_threshold(q: str) -> float | None:
    if m := re.search(r"(?:below|under|less than)\s*(\d{1,3})\s*(%|percent)", q):
        return max(0.05, min(1.5, int(m.group(1)) / 100))
    return None


_GENERIC_FIRST = {
    "western", "eastern", "northern", "southern", "north", "south", "east", "west",
    "mount", "lake", "river", "new", "big", "port", "green", "sun", "hill", "valley",
}


def _name_candidates(code: str, name: str) -> set[str]:
    """'Western Downs Green Power Hub' -> {'western downs green', 'western downs', code}."""
    words = re.sub(r"\s+", " ", _NAME_NOISE.sub(" ", name.lower())).split()
    out = {code.lower(), " ".join(words)}
    if len(words) > 2:
        out.add(" ".join(words[:2]))
    if len(words) > 1 and len(words[0]) >= 6 and words[0] not in _GENERIC_FIRST:
        out.add(words[0])
    return out


def match_facility(q: str, facilities: list[tuple[str, str]]) -> str | None:
    """Return the code of the facility whose (distinctive) name appears in q."""
    best: tuple[int, str] | None = None
    for code, name in facilities:
        for candidate in _name_candidates(code, name):
            if len(candidate) >= 4 and re.search(rf"\b{re.escape(candidate)}\b", q):
                if best is None or len(candidate) > best[0]:
                    best = (len(candidate), code)
    return best[1] if best else None


class RuleBasedProvider:
    name = "rules"

    def choose_tool(self, question: str, specs: list[dict], context: dict) -> ToolCall:
        q = question.lower()
        region = parse_region(q)
        facility = match_facility(q, context.get("facilities", []))
        if facility:
            return ToolCall("facility_performance", {"facility": facility, "hours": parse_hours(q)})
        if _UNDER.search(q):
            args: dict = {"region": region}
            if (threshold := parse_threshold(q)) is not None:
                args["threshold"] = threshold
            return ToolCall("underperformers", args)
        if _LIST.search(q):
            return ToolCall("find_facilities", {"region": region})
        return ToolCall("fleet_summary", {"hours": parse_hours(q), "region": region})

    def summarise(self, question: str, call: ToolCall, result: dict) -> str:
        return summarise(call, result)


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def summarise(call: ToolCall, result: dict) -> str:
    if call.name == "underperformers":
        farms = result.get("farms", [])
        where = f" in {result['region']}" if result.get("region") else ""
        if not farms:
            return (
                f"No farms{where} are below {_pct(result.get('threshold'))} of expected output "
                "at the latest interval (or it is night, when the index is not scored)."
            )
        top = "; ".join(
            f"{f['facility_name']} ({f['region']}) at {_pct(f['performance_index'])}"
            f" - {f['actual_mw']} MW vs {f['expected_mw']} MW expected"
            for f in farms[:5]
        )
        return f"{len(farms)} farm(s){where} below {_pct(result['threshold'])} of expected: {top}."
    if call.name == "facility_performance":
        fac = result.get("facility")
        if not fac:
            return f"I couldn't find a solar farm matching '{result.get('searched_for')}'."
        s = result["summary"]
        if not s.get("intervals"):
            return f"No readings for {fac['facility_name']} in the last {result['hours']} hours."
        return (
            f"{fac['facility_name']} ({fac['region']}, {fac['capacity_mw']} MW) produced "
            f"{s['energy_mwh']} MWh in the last {result['hours']} h, peaking at {s['peak_mw']} MW. "
            f"Average performance index {_pct(s['avg_performance_index'])} "
            f"(lowest {_pct(s['min_performance_index'])})."
        )
    if call.name == "fleet_summary":
        regions = result.get("regions", [])
        if not regions:
            return f"No readings in the last {result['hours']} hours."
        total = sum(r["energy_mwh"] or 0 for r in regions)
        parts = "; ".join(
            f"{r['region']} {r['energy_mwh']} MWh from {r['facilities']} farms "
            f"(avg index {_pct(r['avg_performance_index'])}, "
            f"{r['underperforming_now']} flagged now)"
            for r in regions
        )
        return f"Last {result['hours']} h: {total:.1f} MWh in total. {parts}."
    if call.name == "find_facilities":
        where = f" in {result['region']}" if result.get("region") else ""
        names = ", ".join(f["facility_name"] for f in result.get("facilities", [])[:10])
        more = " ..." if result.get("count", 0) > 10 else ""
        return f"{result.get('count', 0)} solar farms{where}: {names}{more}"
    return "Done."
