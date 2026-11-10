"""Embeddings via an OpenAI-compatible `/v1/embeddings` endpoint.

Default model is `nomic-embed-text` on the same host as chat (Ollama). Set
`EMBED_MODEL` plus optional `EMBED_BASE_URL` / `EMBED_API_KEY` for another host.
Stored vectors are JSON floats; cosine ranking stays in-app (no pgvector).
A model change produces a different vector size — re-run denote without
`--skip-chunks` before searching again.
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

from openai import AsyncOpenAI, OpenAI

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


def _vector_from_response(data: list) -> list[float]:
    if not data:
        raise RuntimeError("embeddings response contained no vectors")
    emb = data[0].embedding
    if not emb:
        raise RuntimeError("embeddings response missing embedding values")
    return [float(x) for x in emb]


async def embed_texts(
    texts: list[str],
    *,
    settings: Settings | None = None,
) -> list[list[float]]:
    """Embed one or more texts. Empty strings yield empty vectors."""
    s = settings or get_settings()
    out: list[list[float]] = []
    client = AsyncOpenAI(
        api_key=s.effective_embed_api_key,
        base_url=s.effective_embed_base_url,
        timeout=120.0,
    )
    try:
        for text in texts:
            if not (text or "").strip():
                out.append([])
                continue
            resp = await client.embeddings.create(model=s.embed_model, input=text)
            out.append(_vector_from_response(resp.data))
    finally:
        await client.close()
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
    client = OpenAI(
        api_key=s.effective_embed_api_key,
        base_url=s.effective_embed_base_url,
        timeout=120.0,
    )
    try:
        for text in texts:
            if not (text or "").strip():
                out.append([])
                continue
            try:
                resp = client.embeddings.create(model=s.embed_model, input=text)
                out.append(_vector_from_response(resp.data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("embed failed: %s", exc)
                out.append([])
    finally:
        client.close()
    return out
