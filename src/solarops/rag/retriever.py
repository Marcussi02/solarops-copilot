"""Keyword (BM25) retrieval, optionally fused with dense embeddings.

Hybrid mode merges the two rankings with reciprocal rank fusion (RRF), which needs
no score calibration between methods. If the embedding provider fails, search
degrades to keyword-only and says so in `Hit.method`, so outages stay visible.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache

from .corpus import Chunk, corpus_version, load_corpus
from .embeddings import EmbeddingError, cosine, get_embedder
from .lexical import BM25

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 20


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float
    method: str  # "bm25" | "hybrid"

    def to_dict(self, n: int | None = None) -> dict:
        d = {
            "id": self.chunk.id,
            "title": self.chunk.title,
            "section": self.chunk.section,
            "text": self.chunk.text,
            "score": round(self.score, 4),
            "method": self.method,
        }
        return d if n is None else {"n": n, **d}


class Retriever:
    def __init__(self, chunks: tuple[Chunk, ...] | None = None, embedder=None):
        self.chunks = chunks if chunks is not None else load_corpus()
        self.version = corpus_version(self.chunks)
        self.bm25 = BM25(self.chunks)
        self.embedder = embedder
        self._vectors: list[list[float]] | None = None

    def _dense_ranking(self, query: str) -> list[int]:
        if self._vectors is None:
            self._vectors = self.embedder.embed([f"{c.heading}\n{c.text}" for c in self.chunks])
        q = self.embedder.embed([query])[0]
        sims = [cosine(q, v) for v in self._vectors]
        return sorted(range(len(sims)), key=lambda i: -sims[i])[:CANDIDATES]

    def search(self, query: str, k: int = 4) -> list[Hit]:
        scores = self.bm25.scores(query)
        lexical = [i for i in sorted(range(len(scores)), key=lambda i: -scores[i]) if scores[i] > 0]
        lexical = lexical[:CANDIDATES]
        if self.embedder is not None:
            try:
                dense = self._dense_ranking(query)
            except EmbeddingError as exc:
                logger.warning("dense retrieval unavailable, using keywords only: %s", exc)
            else:
                fused: dict[int, float] = {}
                for ranking in (lexical, dense):
                    for rank, i in enumerate(ranking):
                        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
                best = sorted(fused, key=lambda i: -fused[i])[:k]
                return [Hit(self.chunks[i], fused[i], "hybrid") for i in best]
        return [Hit(self.chunks[i], scores[i], "bm25") for i in lexical[:k]]


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    """One retriever per warm Lambda container."""
    return Retriever(load_corpus(), get_embedder())
