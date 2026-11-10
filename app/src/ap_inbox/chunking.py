"""Token-aware overlapping chunker for message bodies and attachment text."""

from __future__ import annotations

from dataclasses import dataclass

from ap_inbox.config import Settings, get_settings


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int


_ENC = None


def _encoding():
    global _ENC
    if _ENC is None:
        import tiktoken

        # cl100k approx — good enough for budget accounting without model-native tokenize
        _ENC = tiktoken.get_encoding("cl100k_base")
    return _ENC


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_encoding().encode(text))


def chunk_text(
    text: str,
    *,
    chunk_tokens: int | None = None,
    overlap: int | None = None,
    settings: Settings | None = None,
) -> list[TextChunk]:
    """Split text into overlapping token windows. Empty input → []."""
    s = settings or get_settings()
    size = chunk_tokens if chunk_tokens is not None else s.chunk_tokens
    ov = overlap if overlap is not None else s.chunk_overlap
    if size <= 0:
        raise ValueError("chunk_tokens must be > 0")
    if ov < 0 or ov >= size:
        raise ValueError("chunk_overlap must be >= 0 and < chunk_tokens")

    cleaned = (text or "").strip()
    if not cleaned:
        return []

    enc = _encoding()
    tokens = enc.encode(cleaned)
    if not tokens:
        return []

    step = size - ov
    out: list[TextChunk] = []
    start = 0
    idx = 0
    while start < len(tokens):
        end = min(start + size, len(tokens))
        piece = enc.decode(tokens[start:end])
        out.append(TextChunk(index=idx, text=piece, token_count=end - start))
        idx += 1
        if end >= len(tokens):
            break
        start += step
    return out
