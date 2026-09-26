"""Optional dense embeddings for hybrid retrieval.

    EMBEDDINGS_PROVIDER=none     keyword retrieval only (default; free, offline)
    EMBEDDINGS_PROVIDER=bedrock  Amazon Titan Text Embeddings v2 (BEDROCK_EMBED_MODEL_ID)
    EMBEDDINGS_PROVIDER=openai   OpenAI text-embedding-3-small (OPENAI_EMBED_MODEL)

Corpus vectors are computed once per warm container and kept in memory: at a few
dozen chunks, brute-force cosine similarity is faster than any vector index.
"""

import json
import math
import os
import urllib.request


class EmbeddingError(RuntimeError):
    pass


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class BedrockEmbedder:
    name = "bedrock"

    def __init__(self, model_id: str | None = None, client=None, dimensions: int = 512):
        self.model_id = model_id or os.environ.get(
            "BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"
        )
        self.dimensions = dimensions
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime")
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:  # Titan v2 embeds one text per request
            try:
                resp = self.client.invoke_model(
                    modelId=self.model_id,
                    body=json.dumps(
                        {"inputText": text[:8000], "dimensions": self.dimensions, "normalize": True}
                    ),
                )
                out.append(json.loads(resp["body"].read())["embedding"])
            except Exception as exc:
                raise EmbeddingError(f"bedrock embedding failed: {exc}") from exc
        return out


class OpenAIEmbedder:
    name = "openai"
    URL = "https://api.openai.com/v1/embeddings"

    def __init__(self, model: str | None = None, api_key: str | None = None, post=None):
        self.model = model or os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self._api_key = api_key
        self._post = post or self._http_post

    def _http_post(self, body: dict) -> dict:
        from .. import config

        key = self._api_key or config.openai_api_key()
        if not key:
            raise EmbeddingError("OPENAI_API_KEY is not configured")
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = self._post({"model": self.model, "input": texts})
            return [row["embedding"] for row in sorted(resp["data"], key=lambda r: r["index"])]
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"openai embedding failed: {exc}") from exc


def get_embedder(name: str | None = None):
    name = (name or os.environ.get("EMBEDDINGS_PROVIDER") or "none").lower()
    if name in ("none", ""):
        return None
    if name == "bedrock":
        return BedrockEmbedder()
    if name == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"unknown EMBEDDINGS_PROVIDER {name!r}")
