"""Retrieval-augmented answers over the operations knowledge base (src/solarops/knowledge)."""

from .corpus import Chunk, load_corpus
from .retriever import Hit, Retriever, get_retriever

__all__ = ["Chunk", "Hit", "Retriever", "get_retriever", "load_corpus"]
