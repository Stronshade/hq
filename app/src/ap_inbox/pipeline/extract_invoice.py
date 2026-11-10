"""Best-effort invoice field extraction from email text and PDF attachments."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass
class InvoiceExtraction:
    vendor: str | None = None
    invoice_number: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    po_number: str | None = None
    field_confidence: dict[str, float] = field(default_factory=dict)
    overall_confidence: Decimal = Decimal("0")
    status: str = "ok"
    error_detail: str | None = None
    raw_excerpt: str | None = None
    attachment_fil_id: str | None = None


_INV_NUM = re.compile(
    r"(?:invoice\s*(?:#|no\.?|number)?[:\s#-]*|inv[#\s\-]*)([A-Z0-9][A-Z0-9\-_/]{2,})",
    re.I,
)
_PO = re.compile(r"(?:po|p\.o\.|purchase\s+order)\s*[#:.\-]*\s*([A-Z0-9][A-Z0-9\-_/]{2,})", re.I)
_AMOUNT = re.compile(
    r"(?:total\s*(?:due|amount)?|amount\s*due|balance\s*due|grand\s*total)\s*[:\s]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+\.[0-9]{2})",
    re.I,
)
_DATE = re.compile(
    r"(?:invoice\s*date|date\s*issued|due\s*date)\s*[:\s]*([0-9]{1,2}[/\-][0-9]{1,2}[/\-][0-9]{2,4}|[A-Za-z]{3,9}\s+[0-9]{1,2},?\s+[0-9]{4})",
    re.I,
)
_VENDOR_FROM = re.compile(r"^from:\s*(.+)$", re.I | re.M)
_INVOICE_HINT = re.compile(
    r"\b(invoice|tax\s+invoice|amount\s+due|bill\s+to|remit|statement\s+of\s+account|payment\s+reminder|final\s+notice)\b",
    re.I,
)

_INVOICE_LIKE_TYPES = {
    "invoice",
    "statement",
    "payment_reminder",
    "remittance",
    "escalation",
    "final_notice",
}


def _parse_amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw.replace(",", ""), fmt.replace(",", "")).date() if "," not in fmt else datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # retry without comma normalization edge cases
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw.replace(",", ""), fmt).date()
        except ValueError:
            continue
    return None


def extract_text_from_pdf(path: Path) -> tuple[str | None, str | None]:
    """Return (text, error). Explicit failure — never silent."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        chunks: list[str] = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception as page_exc:  # noqa: BLE001
                return None, f"pdf page extract failed: {page_exc}"
        text = "\n".join(chunks).strip()
        if not text:
            return None, "pdf text extract returned empty (likely scanned / image-only)"
        return text, None
    except Exception as exc:  # noqa: BLE001
        return None, f"pdf unreadable: {exc}"


def looks_invoice_related(
    *,
    doc_type: str | None = None,
    text: str | None = None,
    attachment_doc_types: list[str] | None = None,
) -> bool:
    if doc_type and doc_type in _INVOICE_LIKE_TYPES:
        return True
    if attachment_doc_types and any(t in _INVOICE_LIKE_TYPES for t in attachment_doc_types):
        return True
    if text and _INVOICE_HINT.search(text):
        return True
    return False


def needs_llm_extract(ext: InvoiceExtraction, *, invoice_like: bool) -> bool:
    if not invoice_like:
        return False
    if ext.status in ("unreadable", "skipped_inline", "error"):
        return False
    if not (ext.raw_excerpt or "").strip():
        return False
    if ext.invoice_number is None or ext.amount is None:
        return True
    if ext.overall_confidence < Decimal("0.40"):
        return True
    return False


def merge_extraction(base: InvoiceExtraction, llm_fields: Any) -> InvoiceExtraction:
    """Fill blanks from LLM; keep regex values when their field confidence is higher."""
    from ap_inbox.pipeline.llm_refine import LlmExtractResult

    if not isinstance(llm_fields, LlmExtractResult) or not llm_fields.ok:
        return base

    llm_conf = llm_fields.field_confidence or {}
    base_conf = dict(base.field_confidence or {})
    out = InvoiceExtraction(
        vendor=base.vendor,
        invoice_number=base.invoice_number,
        amount=base.amount,
        currency=base.currency,
        invoice_date=base.invoice_date,
        due_date=base.due_date,
        po_number=base.po_number,
        field_confidence=base_conf,
        overall_confidence=base.overall_confidence,
        status=base.status,
        error_detail=base.error_detail,
        raw_excerpt=base.raw_excerpt,
        attachment_fil_id=base.attachment_fil_id,
    )

    def _take(field_name: str, base_val: Any, llm_val: Any) -> Any:
        if llm_val is None or llm_val == "":
            return base_val
        if base_val is None or base_val == "":
            base_conf[field_name] = float(llm_conf.get(field_name, 0.55))
            return llm_val
        base_c = float(base_conf.get(field_name, 0.0))
        llm_c = float(llm_conf.get(field_name, 0.0))
        if llm_c > base_c:
            base_conf[field_name] = llm_c
            return llm_val
        return base_val

    out.vendor = _take("vendor", out.vendor, llm_fields.vendor)
    out.invoice_number = _take("invoice_number", out.invoice_number, llm_fields.invoice_number)
    out.amount = _take("amount", out.amount, llm_fields.amount)
    out.currency = _take("currency", out.currency, llm_fields.currency)
    out.invoice_date = _take("invoice_date", out.invoice_date, llm_fields.invoice_date)
    out.due_date = _take("due_date", out.due_date, llm_fields.due_date)
    out.po_number = _take("po_number", out.po_number, llm_fields.po_number)
    out.field_confidence = base_conf

    if base_conf:
        out.overall_confidence = Decimal(str(round(sum(base_conf.values()) / len(base_conf), 4)))
    elif llm_fields.overall_confidence > out.overall_confidence:
        out.overall_confidence = llm_fields.overall_confidence

    if out.invoice_number or out.amount:
        if out.status in ("low_confidence", "ok") and out.overall_confidence >= Decimal("0.40"):
            out.status = "ok"
            out.error_detail = None
        elif out.status == "ok" and out.overall_confidence < Decimal("0.40"):
            out.status = "low_confidence"
    return out


def maybe_refine_extraction(
    ext: InvoiceExtraction,
    *,
    text: str,
    vendor_hint: str | None,
    invoice_like: bool,
    use_llm: bool,
) -> InvoiceExtraction:
    if not use_llm or not needs_llm_extract(ext, invoice_like=invoice_like):
        return ext
    if not (text or "").strip():
        return ext

    from ap_inbox.pipeline.llm_refine import llm_extract_invoice_fields

    llm = llm_extract_invoice_fields(text=text, vendor_hint=vendor_hint)
    if not llm.ok:
        # Keep regex result; annotate failure without inventing fields.
        detail = f"llm extract failed: {llm.error}"
        if ext.error_detail:
            detail = f"{ext.error_detail}; {detail}"
        return InvoiceExtraction(
            vendor=ext.vendor,
            invoice_number=ext.invoice_number,
            amount=ext.amount,
            currency=ext.currency,
            invoice_date=ext.invoice_date,
            due_date=ext.due_date,
            po_number=ext.po_number,
            field_confidence=ext.field_confidence,
            overall_confidence=ext.overall_confidence,
            status=ext.status,
            error_detail=detail,
            raw_excerpt=ext.raw_excerpt,
            attachment_fil_id=ext.attachment_fil_id,
        )
    return merge_extraction(ext, llm)


def extract_fields_from_text(
    text: str,
    *,
    vendor_hint: str | None = None,
    attachment_fil_id: str | None = None,
) -> InvoiceExtraction:
    try:
        conf: dict[str, float] = {}
        result = InvoiceExtraction(attachment_fil_id=attachment_fil_id, raw_excerpt=text[:1500])

        if vendor_hint:
            result.vendor = vendor_hint.strip()[:512]
            conf["vendor"] = 0.55
        else:
            m = _VENDOR_FROM.search(text)
            if m:
                result.vendor = m.group(1).strip()[:512]
                conf["vendor"] = 0.45

        m = _INV_NUM.search(text)
        if m:
            result.invoice_number = m.group(1).strip()[:256]
            conf["invoice_number"] = 0.75

        m = _PO.search(text)
        if m:
            result.po_number = m.group(1).strip()[:256]
            conf["po_number"] = 0.70

        m = _AMOUNT.search(text)
        if m:
            result.amount = _parse_amount(m.group(1))
            result.currency = "USD"
            conf["amount"] = 0.70 if result.amount is not None else 0.2
            conf["currency"] = 0.60

        # Prefer labeled dates
        for m in _DATE.finditer(text):
            label = m.group(0).lower()
            parsed = _parse_date(m.group(1))
            if not parsed:
                continue
            if "due" in label:
                result.due_date = parsed
                conf["due_date"] = 0.65
            else:
                result.invoice_date = parsed
                conf["invoice_date"] = 0.65

        result.field_confidence = conf
        if conf:
            result.overall_confidence = Decimal(str(round(sum(conf.values()) / len(conf), 4)))
        else:
            result.overall_confidence = Decimal("0.15")
            result.status = "low_confidence"
            result.error_detail = "no invoice fields matched"

        if result.overall_confidence < Decimal("0.40") and result.status == "ok":
            result.status = "low_confidence"
        return result
    except Exception as exc:  # noqa: BLE001
        return InvoiceExtraction(
            status="error",
            error_detail=str(exc),
            overall_confidence=Decimal("0"),
            attachment_fil_id=attachment_fil_id,
        )


def extract_from_message(
    *,
    message_id: uuid.UUID,
    subject: str | None,
    body: str | None,
    author_email: str | None,
    from_name: str | None,
    attachment_paths: list[tuple[str, Path]],
    doc_type: str | None = None,
    attachment_doc_types: dict[str, str] | None = None,
    use_llm: bool = False,
) -> list[InvoiceExtraction]:
    """Extract from body and each readable attachment. Gaps become status rows, not silent skips."""
    _ = message_id
    vendor_hint = from_name or (author_email.split("@")[0] if author_email else None)
    results: list[InvoiceExtraction] = []
    att_types = list((attachment_doc_types or {}).values())

    email_text = f"{subject or ''}\n{body or ''}"
    email_ext = extract_fields_from_text(email_text, vendor_hint=vendor_hint)
    email_like = looks_invoice_related(doc_type=doc_type, text=email_text, attachment_doc_types=att_types)
    results.append(
        maybe_refine_extraction(
            email_ext,
            text=email_text,
            vendor_hint=vendor_hint,
            invoice_like=email_like,
            use_llm=use_llm,
        )
    )

    for fil_id, path in attachment_paths:
        att_doc = (attachment_doc_types or {}).get(fil_id)
        if path.suffix.lower() == ".pdf":
            text, err = extract_text_from_pdf(path)
            if err or not text:
                results.append(
                    InvoiceExtraction(
                        attachment_fil_id=fil_id,
                        status="unreadable",
                        error_detail=err or "empty pdf text",
                        overall_confidence=Decimal("0"),
                    )
                )
            else:
                base = extract_fields_from_text(text, vendor_hint=vendor_hint, attachment_fil_id=fil_id)
                att_like = looks_invoice_related(
                    doc_type=att_doc or doc_type,
                    text=text,
                    attachment_doc_types=att_types,
                )
                results.append(
                    maybe_refine_extraction(
                        base,
                        text=text,
                        vendor_hint=vendor_hint,
                        invoice_like=att_like,
                        use_llm=use_llm,
                    )
                )
        else:
            # Non-PDF: try utf-8 text files; otherwise mark skipped/low
            try:
                if path.suffix.lower() in {".txt", ".csv", ".html", ".htm"}:
                    text = path.read_text(encoding="utf-8", errors="replace")[:20000]
                    base = extract_fields_from_text(text, vendor_hint=vendor_hint, attachment_fil_id=fil_id)
                    att_like = looks_invoice_related(
                        doc_type=att_doc or doc_type,
                        text=text,
                        attachment_doc_types=att_types,
                    )
                    results.append(
                        maybe_refine_extraction(
                            base,
                            text=text,
                            vendor_hint=vendor_hint,
                            invoice_like=att_like,
                            use_llm=use_llm,
                        )
                    )
                else:
                    results.append(
                        InvoiceExtraction(
                            attachment_fil_id=fil_id,
                            status="skipped_inline" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"} else "low_confidence",
                            error_detail=f"no extractor for {path.suffix or 'unknown'} file type",
                            overall_confidence=Decimal("0.10"),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    InvoiceExtraction(
                        attachment_fil_id=fil_id,
                        status="unreadable",
                        error_detail=str(exc),
                        overall_confidence=Decimal("0"),
                    )
                )
    return results


def extraction_to_dict(ext: InvoiceExtraction) -> dict[str, Any]:
    return {
        "vendor": ext.vendor,
        "invoice_number": ext.invoice_number,
        "amount": str(ext.amount) if ext.amount is not None else None,
        "currency": ext.currency,
        "invoice_date": ext.invoice_date.isoformat() if ext.invoice_date else None,
        "due_date": ext.due_date.isoformat() if ext.due_date else None,
        "po_number": ext.po_number,
        "field_confidence": ext.field_confidence,
        "overall_confidence": str(ext.overall_confidence),
        "status": ext.status,
        "error_detail": ext.error_detail,
        "attachment_fil_id": ext.attachment_fil_id,
    }
