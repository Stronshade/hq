"""Deterministic tool choice for the demo questions.

The 14B model still writes the answer. These calls make sure the read-only
tool that holds the fact actually runs, and chunk prefetch cannot outvote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FIL_RE = re.compile(r"\b(fil_[A-Za-z0-9]+)\b", re.I)
_INVOICE_TOKEN_RE = re.compile(
    r"\binvoice\s+(?:number\s+)?([A-Za-z0-9][A-Za-z0-9\-]{2,})",
    re.I,
)
_GENERIC_INVOICE_WORDS = frozenset(
    {
        "extractions",
        "extraction",
        "numbers",
        "number",
        "fields",
        "field",
        "pdf",
        "pdfs",
        "rows",
        "list",
        "that",
        "this",
        "here",
        "inbox",
    }
)


@dataclass(frozen=True)
class PreflightCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    read_text_if_ok: bool = False


def named_invoice_number(question: str) -> str | None:
    """Identifier after the word invoice, including a deliberate non-numeric miss."""
    match = _INVOICE_TOKEN_RE.search(question or "")
    if not match:
        return None
    token = match.group(1).strip(".,;:\"'")
    if token.lower() in _GENERIC_INVOICE_WORDS:
        return None
    if re.search(r"\d", token) or "-" in token:
        return token
    return None


def vendor_phrase(question: str) -> str | None:
    """Vendor named in 'For X, which chase', 'Which X chase', or 'List X invoices'."""
    patterns = (
        r"\bFor\s+(.+?),\s+which\s+chase\b",
        r"\bWhich\s+(.+?)\s+chase\b",
        r"\bList\s+(.+?)\s+invoices\b",
    )
    for pattern in patterns:
        match = re.search(pattern, question or "", re.I)
        if not match:
            continue
        vendor = match.group(1).strip(" .,;:\"'")
        vendor = re.sub(r"^(the|a|an)\s+", "", vendor, flags=re.I).strip()
        if vendor and vendor.lower() not in {"which", "every", "all", "any"}:
            return vendor
    return None


def _is_classification_summary(lowered: str) -> bool:
    if "counts by document type" in lowered or "counts by doc_type" in lowered:
        return True
    return "how are" in lowered and "classified" in lowered


def _is_low_confidence_other(lowered: str) -> bool:
    return "low confidence" in lowered and re.search(r"\bother\b", lowered) is not None


def _is_statements_and_remittances(lowered: str) -> bool:
    return "statement" in lowered and "remittance" in lowered


def _is_missing_attachments(lowered: str) -> bool:
    return "missing" in lowered and any(word in lowered for word in ("attachment", "disk", "file"))


def _is_unreadable(lowered: str) -> bool:
    return any(
        phrase in lowered
        for phrase in ("unreadable", "no text", "no pdf text", "had no text", "empty pdf")
    )


def _mentions(lowered: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", lowered) is not None


def _wants_absent_invoice(lowered: str) -> bool:
    return "not in the inbox" in lowered or "aren't in the inbox" in lowered


def _is_final_notice_list(lowered: str) -> bool:
    """True when the question is about final notices, not a chase that names every stage."""
    if "final notice" not in lowered or _is_classification_summary(lowered):
        return False
    if _mentions(lowered, "chase") and "reminder" in lowered and "escalation" in lowered:
        return False
    return True


def plan_preflight(question: str) -> list[PreflightCall]:
    """Map one demo question onto the tool calls that hold the answer."""
    text = question or ""
    lowered = text.lower()
    fil_ids = [fil_id for fil_id in _FIL_RE.findall(text) if fil_id.lower() != "fil_id"]
    if fil_ids or ("glass house" in lowered and "fil_" in lowered):
        fil_id = fil_ids[0] if fil_ids else None
        if fil_id:
            return [
                PreflightCall(
                    "list_attachment_meta",
                    {"fil_id": fil_id},
                    intent="attachment_read",
                    read_text_if_ok=True,
                )
            ]

    if _is_classification_summary(lowered):
        return [PreflightCall("get_annotations", {}, intent="classification")]

    if _is_low_confidence_other(lowered):
        return [
            PreflightCall(
                "get_annotations",
                {"doc_type": "other", "status": "low_confidence", "limit": 6},
                intent="low_confidence_other",
            )
        ]

    if _is_statements_and_remittances(lowered):
        return [
            PreflightCall("get_annotations", {"doc_type": "statement", "limit": 5}, intent="statement_remittance"),
            PreflightCall("get_annotations", {"doc_type": "remittance", "limit": 5}, intent="statement_remittance"),
        ]

    if _is_missing_attachments(lowered):
        return [
            PreflightCall(
                "list_attachment_meta",
                {"status": "missing_file", "limit": 12},
                intent="missing_file",
            )
        ]

    if _is_unreadable(lowered):
        return [PreflightCall("get_invoice_fields", {"status": "unreadable", "limit": 8}, intent="unreadable")]

    if _is_final_notice_list(lowered):
        return [PreflightCall("get_chase_events", {"stage": "final_notice", "limit": 20}, intent="final_notice")]

    number = named_invoice_number(text)
    if number:
        return [PreflightCall("get_invoice_fields", {"invoice_number": number, "limit": 20}, intent="named_invoice")]

    if _mentions(lowered, "chase"):
        args: dict[str, Any] = {"limit": 40}
        vendor = vendor_phrase(text)
        if vendor:
            args["vendor"] = vendor
        if _wants_absent_invoice(lowered):
            args["present"] = False
        return [PreflightCall("get_chase_events", args, intent="chase")]

    if "invoice" in lowered:
        vendor = vendor_phrase(text)
        if vendor:
            return [PreflightCall("get_invoice_fields", {"vendor": vendor, "limit": 40}, intent="vendor_invoices")]

    return []


def answer_rules_for(calls: list[PreflightCall]) -> str:
    """Instructions that keep the written answer inside the tool payload."""
    intents = {call.intent for call in calls}
    rules = [
        "Quote totals, confidence, subjects, error_detail, and amounts from the tool evidence. Do not recast them.",
        "If truncated is true, say the list is truncated and do not invent rows that are not in items.",
        "Cite message_id values from the evidence.",
        "Do not invent invoice amounts or file contents.",
    ]
    if "classification" in intents:
        rules.append(
            "Classification counts are by_doc_type and by_status. For low_confidence and error, quote "
            "rationale_histogram (count, confidence, rationale, and error_detail)."
        )
    if "low_confidence_other" in intents:
        rules.append(
            "Quote the filtered total for other + low_confidence, plus confidence and rationale. "
            "Do not force these messages into invoice or payment reminder."
        )
    if "statement_remittance" in intents:
        rules.append(
            "Show a few statements and a few remittances with subject, confidence, and rationale. "
            "If a remittance subject or rationale looks like a payment reminder, say so."
        )
    if "named_invoice" in intents:
        rules.append(
            "If found is true, quote vendor, invoice number, amount, currency, invoice date, due date, "
            "and confidence, and cite the message ids. Do not open PDFs when those fields are present. "
            "If found is false, say the invoice is not in the extracted fields and do not guess a vendor."
        )
    if "vendor_invoices" in intents:
        rules.append(
            "Quote total, returned, and truncated. List only returned rows with invoice number, amount, date, and confidence."
        )
    if "chase" in intents or "final_notice" in intents:
        rules.append(
            "Quote total, truncated, and stage_counts. An empty referenced_invoice_numbers list means no "
            "invoice number was extracted. Quote match_policy: present is an exact case-insensitive match, "
            "so a junk token can look present."
        )
    if "final_notice" in intents:
        rules.append(
            "For each final notice give vendor, subject, confidence, and whether a chased invoice is present. "
            "If the subject is a thank-you or a utility shutoff/disconnection, say this final notice is not an unpaid AP invoice."
        )
    if "missing_file" in intents:
        rules.append(
            "Quote total and the filename counts. Give fil_id, filename, and error_detail for the sample. Do not guess contents."
        )
    if "unreadable" in intents:
        rules.append(
            "Quote total and every by_error_detail count and error_detail. Sample amounts are null. Do not invent amounts."
        )
    if "attachment_read" in intents:
        rules.append(
            "If attachment status is ok and not inline, pull vendor, invoice number, amount, and dates from the text. "
            "If status is not ok, stop and quote error_detail. Do not invent fields that are absent from the text."
        )
    return "\n".join(f"- {rule}" for rule in rules)


def _money(amount: Any) -> str:
    if amount is None:
        return "null"
    return str(amount)


def _not_unpaid_ap_invoice(subject: str | None) -> bool:
    lowered = (subject or "").lower()
    return any(
        token in lowered
        for token in ("thank you", "disconnection", "disconnect", "shutoff", "shut-off", "shut off")
    )


def fact_lines(tool: str, payload: dict[str, Any]) -> list[str]:
    """Short lines the model can quote without scanning a large JSON blob."""
    if payload.get("error"):
        return [f"{tool} failed: {payload.get('code')} {payload.get('detail')}"]

    lines: list[str] = [f"Tool {tool}."]
    if "total" in payload:
        lines.append(
            f"total={payload.get('total')} returned={payload.get('returned')} truncated={payload.get('truncated')}"
        )
    if payload.get("found") is False:
        lines.append(
            f"found=false. Invoice {payload.get('invoice_number')} matched 0 rows. "
            "Say it is not in the extracted fields."
        )
    if payload.get("found") is True:
        lines.append(f"found=true for invoice {payload.get('invoice_number')}.")

    by_type = payload.get("by_doc_type") or {}
    if by_type and not payload.get("filters"):
        lines.append("Counts by document type: " + ", ".join(f"{key}={value}" for key, value in by_type.items()))
    by_status = payload.get("by_status") or {}
    if by_status and not payload.get("filters"):
        lines.append("Counts by status: " + ", ".join(f"{key}={value}" for key, value in by_status.items()))

    filters = payload.get("filters") or {}
    if filters:
        lines.append(
            "Filtered "
            + ", ".join(f"{key}={value}" for key, value in filters.items() if value)
            + f" total={payload.get('total')}."
        )

    histogram = payload.get("rationale_histogram") or []
    if histogram and (not filters.get("doc_type") or filters.get("status") in {"low_confidence", "error"}):
        for row in histogram[:8]:
            lines.append(
                "Histogram "
                f"status={row.get('status')} doc_type={row.get('doc_type')} "
                f"confidence={row.get('confidence')} count={row.get('count')} "
                f"rationale={row.get('rationale')!r} error_detail={row.get('error_detail')!r}"
            )

    if payload.get("stage_counts") is not None:
        counts = payload.get("stage_counts") or {}
        lines.append("stage_counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if payload.get("match_policy"):
        lines.append(f"match_policy: {payload.get('match_policy')}")

    for row in payload.get("by_error_detail") or []:
        lines.append(f"error_detail count={row.get('count')}: {row.get('error_detail')}")

    for row in payload.get("by_filename") or []:
        lines.append(f"filename count={row.get('count')}: {row.get('filename')}")

    if tool == "get_annotations":
        for item in payload.get("items") or []:
            lines.append(
                f"sample message_id={item.get('message_id')} doc_type={item.get('doc_type')} "
                f"status={item.get('status')} confidence={item.get('confidence')} "
                f"subject={item.get('subject')!r} rationale={item.get('rationale')!r}"
            )
    elif tool == "get_invoice_fields":
        for item in payload.get("items") or []:
            lines.append(
                f"invoice message_id={item.get('message_id')} vendor={item.get('vendor')!r} "
                f"number={item.get('invoice_number')!r} amount={_money(item.get('amount'))} "
                f"currency={item.get('currency')} invoice_date={item.get('invoice_date')} "
                f"due_date={item.get('due_date')} confidence={item.get('overall_confidence')} "
                f"status={item.get('status')} error_detail={item.get('error_detail')!r}"
            )
    elif tool == "get_chase_events":
        for item in payload.get("items") or []:
            refs = item.get("referenced_invoice_numbers")
            lines.append(
                f"chase message_id={item.get('message_id')} vendor={item.get('vendor')!r} "
                f"stage={item.get('stage')} subject={item.get('subject')!r} "
                f"referenced_invoice_numbers={refs} present={item.get('invoice_present_in_inbox')} "
                f"confidence={item.get('confidence')}"
            )
            if refs in ([], None):
                lines.append("No invoice number was extracted for this chase row.")
            if _not_unpaid_ap_invoice(item.get("subject")):
                lines.append(
                    "This final notice is not an unpaid AP invoice: the subject is a thank-you or a utility shutoff."
                )
            if refs not in ([], None) and _not_unpaid_ap_invoice(item.get("subject")):
                lines.append(
                    "The referenced token is not a reliable invoice number. "
                    "present=true here is only an exact token match, not proof the invoice is in the inbox."
                )
    elif tool == "list_attachment_meta":
        if payload.get("fil_id") and not payload.get("by_filename"):
            lines.append(
                f"fil_id={payload.get('fil_id')} count={payload.get('count')}"
            )
        for item in (payload.get("items") or [])[:8]:
            lines.append(
                f"attachment fil_id={item.get('fil_id')} filename={item.get('filename')!r} "
                f"status={item.get('status')} inline={item.get('is_inline')} "
                f"message_id={item.get('message_id')} error_detail={item.get('error_detail')!r}"
            )
    elif tool == "fetch_attachment_text":
        lines.append(
            f"Readable text for fil_id={payload.get('fil_id')} filename={payload.get('filename')!r} "
            f"message_id={payload.get('message_id')} source={payload.get('source')}. "
            "Pull vendor, invoice number, amount, and dates only from this text."
        )
    return lines
