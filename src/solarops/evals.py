"""Score a provider's tool routing against the golden question set.

    python -m solarops.evals                      # deterministic router (CI gate)
    python -m solarops.evals --provider bedrock   # score a real model (needs AWS access)

Routing is where a copilot goes wrong in practice (wrong farm, wrong window, a
region silently dropped), and it can be scored without a database, so this runs
in CI on every push. Exits non-zero below --min-accuracy.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

from .copilot import tools
from .copilot.llm import get_provider

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "evals" / "golden.json"


def load(path: Path | str = DEFAULT_PATH) -> dict:
    return json.loads(Path(path).read_text())


def check(name: str, args: dict, expect: dict, facilities: list[list[str]]) -> list[str]:
    """Errors for one proposed call (empty list = pass)."""
    if name != expect["tool"]:
        return [f"tool {name!r} != {expect['tool']!r}"]
    try:
        parsed = tools.validate(name, args).model_dump()
    except tools.ToolError as exc:
        return [str(exc)]
    errors = []
    names = {code.lower(): full.lower() for code, full in facilities}
    for key, want in expect["args"].items():
        got = parsed.get(key)
        if key == "facility":
            text = str(got).lower()
            full = names.get(want.lower(), "")
            if not (text == want.lower() or (len(text) >= 4 and text in full)):
                errors.append(f"facility {got!r} does not identify {want}")
        elif isinstance(want, float) or isinstance(got, float):
            if got is None or abs(float(got) - float(want)) > 1e-6:
                errors.append(f"{key} {got!r} != {want!r}")
        elif got != want:
            errors.append(f"{key} {got!r} != {want!r}")
    return errors


def run(provider, golden: dict) -> dict:
    specs = tools.tool_specs()
    context = {"facilities": [tuple(f) for f in golden["facilities"]]}
    failures, latencies = [], []
    for case in golden["cases"]:
        started = time.perf_counter()
        try:
            call = provider.choose_tool(case["question"], specs, context)
            errors = check(call.name, call.args, case["expect"], golden["facilities"])
        except Exception as exc:
            errors = [f"{type(exc).__name__}: {exc}"]
        latencies.append((time.perf_counter() - started) * 1000)
        if errors:
            failures.append({"question": case["question"], "errors": errors})
    total = len(golden["cases"])
    return {
        "provider": provider.name,
        "total": total,
        "passed": total - len(failures),
        "accuracy": round((total - len(failures)) / total, 3),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(sorted(latencies)[int(0.95 * (total - 1))], 1),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solarops.evals")
    parser.add_argument("--provider", default="none")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--min-accuracy", type=float, default=0.9)
    args = parser.parse_args(argv)
    report = run(get_provider(args.provider), load(args.path))
    print(json.dumps(report, indent=2))
    return 0 if report["accuracy"] >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
