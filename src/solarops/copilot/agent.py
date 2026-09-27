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

Every answer publishes CopilotLatencyMs, token and cost metrics, and a
CopilotFallbacks count by reason (the exception type) whenever a fallback happens.
"""

import logging
import re
import time
from dataclasses import asdict, dataclass, field

import psycopg

from .. import queries
from ..observability import MetricUnit, emit, tracer
from . import tools
from .llm import cost_usd, get_provider
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


def _tokens(provider) -> tuple[int, int]:
    usage = getattr(provider, "usage", None)
    return (usage.input_tokens, usage.output_tokens) if usage else (0, 0)


def record_metrics(
    answer: Answer, provider, tokens_before: tuple[int, int], fallback_kind: str | None
) -> None:
    """Latency, token usage and cost of one answer (tokens are 0 for the rules router)."""
    after = _tokens(provider)
    input_tokens, output_tokens = after[0] - tokens_before[0], after[1] - tokens_before[1]
    model = getattr(provider, "model", None)
    cost = cost_usd(model, input_tokens, output_tokens) if model else 0.0
    emit("CopilotLatencyMs", answer.latency_ms, MetricUnit.Milliseconds)
    emit("LlmInputTokens", input_tokens, MetricUnit.Count)
    emit("LlmOutputTokens", output_tokens, MetricUnit.Count)
    emit("LlmCostUsd", round(cost, 8), MetricUnit.NoUnit)
    if fallback_kind:
        emit("CopilotFallbacks", 1, MetricUnit.Count, reason=fallback_kind)
    tracer.put_annotation("tool", answer.tool)
    tracer.put_annotation("provider", answer.provider)
    tracer.put_annotation("fallback", fallback_kind or "none")


@tracer.capture_method(capture_response=False)
def ask(conn: psycopg.Connection, question: str, provider=None) -> Answer:
    started = time.perf_counter()
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    provider = provider or get_provider()
    tokens_before = _tokens(provider)
    rules = RuleBasedProvider()
    context = {"facilities": queries.facility_names(conn)}
    specs = tools.tool_specs()
    fallback_reason = fallback_kind = None

    try:
        call = provider.choose_tool(question, specs, context)
        tools.validate(call.name, call.args)
        answerer = provider
    except Exception as exc:  # provider failure or invalid proposal: use the router
        fallback_reason = f"{type(exc).__name__}: {exc}"[:300]
        fallback_kind = type(exc).__name__
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
        fallback_kind = fallback_kind or type(exc).__name__
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

    answer = Answer(
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
    record_metrics(answer, provider, tokens_before, fallback_kind)
    return answer
