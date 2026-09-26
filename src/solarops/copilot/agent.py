"""One question in, one grounded answer out.

    question -> provider.choose_tool -> validate args -> fixed query -> provider.summarise

If the provider errors, times out, or proposes an invalid tool call, the
deterministic router takes over for that step. The response records which
provider answered and why a fallback happened, so failures stay observable.
"""

import logging
import time
from dataclasses import asdict, dataclass

import psycopg

from .. import queries
from . import tools
from .llm import get_provider
from .router import RuleBasedProvider
from .types import ToolCall

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

    def to_dict(self) -> dict:
        return asdict(self)


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
    except Exception as exc:
        fallback_reason = fallback_reason or f"{type(exc).__name__}: {exc}"[:300]
        answerer = rules
        text = rules.summarise(question, call, result)

    return Answer(
        question=question,
        answer=text,
        tool=call.name,
        args=call.args,
        data=result,
        provider=answerer.name,
        fallback_reason=fallback_reason,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
