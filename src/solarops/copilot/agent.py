"""One question in, one grounded answer out.

    question -> provider.choose_tool -> validate args -> fixed query -> provider.summarise

If the provider errors, times out, or proposes an invalid tool call, the
deterministic router takes over for that step. The response records which
provider answered and why a fallback happened, so failures stay observable.

Knowledge answers (search_docs) must cite the passages they use as [n]. A model
answer with no citation, or one citing a passage it was not given, is rejected
and replaced by the extractive answer, so every claim traces to a source.
Diagnostic questions about live data ("why is X low?") also get related guidance
from the knowledge base attached as sources.
"""

import logging
import re
import time
from dataclasses import asdict, dataclass, field

import psycopg

from .. import queries
from . import tools
from .llm import get_provider
from .router import DIAGNOSTIC, RuleBasedProvider
from .types import ProviderError, ToolCall

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500


@dataclass
class Answer:
    question: str
    answer: str
    tool: str
    args: dict
    data: dict
    provider: str
    fallback_reason: str | None
    latency_ms: int
    sources: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _sources(result: dict) -> list[dict]:
    return [
        {"n": r["n"], "id": r["id"], "title": r["title"], "section": r["section"]}
        for r in result.get("results", [])
    ]


def check_citations(text: str, result: dict) -> None:
    """Raise if a knowledge answer cites nothing, or cites a passage it was not given."""
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    given = {r["n"] for r in result.get("results", [])}
    if given and not cited:
        raise ProviderError("answer cites no passages")
    if cited - given:
        raise ProviderError(f"answer cites unknown passages {sorted(cited - given)}")


def ask(conn: psycopg.Connection, question: str, provider=None) -> Answer:
    started = time.perf_counter()
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    provider = provider or get_provider()
    rules = RuleBasedProvider()
    context = {"facilities": queries.facility_names(conn)}
    specs = tools.tool_specs()
    fallback_reason = None

    try:
        call = provider.choose_tool(question, specs, context)
        tools.validate(call.name, call.args)
        answerer = provider
    except Exception as exc:  # provider failure or invalid proposal: use the router
        fallback_reason = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("copilot routing fell back to rules: %s", fallback_reason)
        call = rules.choose_tool(question, specs, context)
        answerer = rules

    parsed = tools.validate(call.name, call.args)
    call = ToolCall(call.name, parsed.model_dump())
    result = tools.run_tool(conn, call.name, call.args)

    try:
        text = answerer.summarise(question, call, result)
        if call.name == "search_docs":
            check_citations(text, result)
    except Exception as exc:
        fallback_reason = fallback_reason or f"{type(exc).__name__}: {exc}"[:300]
        answerer = rules
        text = rules.summarise(question, call, result)

    sources = _sources(result) if call.name == "search_docs" else []
    if call.name != "search_docs" and DIAGNOSTIC.search(question.lower()):
        guidance = tools.run_tool(conn, "search_docs", {"query": question, "k": 3})
        sources = _sources(guidance)
        if sources:
            text += " Related guidance: " + "; ".join(
                f"{s['title']} - {s['section']} [{s['n']}]" for s in sources
            ) + "."

    return Answer(
        question=question,
        answer=text,
        tool=call.name,
        args=call.args,
        data=result,
        provider=answerer.name,
        fallback_reason=fallback_reason,
        latency_ms=round((time.perf_counter() - started) * 1000),
        sources=sources,
    )
