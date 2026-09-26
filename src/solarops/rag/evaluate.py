"""Score retrieval against the golden question set.

    python -m solarops.rag.evaluate                      # keyword retrieval (CI gate)
    python -m solarops.rag.evaluate --embeddings bedrock # hybrid (needs AWS access)

Reports hit@1, hit@3, hit@5 and mean reciprocal rank at section level, plus hit@3
at document level. Exits non-zero if hit@3 is below --min-hit-at-3.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

from .corpus import load_corpus
from .embeddings import get_embedder
from .retriever import Retriever

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "evals" / "retrieval.json"


def run(retriever: Retriever, golden: dict, k_max: int = 5) -> dict:
    known = {c.id for c in retriever.chunks}
    hits = {1: 0, 3: 0, 5: 0}
    doc_hits3, reciprocal, latencies, misses = 0, [], [], []
    for case in golden["cases"]:
        expected = set(case["expect"])
        unknown = expected - known
        if unknown:
            raise ValueError(f"golden set references unknown sections: {sorted(unknown)}")
        started = time.perf_counter()
        ranked = [h.chunk.id for h in retriever.search(case["question"], k=k_max)]
        latencies.append((time.perf_counter() - started) * 1000)
        rank = next((i + 1 for i, cid in enumerate(ranked) if cid in expected), None)
        for k in hits:
            hits[k] += rank is not None and rank <= k
        reciprocal.append(1 / rank if rank else 0.0)
        expected_docs = {e.split("#")[0] for e in expected}
        doc_hits3 += any(cid.split("#")[0] in expected_docs for cid in ranked[:3])
        if rank is None or rank > 3:
            misses.append({"question": case["question"], "expected": sorted(expected),
                           "got": ranked[:3]})
    n = len(golden["cases"])
    return {
        "retriever": "hybrid" if retriever.embedder else "bm25",
        "cases": n,
        "chunks": len(retriever.chunks),
        "hit@1": round(hits[1] / n, 3),
        "hit@3": round(hits[3] / n, 3),
        "hit@5": round(hits[5] / n, 3),
        "mrr": round(statistics.mean(reciprocal), 3),
        "doc_hit@3": round(doc_hits3 / n, 3),
        "p50_ms": round(statistics.median(latencies), 2),
        "misses@3": misses,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solarops.rag.evaluate")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--embeddings", default="none")
    parser.add_argument("--min-hit-at-3", type=float, default=0.8)
    args = parser.parse_args(argv)
    retriever = Retriever(load_corpus(), get_embedder(args.embeddings))
    report = run(retriever, json.loads(Path(args.path).read_text()))
    print(json.dumps(report, indent=2))
    return 0 if report["hit@3"] >= args.min_hit_at_3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
