"""Pluggable model providers.

    LLM_PROVIDER=none     deterministic router only (default; free, offline)
    LLM_PROVIDER=bedrock  Amazon Bedrock Converse API (BEDROCK_MODEL_ID)
    LLM_PROVIDER=openai   OpenAI Chat Completions (OPENAI_MODEL, OPENAI_API_KEY[_PARAM])

A provider does two things: pick one tool call for a question, and turn the tool's
JSON result into a short answer. It only ever sees tool results, never the
database, so every number in an answer comes from a fixed, parameterised query.
"""

import json
import os
import urllib.request

from .router import RuleBasedProvider
from .types import ProviderError, ToolCall

ROUTE_PROMPT = (
    "You route questions about Australian utility-scale solar farms (AEMO NEM data) to "
    "exactly one tool. NEM regions: NSW1, QLD1, VIC1, SA1, TAS1. Default to a 24 hour "
    "window unless the question gives one. Always call a tool."
)
ANSWER_PROMPT = (
    "You are an operations analyst. Answer the question in at most four sentences using "
    "ONLY the numbers in the JSON tool result. If the result is empty, say so plainly. "
    "Performance index = actual / weather-expected output; below 0.6 means underperforming. "
    "Do not speculate about causes the data does not show."
)


def _answer_input(question: str, call: ToolCall, result: dict) -> str:
    payload = json.dumps(result, default=str)[:12000]
    return f"Question: {question}\nTool: {call.name} {json.dumps(call.args)}\nResult: {payload}"


class BedrockProvider:
    name = "bedrock"

    def __init__(self, model_id: str | None = None, client=None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime")
        return self._client

    def choose_tool(self, question: str, specs: list[dict], context: dict) -> ToolCall:
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": ROUTE_PROMPT}],
                messages=[{"role": "user", "content": [{"text": question}]}],
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": s["name"],
                                "description": s["description"],
                                "inputSchema": {"json": s["input_schema"]},
                            }
                        }
                        for s in specs
                    ],
                    "toolChoice": {"any": {}},
                },
                inferenceConfig={"maxTokens": 300, "temperature": 0},
            )
        except Exception as exc:
            raise ProviderError(f"bedrock routing failed: {exc}") from exc
        for block in resp.get("output", {}).get("message", {}).get("content", []):
            if "toolUse" in block:
                return ToolCall(block["toolUse"]["name"], block["toolUse"].get("input") or {})
        raise ProviderError("bedrock returned no tool call")

    def summarise(self, question: str, call: ToolCall, result: dict) -> str:
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": ANSWER_PROMPT}],
                messages=[
                    {"role": "user", "content": [{"text": _answer_input(question, call, result)}]}
                ],
                inferenceConfig={"maxTokens": 300, "temperature": 0},
            )
            return resp["output"]["message"]["content"][0]["text"].strip()
        except Exception as exc:
            raise ProviderError(f"bedrock answer failed: {exc}") from exc


class OpenAIProvider:
    name = "openai"
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str | None = None, api_key: str | None = None, post=None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._api_key = api_key
        self._post = post or self._http_post

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            from .. import config

            self._api_key = config.openai_api_key()
            if not self._api_key:
                raise ProviderError("OPENAI_API_KEY is not configured")
        return self._api_key

    def _http_post(self, body: dict) -> dict:
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())

    def _call(self, body: dict) -> dict:
        try:
            return self._post({"model": self.model, "temperature": 0, **body})
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"openai request failed: {exc}") from exc

    def choose_tool(self, question: str, specs: list[dict], context: dict) -> ToolCall:
        resp = self._call(
            {
                "messages": [
                    {"role": "system", "content": ROUTE_PROMPT},
                    {"role": "user", "content": question},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": s["name"],
                            "description": s["description"],
                            "parameters": s["input_schema"],
                        },
                    }
                    for s in specs
                ],
                "tool_choice": "required",
            }
        )
        try:
            fn = resp["choices"][0]["message"]["tool_calls"][0]["function"]
            return ToolCall(fn["name"], json.loads(fn.get("arguments") or "{}"))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("openai returned no usable tool call") from exc

    def summarise(self, question: str, call: ToolCall, result: dict) -> str:
        resp = self._call(
            {
                "messages": [
                    {"role": "system", "content": ANSWER_PROMPT},
                    {"role": "user", "content": _answer_input(question, call, result)},
                ],
                "max_tokens": 300,
            }
        )
        try:
            return resp["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise ProviderError("openai returned no answer") from exc


def get_provider(name: str | None = None):
    name = (name or os.environ.get("LLM_PROVIDER") or "none").lower()
    if name in ("none", "rules", ""):
        return RuleBasedProvider()
    if name == "bedrock":
        return BedrockProvider()
    if name == "openai":
        return OpenAIProvider()
    raise ValueError(f"unknown LLM_PROVIDER {name!r}")
