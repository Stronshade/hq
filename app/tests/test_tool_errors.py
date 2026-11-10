"""Unit tests for structured attachment tool errors (no FS path leakage)."""

from __future__ import annotations

import pytest

from ap_inbox.services.tool_backend import (
    ToolError,
    _safe_fil_id,
    number_matches_text,
    query_tokens,
)


def test_safe_fil_id_accepts_front_ids():
    assert _safe_fil_id("fil_abc123") == "fil_abc123"


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "fil_../../x", "fil_a/b", "fil_a\\b", "attachment.pdf", ""],
)
def test_safe_fil_id_rejects_traversal(bad: str):
    with pytest.raises(ToolError) as ei:
        _safe_fil_id(bad)
    assert ei.value.code == "tool_denied"


THOMSON_QUESTION = (
    "Show the key fields for Thomson Reuters invoice 853825322: vendor, invoice number, "
    "amount, currency, invoice date, due date, and confidence. Cite the message id. "
    "If a field is missing, say so."
)


def test_invoice_question_keeps_number_not_stopwords():
    words, numbers = query_tokens(THOMSON_QUESTION)
    assert "853825322" in numbers
    assert "0853825322" in numbers
    lowered = [w.lower() for w in words]
    assert "the" not in lowered
    assert "invoice" not in lowered
    assert "thomson" in lowered
    assert "reuters" in lowered


def test_invoice_number_matches_leading_zero_filename():
    filename = "WEST_1005873017_0853825322_ZCPG_07012026.pdf"
    assert number_matches_text("853825322", filename)
    assert number_matches_text("0853825322", filename)
    assert not number_matches_text("215721", filename)


def test_tool_error_dict_shape():
    err = ToolError("missing_file", "gone", fil_id="fil_x", message_id="m1")
    d = err.to_dict()
    assert d["error"] is True
    assert d["code"] == "missing_file"
    assert d["fil_id"] == "fil_x"
    assert "path" not in d
