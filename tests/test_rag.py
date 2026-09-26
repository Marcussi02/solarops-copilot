"""Knowledge-base retrieval: chunking, ranking quality, hybrid fusion and cited answers."""

import json
from pathlib import Path

import pytest

from solarops.copilot import agent, router, tools
from solarops.copilot.types import ProviderError, ToolCall
from solarops.rag import Retriever, corpus, evaluate, lexical
from solarops.rag.embeddings import EmbeddingError, get_embedder

GOLDEN = json.loads((Path(__file__).parents[1] / "evals" / "retrieval.json").read_text())

# ---------- corpus ----------


def test_corpus_is_chunked_by_section_with_stable_ids():
    chunks = corpus.load_corpus()
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids)) and len(chunks) > 40
    assert "curtailment#economic-curtailment-at-negative-prices" in ids
    assert not any(c.doc.lower() == "readme" for c in chunks)
    assert all(c.text and len(c.text) <= corpus.MAX_CHARS for c in chunks)


def test_long_sections_split_on_paragraphs():
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(8))
    chunks = corpus.chunk_markdown("doc", f"# Doc\n\n## Long part\n\n{body}\n")
    assert [c.id for c in chunks][:2] == ["doc#long-part", "doc#long-part-2"]
    assert all(len(c.text) <= corpus.MAX_CHARS for c in chunks)


def test_duplicate_headings_are_rejected(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\n## Same\n\none\n\n## Same\n\ntwo\n")
    with pytest.raises(ValueError):
        corpus.load_corpus(tmp_path)


def test_stemming_matches_word_forms():
    assert lexical.tokenize("prices priced price") == ["pric"] * 3
    assert "cleaning" not in lexical.tokenize("washing", expand=True)
    assert lexical.tokenize("wash", expand=True)[-1] == lexical.tokenize("cleaning")[0]


# ---------- ranking quality (CI gate) ----------


def test_golden_retrieval_quality_gate():
    report = evaluate.run(Retriever(), GOLDEN)
    assert report["hit@3"] >= 0.9, report["misses@3"]
    assert report["mrr"] >= 0.8


def test_every_golden_retrieval_question_routes_to_the_knowledge_base():
    rules = router.RuleBasedProvider()
    routed = {c["question"]: rules.choose_tool(c["question"], [], {}).name for c in GOLDEN["cases"]}
    assert {q: t for q, t in routed.items() if t != "search_docs"} == {}


def test_unrelated_query_returns_nothing():
    assert Retriever().search("how are you", 4) == []


# ---------- hybrid retrieval ----------


class FakeEmbedder:
    """Deterministic bag-of-words vectors over a tiny vocabulary."""

    name = "fake"
    VOCAB = ["stow", "wind", "tracker", "curtail", "price", "inverter", "dust"]

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[t.lower().count(w) + 0.01 for w in self.VOCAB] for t in texts]


class DownEmbedder:
    name = "down"

    def embed(self, texts):
        raise EmbeddingError("503")


def test_hybrid_fuses_rankings_and_caches_corpus_vectors():
    fake = FakeEmbedder()
    r = Retriever(embedder=fake)
    first = r.search("Why do trackers go flat on windy days?", 3)
    r.search("negative prices", 3)
    assert first[0].method == "hybrid"
    assert first[0].chunk.id == "trackers#wind-and-hail-stow"
    assert fake.calls == 3  # corpus once, then one call per query


def test_embedding_outage_degrades_to_keywords():
    hits = Retriever(embedder=DownEmbedder()).search("What is a DUID?", 2)
    assert hits[0].method == "bm25" and hits[0].chunk.id == "nem-data#duids-and-facilities"


def test_get_embedder_defaults_to_none(monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_PROVIDER", raising=False)
    assert get_embedder() is None
    with pytest.raises(ValueError):
        get_embedder("word2vec")


# ---------- the search_docs tool and cited answers ----------


def test_search_docs_tool_is_bounded():
    with pytest.raises(tools.ToolError):
        tools.validate("search_docs", {"query": "x"})
    with pytest.raises(tools.ToolError):
        tools.validate("search_docs", {"query": "curtailment", "k": 50})


def test_extractive_answer_cites_its_passages():
    result = tools.run_tool(None, "search_docs", {"query": "What is a DUID?"})
    text = router.summarise(ToolCall("search_docs", {}), result)
    assert "dispatchable unit identifier" in text and text.endswith("[1]")
    agent.check_citations(text, result)  # does not raise


def test_empty_result_says_so():
    text = router.summarise(ToolCall("search_docs", {}), {"query": "q", "results": []})
    assert "couldn't find" in text


@pytest.mark.parametrize("text", ["No citation here.", "Made up [7]."])
def test_citation_check_rejects_uncited_or_invented_sources(text):
    result = {"results": [{"n": 1}, {"n": 2}]}
    with pytest.raises(ProviderError):
        agent.check_citations(text, result)


class DocsModel:
    """Routes to search_docs and answers with the given text."""

    def __init__(self, answer):
        self.name, self.answer = "docs-model", answer

    def choose_tool(self, question, specs, context):
        return ToolCall("search_docs", {"query": question, "k": 3})

    def summarise(self, question, call, result):
        return self.answer


def test_model_answer_with_valid_citations_is_kept(seeded):
    answer = agent.ask(seeded, "What is backtracking?", DocsModel("Rows rotate back [1]."))
    assert answer.provider == "docs-model" and answer.answer == "Rows rotate back [1]."
    assert answer.sources[0]["id"] == "trackers#backtracking"


def test_uncited_model_answer_is_replaced_by_extractive_answer(seeded):
    answer = agent.ask(seeded, "What is backtracking?", DocsModel("Trust me."))
    assert answer.provider == "rules" and "cites no passages" in answer.fallback_reason
    assert "[1]" in answer.answer and answer.sources


def test_diagnostic_data_question_attaches_guidance(seeded):
    answer = agent.ask(seeded, "Why are farms in NSW underperforming right now?",
                       router.RuleBasedProvider())
    assert answer.tool == "underperformers"
    assert answer.sources and "Related guidance:" in answer.answer
