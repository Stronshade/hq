"""Heuristic + optional LLM-assisted document type classification."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

DOC_TYPES = (
    "invoice",
    "statement",
    "payment_reminder",
    "remittance",
    "escalation",
    "final_notice",
    "other",
)

# Escalate weak "other" when confidence is below this.
_WEAK_OTHER_CONF = Decimal("0.60")


@dataclass
class ClassificationResult:
    doc_type: str
    confidence: Decimal
    rationale: str
    model: str
    status: str = "ok"
    error_detail: str | None = None


_RULES: list[tuple[str, re.Pattern[str], Decimal, str]] = [
    ("final_notice", re.compile(r"\b(final\s+notice|final\s+demand|collections?\s+notice)\b", re.I), Decimal("0.92"), "final notice keywords"),
    ("escalation", re.compile(r"\b(escalat|past\s+due|overdue|second\s+notice|third\s+notice)\b", re.I), Decimal("0.85"), "escalation keywords"),
    ("payment_reminder", re.compile(r"\b(payment\s+reminder|friendly\s+reminder|reminder:\s*invoice|please\s+remit)\b", re.I), Decimal("0.88"), "reminder keywords"),
    ("remittance", re.compile(r"\b(remittance|payment\s+advice|paid\s+invoice|payment\s+confirmation)\b", re.I), Decimal("0.84"), "remittance keywords"),
    ("statement", re.compile(r"\b(account\s+statement|statement\s+of\s+account|aging\s+report)\b", re.I), Decimal("0.86"), "statement keywords"),
    ("invoice", re.compile(r"\b(invoice|tax\s+invoice|sales\s+invoice|inv[#\s\-]|bill\s+to)\b", re.I), Decimal("0.80"), "invoice keywords"),
]


def needs_llm_classify(result: ClassificationResult) -> bool:
    if result.status in ("low_confidence", "error"):
        return True
    if result.doc_type == "other" and result.confidence < _WEAK_OTHER_CONF:
        return True
    return False


def classify_text(subject: str | None, body: str | None, filenames: list[str] | None = None) -> ClassificationResult:
    try:
        blob = " ".join(
            part
            for part in [
                subject or "",
                (body or "")[:8000],
                " ".join(filenames or []),
            ]
            if part
        )
        if not blob.strip():
            return ClassificationResult(
                doc_type="other",
                confidence=Decimal("0.20"),
                rationale="empty subject/body/filenames",
                model="rules-v1",
                status="low_confidence",
                error_detail="insufficient text to classify",
            )

        best: ClassificationResult | None = None
        for doc_type, pattern, conf, why in _RULES:
            if pattern.search(blob):
                candidate = ClassificationResult(
                    doc_type=doc_type,
                    confidence=conf,
                    rationale=why,
                    model="rules-v1",
                )
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        if best is None:
            return ClassificationResult(
                doc_type="other",
                confidence=Decimal("0.35"),
                rationale="no keyword rule matched",
                model="rules-v1",
                status="low_confidence",
            )
        if best.confidence < Decimal("0.50"):
            best.status = "low_confidence"
        return best
    except Exception as exc:  # noqa: BLE001
        return ClassificationResult(
            doc_type="other",
            confidence=Decimal("0"),
            rationale="classification failed",
            model="rules-v1",
            status="error",
            error_detail=str(exc),
        )


def classify_text_hybrid(
    subject: str | None,
    body: str | None,
    filenames: list[str] | None = None,
    *,
    use_llm: bool = True,
) -> ClassificationResult:
    """Rules first; escalate to Qwen only for low_confidence / weak other."""
    rules = classify_text(subject, body, filenames)
    if not use_llm or not needs_llm_classify(rules):
        return rules

    from ap_inbox.pipeline.llm_refine import llm_classify_text

    llm = llm_classify_text(subject=subject, body=body, filenames=filenames)
    if not llm.ok:
        # Keep rules baseline; surface LLM failure only if rules already erred.
        if rules.status == "error":
            return ClassificationResult(
                doc_type=rules.doc_type,
                confidence=rules.confidence,
                rationale=rules.rationale,
                model=rules.model,
                status="error",
                error_detail=f"{rules.error_detail or 'rules error'}; llm: {llm.error}",
            )
        return ClassificationResult(
            doc_type=rules.doc_type,
            confidence=rules.confidence,
            rationale=f"{rules.rationale} (llm refine failed: {llm.error})",
            model=rules.model,
            status=rules.status,
            error_detail=rules.error_detail,
        )

    status = "ok"
    if llm.confidence < Decimal("0.50"):
        status = "low_confidence"
    return ClassificationResult(
        doc_type=llm.doc_type,
        confidence=llm.confidence,
        rationale=llm.rationale,
        model=llm.model or "llm",
        status=status,
    )


def classify_attachment(
    fil_id: str,
    filename: str | None,
    content_type: str | None,
    text_excerpt: str | None,
    *,
    use_llm: bool = True,
) -> ClassificationResult:
    """Per-attachment classify from filename + optional PDF/text excerpt."""
    _ = fil_id
    names = [filename] if filename else []
    rules = classify_text(subject=filename, body=text_excerpt, filenames=names)
    # Content-type hint for PDFs named generically
    if rules.doc_type == "other" and content_type and "pdf" in content_type.lower():
        if filename and re.search(r"inv|invoice|bill|statement", filename, re.I):
            rules = ClassificationResult(
                doc_type="invoice" if not re.search(r"statement", filename or "", re.I) else "statement",
                confidence=Decimal("0.62"),
                rationale="filename+content_type hint",
                model="rules-v1",
            )

    if not use_llm or not needs_llm_classify(rules):
        return rules

    from ap_inbox.pipeline.llm_refine import llm_classify_text

    llm = llm_classify_text(
        filename=filename,
        content_type=content_type,
        text_excerpt=text_excerpt,
        filenames=names,
    )
    if not llm.ok:
        return ClassificationResult(
            doc_type=rules.doc_type,
            confidence=rules.confidence,
            rationale=f"{rules.rationale} (llm refine failed: {llm.error})",
            model=rules.model,
            status=rules.status,
            error_detail=rules.error_detail,
        )

    status = "ok"
    if llm.confidence < Decimal("0.50"):
        status = "low_confidence"
    return ClassificationResult(
        doc_type=llm.doc_type,
        confidence=llm.confidence,
        rationale=llm.rationale,
        model=llm.model or "llm",
        status=status,
    )


def classify_message(
    message_id: uuid.UUID,
    subject: str | None,
    body: str | None,
    filenames: list[str] | None = None,
    *,
    use_llm: bool = False,
) -> ClassificationResult:
    _ = message_id
    if use_llm:
        return classify_text_hybrid(subject, body, filenames, use_llm=True)
    return classify_text(subject, body, filenames)
