"""Detect vendor chase / reminder / escalation / final-notice threads."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from ap_inbox.pipeline.classify import ClassificationResult
from ap_inbox.pipeline.extract_invoice import _INV_NUM


CHASE_STAGES = ("reminder", "escalation", "final_notice")


@dataclass
class ChaseDetection:
    vendor: str | None
    stage: str
    referenced_invoice_numbers: list[str] = field(default_factory=list)
    invoice_present_in_inbox: bool = False
    confidence: Decimal = Decimal("0")
    rationale: str | None = None
    status: str = "ok"
    error_detail: str | None = None


_STAGE_MAP = {
    "payment_reminder": "reminder",
    "escalation": "escalation",
    "final_notice": "final_notice",
}


def detect_chase(
    *,
    classification: ClassificationResult,
    subject: str | None,
    body: str | None,
    vendor_hint: str | None,
    known_invoice_numbers: set[str],
) -> ChaseDetection | None:
    """Return a chase event if message looks like a chase; else None."""
    try:
        stage = _STAGE_MAP.get(classification.doc_type)
        if stage is None:
            # Secondary keyword pass for borderline "other"
            blob = f"{subject or ''}\n{(body or '')[:4000]}"
            if re.search(r"\b(final\s+notice|final\s+demand)\b", blob, re.I):
                stage = "final_notice"
            elif re.search(r"\b(escalat|past\s+due|overdue)\b", blob, re.I):
                stage = "escalation"
            elif re.search(r"\b(payment\s+reminder|friendly\s+reminder|please\s+remit)\b", blob, re.I):
                stage = "reminder"
            else:
                return None

        blob = f"{subject or ''}\n{(body or '')[:8000]}"
        invs = list({m.group(1).strip() for m in _INV_NUM.finditer(blob)})
        present = any(inv.upper() in {k.upper() for k in known_invoice_numbers} for inv in invs)

        conf = max(classification.confidence, Decimal("0.55"))
        return ChaseDetection(
            vendor=vendor_hint,
            stage=stage,
            referenced_invoice_numbers=invs,
            invoice_present_in_inbox=present,
            confidence=conf,
            rationale=f"classified as {classification.doc_type}; invoices={invs or 'none'}; present={present}",
            status="ok" if conf >= Decimal("0.50") else "low_confidence",
        )
    except Exception as exc:  # noqa: BLE001
        return ChaseDetection(
            vendor=vendor_hint,
            stage="reminder",
            confidence=Decimal("0"),
            status="error",
            error_detail=str(exc),
            rationale="chase detection failed",
        )


def message_id_key(message_id: uuid.UUID) -> str:
    return str(message_id)
