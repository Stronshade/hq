"""nomic-embed-text via Ollama embeddings API + in-app cosine ranking (no pgvector)."""

from __future__ import annotations

import logging
import math
from typing import Sequence

import httpx

from ap_inbox.config import Settings, get_settings

logger = logging.getLogger(__name__)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


async def embed_texts(
    texts: list[str],
    *,
    settings: Settings | None = None,
) -> list[list[float]]:
    """Embed one or more texts with Ollama. Empty strings yield empty vectors."""
    s = settings or get_settings()
    out: list[list[float]] = []
    async with httpx.AsyncClient(base_url=s.ollama_host, timeout=120.0) as client:
        for text in texts:
            if not (text or "").strip():
                out.append([])
                continue
            resp = await client.post(
                "/api/embeddings",
                json={"model": s.embed_model, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("embedding")
            if not isinstance(emb, list):
                raise RuntimeError(f"unexpected embeddings payload: {data!r}")
            out.append([float(x) for x in emb])
    return out


async def embed_query(text: str, *, settings: Settings | None = None) -> list[float]:
    vecs = await embed_texts([text], settings=settings)
    return vecs[0] if vecs else []


def embed_texts_sync(
    texts: list[str],
    *,
    settings: Settings | None = None,
) -> list[list[float]]:
    """Sync helper for denote batch worker."""
    s = settings or get_settings()
    out: list[list[float]] = []
    with httpx.Client(base_url=s.ollama_host, timeout=120.0) as client:
        for text in texts:
            if not (text or "").strip():
                out.append([])
                continue
            try:
                resp = client.post(
                    "/api/embeddings",
                    json={"model": s.embed_model, "prompt": text},
                )
                resp.raise_for_status()
                data = resp.json()
                emb = data.get("embedding")
                if not isinstance(emb, list):
                    raise RuntimeError(f"unexpected embeddings payload: {data!r}")
                out.append([float(x) for x in emb])
            except Exception as exc:  # noqa: BLE001
                logger.warning("embed failed: %s", exc)
                out.append([])
    return out
