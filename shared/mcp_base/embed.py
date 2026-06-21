"""Shared LOCAL text-embedding helper (free, offline) — for semantic/hybrid search.

Prefers model2vec (light, no torch); falls back to sentence-transformers; returns None when neither is
installed so callers degrade gracefully to keyword search. Vectors are normalized so cosine is
comparable across backends. Mirrors the proven approach in servers/codeindex/server.py.
"""
from __future__ import annotations

import math
import struct

MODEL2VEC_MODEL = "minishlab/potion-base-8M"
ST_MODEL = "all-MiniLM-L6-v2"


class _Model2VecWrapper:
    name = MODEL2VEC_MODEL

    def __init__(self, model):
        self._m = model

    def encode(self, texts, normalize_embeddings: bool = True, show_progress_bar: bool = False):
        out = []
        for v in self._m.encode(list(texts)):
            row = [float(x) for x in v]
            if normalize_embeddings:
                norm = math.sqrt(sum(x * x for x in row)) or 1.0
                row = [x / norm for x in row]
            out.append(row)
        return out


def embedder():
    """Lazy, cached local embedder. None if no backend is installed (callers fall back to FTS)."""
    if getattr(embedder, "_cache", "x") != "x":
        return embedder._cache  # type: ignore[attr-defined]
    model = None
    try:
        from model2vec import StaticModel  # type: ignore
        model = _Model2VecWrapper(StaticModel.from_pretrained(MODEL2VEC_MODEL))
    except Exception:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            model = SentenceTransformer(ST_MODEL)
        except Exception:
            model = None
    embedder._cache = model  # type: ignore[attr-defined]
    return model


def model_name() -> str:
    m = embedder()
    return "unavailable" if m is None else getattr(m, "name", ST_MODEL)


def available() -> bool:
    return embedder() is not None


def encode(texts: list[str]) -> list[list[float]]:
    """Encode a batch of texts to normalized vectors. [] if no backend."""
    m = embedder()
    if m is None:
        return []
    try:
        return m.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    except Exception:
        return []


def encode_one(text: str) -> list[float] | None:
    v = encode([text])
    return v[0] if v else None


def pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
