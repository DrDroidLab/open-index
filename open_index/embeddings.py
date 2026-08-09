"""Embedding providers for semantic search.

The default is a local ONNX model via `fastembed` (BAAI/bge-small-en-v1.5, 384-D).
An OpenAI-compatible API can be configured via environment variables:

    OPEN_INDEX_EMBEDDING_BASE_URL
    OPEN_INDEX_EMBEDDING_API_KEY
    OPEN_INDEX_EMBEDDING_MODEL
    OPEN_INDEX_EMBEDDING_DIM

Everything is optional: if no provider is configured, the backends fall back to
keyword-only search and log a one-time warning.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("open_index.embeddings")
_warned_once = False


def _warn_once(message: str) -> None:
    global _warned_once
    if not _warned_once:
        logger.warning(message)
        _warned_once = True


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that can turn a list of strings into dense vectors."""

    name: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FastEmbedProvider:
    """Local ONNX embedding via fastembed (default: BAAI/bge-small-en-v1.5)."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", dim: Optional[int] = None,
                 cache_dir: Optional[str] = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "fastembed is not installed; install 'open-index[semantic]'"
            ) from exc
        self.model = model
        self._dim = dim or 384
        # fastembed defaults to a temp directory, which containers throw away on
        # every recreate — a ~90MB re-download each restart. `cache_dir` (from
        # OPEN_INDEX_EMBEDDING_CACHE) lets a deployment point it somewhere
        # persistent; passing None keeps fastembed's own default.
        self.cache_dir = cache_dir
        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        self._embedder = TextEmbedding(model_name=model, **kwargs)

    @property
    def name(self) -> str:
        return f"fastembed/{self.model}"

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = list(self._embedder.embed(texts))
        return [v.tolist() for v in vectors]


class OpenAICompatibleProvider:
    """OpenAI-compatible API embeddings (e.g. OpenAI, local text-embedding-inference)."""

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._dim = int(dim)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60.0,
        )

    @property
    def name(self) -> str:
        return f"openai/{self.model}"

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        import httpx

        try:
            response = self._client.post(
                "/embeddings",
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"embedding request failed: {exc}") from exc

        data = sorted(response.json()["data"], key=lambda x: x.get("index", 0))
        vectors: list[list[float]] = []
        for item in data:
            vec = list(item["embedding"])
            if len(vec) < self._dim:
                vec = vec + [0.0] * (self._dim - len(vec))
            elif len(vec) > self._dim:
                vec = vec[: self._dim]
            # Normalize so cosine similarity is well-defined.
            norm = math.sqrt(sum(x * x for x in vec))
            if norm:
                vec = [x / norm for x in vec]
            vectors.append(vec)
        return vectors


class FakeEmbedProvider:
    """Deterministic, model-free embedding provider for tests.

    Uses a small set of synonym groups so conceptually related phrases land near
    each other even when they share no literal keywords. Unknown tokens fall back
    to a deterministic hash vector, keeping the provider stable across runs.
    """

    DEFAULT_GROUPS: dict[str, list[str]] = {
        "payment": [
            "payment", "card", "credit", "bank", "issuer", "authorization", "purchase",
            "buy", "buyer", "shopper", "customer", "basket", "checkout", "amount",
            "charge", "charged", "withdrawn", "statement", "receipt", "invoice",
            "confirm", "confirmation", "order",
        ],
        "rejected": [
            "refused", "rejected", "declined", "denied", "fails", "failure", "fail",
            "not", "accepted", "unauthorized", "invalid", "broken", "error",
        ],
        "auth": [
            "authentication", "authenticated", "credentials", "login", "log-in", "sign-in",
            "signin", "stay", "bounced", "loop", "entering", "correct", "enter", "logged",
        ],
        "email": [
            "email", "mail", "inbox", "message", "notification", "notify", "receive",
            "arriving", "arrived", "missing", "never", "sent",
        ],
        "slow": [
            "slow", "sluggish", "forever", "taking", "render", "load", "page", "mobile",
            "phone", "network", "latency", "long", "website", "complaint",
        ],
        "duplicate": [
            "double", "twice", "duplicate", "two", "same", "again", "multiple", "billed",
        ],
    }

    def __init__(self, dim: int = 32, synonym_groups: Optional[dict[str, list[str]]] = None):
        self._dim = dim
        self.groups = synonym_groups or self.DEFAULT_GROUPS
        self._token_to_concepts: dict[str, list[int]] = {}
        for concept, tokens in self.groups.items():
            idx = self._concept_index(concept)
            for token in tokens:
                self._token_to_concepts.setdefault(token, []).append(idx)
        self._cache: dict[str, list[float]] = {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def dim(self) -> int:
        return self._dim

    def _concept_index(self, concept: str) -> int:
        digest = hashlib.md5(concept.encode()).hexdigest()
        return int(digest, 16) % self._dim

    def _token_vector(self, token: str) -> list[float]:
        token = token.lower().strip(".,;:!?\"'")
        if token in self._cache:
            return self._cache[token]

        if token in self._token_to_concepts:
            indices = self._token_to_concepts[token]
            vec = [0.0] * self._dim
            for i in indices:
                vec[i] = 1.0 / len(indices)
            norm = math.sqrt(sum(x * x for x in vec))
            if norm:
                vec = [x / norm for x in vec]
            self._cache[token] = vec
            return vec

        # Unknown token: deterministic pseudo-random unit vector.
        digest = hashlib.md5(token.encode()).hexdigest()
        vec = []
        step = 4
        for i in range(self._dim):
            chunk = digest[(i * step) % len(digest) : ((i * step) % len(digest)) + step]
            val = int(chunk, 16) / 0xFFFF - 0.5
            vec.append(val)
        norm = math.sqrt(sum(x * x for x in vec))
        if norm:
            vec = [x / norm for x in vec]
        self._cache[token] = vec
        return vec

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            tokens = [
                t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split()
                if t
            ]
            if not tokens:
                vectors.append([0.0] * self._dim)
                continue
            vec = [0.0] * self._dim
            for token in tokens:
                tv = self._token_vector(token)
                for i, v in enumerate(tv):
                    vec[i] += v
            norm = math.sqrt(sum(x * x for x in vec))
            if norm:
                vec = [x / norm for x in vec]
            vectors.append(vec)
        return vectors


def embedding_provider_available() -> bool:
    """Cheap check for whether any embedding provider could be constructed.

    Does not load any model. Used by the UI to warn before the user picks
    Semantic mode in an environment without embeddings.
    """
    if os.environ.get("OPEN_INDEX_EMBEDDING_BASE_URL"):
        return all(
            os.environ.get(v)
            for v in (
                "OPEN_INDEX_EMBEDDING_API_KEY",
                "OPEN_INDEX_EMBEDDING_MODEL",
                "OPEN_INDEX_EMBEDDING_DIM",
            )
        )
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


_provider_cache: dict[tuple, Optional[EmbeddingProvider]] = {}


def get_embedding_provider(config) -> Optional[EmbeddingProvider]:
    """Return the configured embedding provider, or None if not available.

    Providers are cached process-wide (keyed on their configuration) so that
    multiple backends — e.g. both engines behind the UI's engine toggle —
    share a single loaded model.
    """
    base_url = os.environ.get("OPEN_INDEX_EMBEDDING_BASE_URL")
    api_key = os.environ.get("OPEN_INDEX_EMBEDDING_API_KEY")
    model = os.environ.get("OPEN_INDEX_EMBEDDING_MODEL")
    dim = os.environ.get("OPEN_INDEX_EMBEDDING_DIM")
    local_model = (config.search.embedding_model if config else None) or "BAAI/bge-small-en-v1.5"
    cache_dir = os.environ.get("OPEN_INDEX_EMBEDDING_CACHE")
    key = (base_url, api_key, model, dim,
           None if base_url else (local_model, cache_dir))
    if key in _provider_cache:
        return _provider_cache[key]

    provider: Optional[EmbeddingProvider] = None
    if base_url:
        if not (api_key and model and dim):
            _warn_once(
                "OPEN_INDEX_EMBEDDING_BASE_URL is set but one of API_KEY/MODEL/DIM is missing; "
                "semantic search is disabled."
            )
        else:
            try:
                provider = OpenAICompatibleProvider(base_url, api_key, model, int(dim))
            except Exception as exc:  # pragma: no cover
                _warn_once(f"Could not create OpenAI-compatible embedding provider: {exc}")
    else:
        try:
            from fastembed import TextEmbedding  # noqa: F401
        except ImportError:
            _warn_once(
                "fastembed is not installed; semantic search is disabled. "
                "Install 'open-index[semantic]' to enable it."
            )
        else:
            try:
                provider = FastEmbedProvider(
                    local_model, cache_dir=os.environ.get("OPEN_INDEX_EMBEDDING_CACHE")
                )
            except Exception as exc:  # pragma: no cover
                _warn_once(f"Could not create FastEmbedProvider: {exc}")

    _provider_cache[key] = provider
    return provider
