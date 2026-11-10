"""Unit tests for demo-question tool choice, lookup keys, and summary gaps."""

from __future__ import annotations

from ap_inbox.llm.orchestrator import _extract_gaps_from_tool
from ap_inbox.llm.preflight import named_invoice_number, plan_preflight, vendor_phrase
from ap_inbox.services.tool_backend import (
    diverse_page,
    invoice_lookup_keys,
    normalize_chase_stage,
    page_meta,
)

CLASSIFY = (
    "How are the emails in this inbox classified? Give counts by document type "
    "(invoice, statement, payment reminder, remittance, escalation, final notice, other). "
    "For anything marked low confidence or error, say how many and why."
)
THOMSON = (
    "Show the key fields for Thomson Reuters invoice 853825322: vendor, invoice number, "
    "amount, currency, invoice date, due date, and confidence. Cite the message id. "
    "If a field is missing, say so."
)
PUNCH_INVOICES = (
    "List Punch Distribution invoices you have extracted, with invoice number, amount, date, "
    "and confidence. Do not invent rows beyond what the tool returns, and say if the list is truncated."
)
PUNCH_CHASE = (
    "For Punch Distribution, which chase emails do we have? For each, give the stage "
    "(reminder, escalation, or final notice), the invoice numbers they mention, whether that "
    "invoice is actually in the inbox, and your confidence. Cite message ids."
)
FINAL = (
    "List every final notice in the inbox. For each, give vendor, subject, confidence, and "
    "whether a chased invoice is present. If a “final notice” does not look like an unpaid AP invoice, say that."
)
FPL = (
    "Which FPL Communications chase emails refer to invoices that are not in the inbox? "
    "Give the stage and the referenced invoice numbers. If no invoice number was extracted, say that explicitly."
)
MISSING = "Which attachments are missing from disk? Give fil_id, filename, and the error. Do not guess their contents."
UNREADABLE = (
    "Which invoice extractions failed because the PDF had no text? Quote the error detail and "
    "the confidence or status. Do not invent amounts for those files."
)
GLASS = (
    "Find the Glass House Brands invoice PDF (filename contains Glass-House-Brands, fil_id fil_4xyrdndg). "
    "Check attachment status first. If it is ok, read the text and pull vendor, invoice number, amount, and dates. "
    "If you cannot read it, stop and say why."
)
LOW = (
    "Show messages classified as “other” with low confidence. How many are there, what confidence "
    "did we assign, and what is the rationale? Do not force them into invoice or reminder."
)
MISS = "Is invoice ZZ-DOES-NOT-EXIST-999 in the inbox? Say whether you found it, and your confidence."
STATEMENTS = (
    "Show a few emails classified as statements and a few classified as remittances. "
    "Include subject, confidence, and rationale. If a remittance label looks like a payment reminder, say so."
)


def test_stage_aliases_final_notice():
    assert normalize_chase_stage("final") == "final_notice"
    assert normalize_chase_stage("final notice") == "final_notice"
    assert normalize_chase_stage("Final Notice") == "final_notice"
    assert normalize_chase_stage("reminder") == "reminder"
    assert normalize_chase_stage("escalation") == "escalation"


def test_fake_invoice_does_not_collapse_to_short_digits():
    keys, numeric = invoice_lookup_keys("ZZ-DOES-NOT-EXIST-999")
    assert keys == ["ZZ-DOES-NOT-EXIST-999"]
    assert numeric is False
    assert named_invoice_number(MISS) == "ZZ-DOES-NOT-EXIST-999"

    keys, numeric = invoice_lookup_keys("853825322")
    assert numeric is True
    assert "853825322" in keys
    assert "0853825322" in keys


def test_page_meta_truncation_flag():
    assert page_meta(311, 40) == {"total": 311, "returned": 40, "truncated": True}
    assert page_meta(5, 5)["truncated"] is False


def test_diverse_page_keeps_each_stage_inside_the_limit():
    reps = [{"id": "esc", "stage": "escalation"}, {"id": "rem", "stage": "reminder"}]
    rest = [{"id": f"r{i}", "stage": "reminder"} for i in range(10)]
    page = diverse_page(reps, rest, 4)
    assert [row["id"] for row in page] == ["esc", "rem", "r0", "r1"]
    assert {row["stage"] for row in page} == {"escalation", "reminder"}


def test_preflight_routes_the_twelve_questions():
    assert plan_preflight(CLASSIFY)[0].tool == "get_annotations"
    assert plan_preflight(CLASSIFY)[0].args == {}

    thomson = plan_preflight(THOMSON)
    assert [(c.tool, c.args.get("invoice_number")) for c in thomson] == [("get_invoice_fields", "853825322")]
    assert all(c.tool != "fetch_attachment_text" for c in thomson)

    punch = plan_preflight(PUNCH_INVOICES)[0]
    assert punch.tool == "get_invoice_fields"
    assert punch.args["vendor"] == "Punch Distribution"

    chase = plan_preflight(PUNCH_CHASE)[0]
    assert chase.tool == "get_chase_events"
    assert chase.args["vendor"] == "Punch Distribution"
    assert "present" not in chase.args

    final = plan_preflight(FINAL)[0]
    assert final.tool == "get_chase_events"
    assert final.args["stage"] == "final_notice"

    fpl = plan_preflight(FPL)[0]
    assert fpl.tool == "get_chase_events"
    assert fpl.args["vendor"] == "FPL Communications"
    assert fpl.args["present"] is False

    missing = plan_preflight(MISSING)[0]
    assert missing.tool == "list_attachment_meta"
    assert missing.args["status"] == "missing_file"

    unreadable = plan_preflight(UNREADABLE)[0]
    assert unreadable.tool == "get_invoice_fields"
    assert unreadable.args["status"] == "unreadable"

    glass = plan_preflight(GLASS)[0]
    assert glass.tool == "list_attachment_meta"
    assert glass.args["fil_id"] == "fil_4xyrdndg"
    assert glass.read_text_if_ok is True

    low = plan_preflight(LOW)[0]
    assert low.tool == "get_annotations"
    assert low.args["doc_type"] == "other"
    assert low.args["status"] == "low_confidence"

    miss = plan_preflight(MISS)[0]
    assert miss.tool == "get_invoice_fields"
    assert miss.args["invoice_number"] == "ZZ-DOES-NOT-EXIST-999"

    both = plan_preflight(STATEMENTS)
    assert [c.args["doc_type"] for c in both] == ["statement", "remittance"]
    assert vendor_phrase(PUNCH_INVOICES) == "Punch Distribution"
    assert vendor_phrase(FPL) == "FPL Communications"


def test_summary_gaps_are_one_per_kind():
    missing = _extract_gaps_from_tool(
        "list_attachment_meta",
        {
            "status": "missing_file",
            "total": 83,
            "by_filename": [{"filename": "Kanha horizontal.png", "count": 56}],
            "items": [
                {"fil_id": "fil_a", "message_id": "m1", "error_detail": "no file matching fil_a under attachments/"},
                {"fil_id": "fil_b", "message_id": "m2", "error_detail": "no file matching fil_b under attachments/"},
            ],
        },
    )
    assert len(missing) == 1
    assert missing[0]["kind"] == "missing_file"
    assert "83" in missing[0]["detail"]
    assert "path" not in missing[0]

    unread = _extract_gaps_from_tool(
        "get_invoice_fields",
        {
            "total": 1116,
            "returned": 2,
            "truncated": True,
            "by_error_detail": [{"error_detail": "pdf text extract returned empty", "count": 1116}],
            "items": [{}, {}],
        },
    )
    assert len(unread) == 1 and unread[0]["kind"] == "unreadable"

    miss = _extract_gaps_from_tool(
        "get_invoice_fields",
        {"found": False, "invoice_number": "ZZ-DOES-NOT-EXIST-999", "total": 0, "returned": 0, "items": []},
    )
    assert len(miss) == 1 and miss[0]["kind"] == "no_matches"

    low = _extract_gaps_from_tool(
        "get_annotations",
        {
            "filters": {"doc_type": "other", "status": "low_confidence"},
            "total": 1098,
            "rationale_histogram": [
                {"rationale": "no keyword rule matched", "count": 772, "confidence": 0.35}
            ],
            "items": [{}, {}],
        },
    )
    assert len(low) == 1 and low[0]["kind"] == "low_confidence"
