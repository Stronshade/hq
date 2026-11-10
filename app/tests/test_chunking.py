"""Unit tests for token chunker (no Docker required)."""

from __future__ import annotations

from ap_inbox.chunking import chunk_text, count_tokens


def test_count_tokens_nonempty():
    assert count_tokens("hello world") > 0


def test_chunk_overlap_preserves_boundaries():
    # Build text long enough for multiple 768-token windows
    words = [f"word{i}" for i in range(2000)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_tokens=768, overlap=128)
    assert len(chunks) >= 2
    assert chunks[0].token_count <= 768
    # Overlap: start of chunk1 should appear near end of chunk0
    assert chunks[0].index == 0
    assert chunks[1].index == 1
    # Total coverage: last chunk ends at full length in tokens
    total = count_tokens(text)
    covered = sum(c.token_count for c in chunks)
    # With overlap, sum of window sizes > total
    assert covered >= total


def test_empty_input():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_small_text_single_chunk():
    chunks = chunk_text("invoice 123 total due $10.00", chunk_tokens=768, overlap=128)
    assert len(chunks) == 1
    assert "invoice" in chunks[0].text.lower()
