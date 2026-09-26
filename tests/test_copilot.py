"""Copilot: routing, provider adapters, validation guardrails and fallback."""

import json
from datetime import UTC, datetime

import pytest

from conftest import make_scada_zip
from solarops import db, evals, pipeline
from solarops.copilot import agent, router, tools
from solarops.copilot.llm import BedrockProvider, OpenAIProvider, get_provider
from solarops.copilot.types import ProviderError, ToolCall
from solarops.weather import WeatherObs

# ---------- deterministic router ----------


@pytest.mark.parametrize(
    ("text", "hours"),
    [("last 6 hours", 6), ("past 3 days", 72), ("this week", 168), ("in the last hour", 1),
     ("today", 24), ("999 hours", 168)],
)
def test_parse_hours(text, hours):
    assert router.parse_hours(text) == hours


@pytest.mark.parametrize(
    ("text", "region"),
    [("farms in nsw", "NSW1"), ("queensland output", "QLD1"), ("how is sa doing", "SA1"),
     ("south australia", "SA1"), ("salt flats", None), ("usa", None)],
)
def test_parse_region(text, region):
    assert router.parse_region(text) == region


def test_facility_match_prefers_longest_distinctive_name():
    farms = [("DARL", "Darlington Point Solar Farm"), ("DARLN", "Darlington North Solar Farm")]
    assert router.match_facility("how did darlington north do", farms) == "DARLN"
    assert router.match_facility("how is the weather", farms) is None


def test_golden_set_routes_perfectly_without_a_model():
    """CI gate: the offline router must answer every golden question correctly."""
    report = evals.run(router.RuleBasedProvider(), evals.load())
    assert report["failures"] == []
    assert report["accuracy"] == 1.0


def test_eval_check_catches_dropped_region():
    expect = {"tool": "underperformers", "args": {"region": "QLD1"}}
    assert evals.check("underperformers", {"region": None}, expect, []) != []
    assert evals.check("underperformers", {"region": "QLD1"}, expect, []) == []


# ---------- tool validation guardrails ----------


def test_tool_specs_expose_bounded_json_schema():
    specs = {s["name"]: s for s in tools.tool_specs()}
    assert set(specs) == {"underperformers", "facility_performance", "fleet_summary",
                          "find_facilities"}
    hours = specs["fleet_summary"]["input_schema"]["properties"]["hours"]
    assert hours["maximum"] == 168 and hours["minimum"] == 1


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("drop_table", {}),
        ("fleet_summary", {"hours": 10_000}),
        ("fleet_summary", {"region": "TEXAS"}),
        ("underperformers", {"sql": "select 1"}),  # unknown fields are rejected
        ("facility_performance", {"facility": "x"}),
    ],
)
def test_invalid_tool_calls_are_rejected(name, args):
    with pytest.raises(tools.ToolError):
        tools.validate(name, args)


# ---------- provider adapters (no network) ----------


class FakeBedrock:
    def __init__(self, route_input, answer="Grounded answer."):
        self.route_input = route_input
        self.answer = answer
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if "toolConfig" in kwargs:
            tool = {"name": "fleet_summary", "input": self.route_input}
            return {"output": {"message": {"content": [{"toolUse": tool}]}}}
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


def test_bedrock_provider_uses_converse_tool_use():
    fake = FakeBedrock({"hours": 6, "region": "QLD1"})
    provider = BedrockProvider(model_id="test-model", client=fake)
    call = provider.choose_tool("qld last 6h", tools.tool_specs(), {})
    assert call == ToolCall("fleet_summary", {"hours": 6, "region": "QLD1"})
    config = fake.calls[0]["toolConfig"]
    assert {t["toolSpec"]["name"] for t in config["tools"]} >= {"fleet_summary"}
    assert fake.calls[0]["inferenceConfig"]["temperature"] == 0


def test_openai_provider_parses_tool_call():
    def fake_post(body):
        assert body["tool_choice"] == "required"
        fn = {"name": "underperformers", "arguments": json.dumps({"region": "NSW1"})}
        return {"choices": [{"message": {"tool_calls": [{"function": fn}]}}]}

    provider = OpenAIProvider(model="test", api_key="k", post=fake_post)
    call = provider.choose_tool("nsw issues", tools.tool_specs(), {})
    assert call == ToolCall("underperformers", {"region": "NSW1"})


def test_openai_provider_wraps_errors():
    def broken(body):
        raise TimeoutError("slow")

    with pytest.raises(ProviderError):
        OpenAIProvider(api_key="k", post=broken).choose_tool("q", [], {})


def test_get_provider_defaults_to_rules(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert get_provider().name == "rules"
    with pytest.raises(ValueError):
        get_provider("gpt-17")


# ---------- end to end against Postgres ----------

FILE = "PUBLIC_DISPATCHSCADA_202609251200_0000000000000001.zip"
ROWS = [
    ("2026/09/25 12:00:00", "TESTSF1", 40.0),
    ("2026/09/25 12:00:00", "TESTSF2", 20.0),
    ("2026/09/25 12:00:00", "OTHERSF1", 150.0),
]


@pytest.fixture
def loaded(seeded):
    pipeline.process_file(seeded, FILE, download=lambda n: make_scada_zip(ROWS))
    at = datetime(2026, 9, 25, 1, 45, tzinfo=UTC)
    db.upsert_weather(
        seeded,
        [WeatherObs("TESTSF", at, 1000.0, 25.0, 0.0), WeatherObs("OTHERSF", at, 1000.0, 25.0, 0.0)],
    )
    return seeded


def test_ask_with_rules_is_grounded_in_query_results(loaded):
    question = "Which farms are underperforming in NSW?"
    answer = agent.ask(loaded, question, router.RuleBasedProvider())
    assert answer.tool == "underperformers" and answer.args["region"] == "NSW1"
    assert [f["facility_code"] for f in answer.data["farms"]] == ["TESTSF"]
    assert "Test Solar Farm" in answer.answer and "50%" in answer.answer
    assert answer.provider == "rules" and answer.fallback_reason is None


def test_ask_resolves_a_named_farm(loaded):
    answer = agent.ask(loaded, "How did Other Solar Farm do in the last 2 hours?",
                       router.RuleBasedProvider())
    assert answer.tool == "facility_performance"
    assert answer.data["facility"]["facility_code"] == "OTHERSF"
    assert answer.data["summary"]["energy_mwh"] == 12.5  # 150 MW for one 5-minute interval


class BadModel:
    """A model that proposes an out-of-bounds call, as real models sometimes do."""

    name = "bad-model"

    def choose_tool(self, question, specs, context):
        return ToolCall("fleet_summary", {"hours": 100_000})

    def summarise(self, question, call, result):
        raise AssertionError("must not be used after a rejected proposal")


class DownModel:
    name = "down-model"

    def choose_tool(self, question, specs, context):
        raise ProviderError("503 from provider")


def test_invalid_model_proposal_falls_back_to_rules(loaded):
    answer = agent.ask(loaded, "fleet summary for NSW", BadModel())
    assert answer.provider == "rules" and "invalid arguments" in answer.fallback_reason
    assert answer.tool == "fleet_summary" and answer.args["region"] == "NSW1"


def test_provider_outage_falls_back_to_rules(loaded):
    answer = agent.ask(loaded, "list farms in QLD", DownModel())
    assert answer.provider == "rules" and "503" in answer.fallback_reason
    assert answer.data["count"] == 1


def test_model_summary_is_used_when_healthy(loaded):
    fake = FakeBedrock({"hours": 24, "region": None}, answer="Fleet produced 17.5 MWh.")
    answer = agent.ask(loaded, "fleet total", BedrockProvider(model_id="m", client=fake))
    assert answer.provider == "bedrock" and answer.answer == "Fleet produced 17.5 MWh."
    assert sum(r["energy_mwh"] for r in answer.data["regions"]) == 17.5


def test_prune_removes_only_old_rows(seeded):
    seeded.execute(
        """INSERT INTO scada_readings VALUES
           ('TESTSF1', now() - interval '40 days', 1.0),
           ('TESTSF1', now() - interval '1 day', 2.0)"""
    )
    seeded.commit()
    assert db.prune(seeded, days=30)["scada_readings"] == 1
    left = [r[0] for r in seeded.execute("SELECT mw FROM scada_readings").fetchall()]
    seeded.commit()
    assert left == [2.0]
