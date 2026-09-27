"""Deterministic question router and answer templates.

This is the copilot's floor: it needs no model, no API key and no network, so CI,
local demos and LLM outages all still get a correct, grounded answer. LLM
providers sit on top of it and fall back to it whenever they fail.
"""

import re
from datetime import datetime, timedelta

from ..rag import get_retriever
from ..rag.lexical import tokenize
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
_CURTAIL = re.compile(
    r"curtail|dispatched down|constrained|negative pric|semi-dispatch|\bcapped\b"
)
_LIST = re.compile(
    r"\blist\b|which (solar )?farms|what (solar )?farms|how many (solar )?farms|"
    r"show (me )?(the |all )?(solar )?farms|\bfind\b"
)
# Knowledge questions: definitions, causes, how things work, what to do.
_DOCS = re.compile(
    r"\bhow (do|does|should|can|to|is|are)\b|\bwhat (is|are|does|do|causes?|should)\b|"
    r"\bwhat's (a|an|the)\b|\bwhy\b|\bexplain|\bmean(s|ing)?\b|\bdefin|troubleshoot|"
    r"checklist|runbook|procedure|\bsteps?\b|\bguid(e|ance)\b|\bwhen (is|should|do)\b|"
    r"\btell me about\b|\bdifference between\b|\bis (it|that|this) (a|normal)\b"
)
# A question about a specific time window wants live numbers, even if it says "why".
_LIVE = re.compile(
    r"\b(right now|now|currently|today|tonight|at the moment|latest|"
    r"this (morning|afternoon|evening|week)|(last|past) (\d+|hour|day|week))\b"
)
# Words that ask for numbers about the fleet rather than for an explanation.
_FLEET = re.compile(
    r"\b(energy|mwh|generat\w*|total|fleet|summary|compare|market|happened|overall|doing)\b|"
    r"\d+\s*(hours?|hrs?|days?)\b|\bweek\b"
)
_DATA_ASK = re.compile(r"\b(which|any|show|list|worst|farms)\b")
# Diagnostic phrasing: answer with data, then attach relevant guidance.
DIAGNOSTIC = re.compile(
    r"\bwhy\b|\bcause|\breason|what should i (check|do)|"
    r"how (do|can|should) i (fix|check|investigate)|troubleshoot"
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
        if _DOCS.search(q) and not _LIVE.search(q):
            return ToolCall("search_docs", {"query": question, "k": 4})
        if _CURTAIL.search(q):
            return ToolCall("curtailed_farms", {"region": region})
        if _UNDER.search(q) and (region or _LIVE.search(q) or _DATA_ASK.search(q)):
            args: dict = {"region": region}
            if (threshold := parse_threshold(q)) is not None:
                args["threshold"] = threshold
            return ToolCall("underperformers", args)
        if _LIST.search(q):
            return ToolCall("find_facilities", {"region": region})
        if not (region or _LIVE.search(q) or _FLEET.search(q)) and get_retriever().search(q, 1):
            # No sign of a data question, and the knowledge base has something on it.
            return ToolCall("search_docs", {"query": question, "k": 4})
        return ToolCall("fleet_summary", {"hours": parse_hours(q), "region": region})

    def summarise(self, question: str, call: ToolCall, result: dict) -> str:
        return summarise(call, result)


def _nem_time(iso: str | None) -> str:
    """'2026-09-26T07:30:00+00:00' -> '17:30 AEST' (NEM time is fixed UTC+10)."""
    if not iso:
        return "the latest interval"
    return (datetime.fromisoformat(iso) + timedelta(hours=10)).strftime("%H:%M AEST")


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _best_sentences(text: str, terms: set[str], n: int = 2) -> list[str]:
    """The n sentences sharing the rarest query terms, kept in document order."""
    idf = get_retriever().bm25.idf
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    overlap = [sum(idf.get(t, 0.0) for t in terms & set(tokenize(s))) for s in sentences]
    best = sorted(range(len(sentences)), key=lambda i: (-overlap[i], i))[:n]
    return [sentences[i] for i in sorted(best)]


def summarise_docs(result: dict, max_sources: int = 2) -> str:
    """Extractive answer: the most relevant sentences of the top passages, cited [n]."""
    results = result.get("results", [])
    if not results:
        return "I couldn't find anything about that in the knowledge base."
    terms = set(tokenize(result.get("query", ""), expand=True))
    top = results[0]["score"] or 1.0
    parts = []
    for r in results[:max_sources]:
        if r is not results[0] and r["score"] < 0.5 * top:
            break  # a much weaker match adds noise, not evidence
        n = 3 if r is results[0] else 2
        parts.append(" ".join(_best_sentences(r["text"], terms, n)) + f" [{r['n']}]")
    return " ".join(parts)


def _excluded(result: dict) -> str:
    n = result.get("curtailed_excluded") or 0
    return f" {n} curtailed farm(s) were excluded as not faults." if n else ""


def summarise(call: ToolCall, result: dict) -> str:
    if call.name == "search_docs":
        return summarise_docs(result)
    if call.name == "underperformers":
        farms = result.get("farms", [])
        where = f" in {result['region']}" if result.get("region") else ""
        if not farms:
            return (
                f"No farms{where} are below {_pct(result.get('threshold'))} of expected output "
                f"at the latest daylight interval.{_excluded(result)}"
            )
        top = "; ".join(
            f"{f['facility_name']} ({f['region']}) at {_pct(f['performance_index'])}"
            f" - {f['actual_mw']} MW vs {f['expected_mw']} MW expected"
            for f in farms[:5]
        )
        at = _nem_time(farms[0].get("interval_end"))
        return (
            f"At {at}, {len(farms)} farm(s){where} were below "
            f"{_pct(result['threshold'])} of expected: {top}.{_excluded(result)}"
        )
    if call.name == "curtailed_farms":
        farms = result.get("farms", [])
        where = f" in {result['region']}" if result.get("region") else ""
        if not farms:
            return f"No farms{where} were curtailed at the latest daylight interval."
        parts = []
        for f in farms[:5]:
            if f["status"] == "curtailed":
                why = f"{f['curtailed_mw']} MW held back by a dispatch cap"
            else:
                why = f"price ${f['price_per_mwh']}/MWh, likely curtailed (provisional)"
            parts.append(f"{f['facility_name']} ({f['region']}) at {f['actual_mw']} MW, {why}")
        at = _nem_time(farms[0].get("interval_end"))
        return (
            f"At {at}, {len(farms)} farm(s){where} were producing below the weather "
            f"because of curtailment, not faults: {'; '.join(parts)}."
        )
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
            + (
                f" {s['curtailed_mwh']} MWh was curtailed by AEMO dispatch caps."
                if s.get("curtailed_mwh")
                else ""
            )
        )
    if call.name == "fleet_summary":
        regions = result.get("regions", [])
        if not regions:
            return f"No readings in the last {result['hours']} hours."
        total = sum(r["energy_mwh"] or 0 for r in regions)
        parts = "; ".join(
            f"{r['region']} {r['energy_mwh']} MWh from {r['facilities']} farms "
            f"(avg index {_pct(r['avg_performance_index'])}, "
            f"{r['underperforming_latest']} underperforming and "
            f"{r.get('curtailed_latest', 0)} curtailed at the last daylight interval)"
            for r in regions
        )
        return f"Last {result['hours']} h: {total:.1f} MWh in total. {parts}."
    if call.name == "find_facilities":
        where = f" in {result['region']}" if result.get("region") else ""
        names = ", ".join(f["facility_name"] for f in result.get("facilities", [])[:10])
        more = " ..." if result.get("count", 0) > 10 else ""
        return f"{result.get('count', 0)} solar farms{where}: {names}{more}"
    return "Done."
