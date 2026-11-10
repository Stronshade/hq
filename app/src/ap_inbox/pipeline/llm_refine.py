"""Sync Ollama JSON helpers for hybrid classify + invoice extract refine.

Failures are structured — never silent. Callers keep rules/regex results on error.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ap_inbox.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Soft truncate for refine prompts (~6–8k chars), capped by LLM_MAX_ATTACHMENT_CHARS.
_DEFAULT_REFINE_CHARS = 7000


@dataclass
class LlmClassifyResult:
    ok: bool
    doc_type: str = "other"
    confidence: Decimal = Decimal("0")
    rationale: str = ""
    model: str = ""
    error: str | None = None


@dataclass
class LlmExtractResult:
    ok: bool
    vendor: str | None = None
    invoice_number: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    po_number: str | None = None
    field_confidence: dict[str, float] | None = None
    overall_confidence: Decimal = Decimal("0")
    model: str = ""
    error: str | None = None


def _truncate(text: str, settings: Settings) -> str:
    cap = min(int(settings.llm_max_attachment_chars or 20000), _DEFAULT_REFINE_CHARS)
    if len(text) <= cap:
        return text
    return text[:cap]


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty LLM response")
    # Strip common markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def _chat_json(
    *,
    system: str,
    user: str,
    settings: Settings | None = None,
    timeout: float = 600.0,
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Return (parsed_dict|None, model, error|None)."""
    s = settings or get_settings()
    model = s.chat_model
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "format": "json",
        # Qwen3 "thinking" balloons latency on CPU; disable for batch JSON refine.
        "think": False,
        "options": {
            "num_ctx": min(int(s.llm_num_ctx or 16384), 8192),
            "temperature": 0.1,
            "num_predict": 512,
        },
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        # Connect timeout short; read timeout long (14b on CPU can exceed several minutes).
        timeout_cfg = httpx.Timeout(timeout, connect=30.0)
        with httpx.Client(base_url=s.ollama_host, timeout=timeout_cfg) as client:
            resp = client.post("/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
        content = ""
        msg = body.get("message") if isinstance(body, dict) else None
        if isinstance(msg, dict):
            content = msg.get("content") or ""
        parsed = _extract_json_object(content)
        return parsed, model, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_refine chat failed: %s", exc)
        return None, model, str(exc)


def _clamp_confidence(raw: Any, default: Decimal = Decimal("0.5")) -> Decimal:
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if val < 0:
        return Decimal("0")
    if val > 1:
        return Decimal("1")
    return val.quantize(Decimal("0.0001"))


def llm_classify_text(
    *,
    subject: str | None = None,
    body: str | None = None,
    filenames: list[str] | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    text_excerpt: str | None = None,
    settings: Settings | None = None,
) -> LlmClassifyResult:
    """Classify message or attachment text into DOC_TYPES via Ollama JSON."""
    from ap_inbox.pipeline.classify import DOC_TYPES

    s = settings or get_settings()
    types_csv = ", ".join(DOC_TYPES)
    system = (
        "You classify accounts-payable email/documents. "
        f"Respond with ONLY a JSON object: "
        f'{{"doc_type": one of [{types_csv}], "confidence": 0..1 number, "rationale": short string}}. '
        "Never invent content that is not supported by the text. If unclear, use doc_type other with low confidence."
    )
    parts = [
        f"subject: {subject or ''}",
        f"filenames: {', '.join(filenames or [])}",
        f"filename: {filename or ''}",
        f"content_type: {content_type or ''}",
        "text:",
        _truncate((body or "") + "\n" + (text_excerpt or ""), s),
    ]
    user = "\n".join(parts)
    data, model, err = _chat_json(system=system, user=user, settings=s)
    if err or data is None:
        return LlmClassifyResult(ok=False, model=model, error=err or "no parse")

    doc_type = str(data.get("doc_type") or "other").strip().lower()
    if doc_type not in DOC_TYPES:
        return LlmClassifyResult(
            ok=False,
            model=model,
            error=f"invalid doc_type from LLM: {doc_type!r}",
        )
    return LlmClassifyResult(
        ok=True,
        doc_type=doc_type,
        confidence=_clamp_confidence(data.get("confidence")),
        rationale=str(data.get("rationale") or "llm classify")[:2000],
        model=model,
    )


def _parse_llm_date(raw: Any) -> date | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_llm_amount(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if not text or text.lower() in {"null", "none", "n/a"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def llm_extract_invoice_fields(
    *,
    text: str,
    vendor_hint: str | None = None,
    settings: Settings | None = None,
) -> LlmExtractResult:
    """Extract invoice fields as JSON. Caller must not pass empty/unreadable text."""
    s = settings or get_settings()
    clipped = _truncate(text or "", s)
    if not clipped.strip():
        return LlmExtractResult(ok=False, error="refused: empty text (would invent fields)", model=s.chat_model)

    system = (
        "You extract invoice fields from AP email/PDF text. "
        "Respond with ONLY a JSON object with keys: "
        "vendor, invoice_number, amount, currency, invoice_date, due_date, po_number, "
        "field_confidence (object of field->0..1), overall_confidence (0..1). "
        "Use null for unknown fields. Dates as YYYY-MM-DD. Amount as number. "
        "Do not invent values that are not present in the text."
    )
    user = f"vendor_hint: {vendor_hint or ''}\n\ntext:\n{clipped}"
    data, model, err = _chat_json(system=system, user=user, settings=s)
    if err or data is None:
        return LlmExtractResult(ok=False, model=model, error=err or "no parse")

    field_conf_raw = data.get("field_confidence") or {}
    field_confidence: dict[str, float] = {}
    if isinstance(field_conf_raw, dict):
        for k, v in field_conf_raw.items():
            try:
                field_confidence[str(k)] = float(_clamp_confidence(v))
            except (TypeError, ValueError):
                continue

    overall = data.get("overall_confidence")
    if overall is None and field_confidence:
        overall = sum(field_confidence.values()) / len(field_confidence)

    vendor = data.get("vendor")
    invoice_number = data.get("invoice_number")
    currency = data.get("currency")
    po_number = data.get("po_number")

    return LlmExtractResult(
        ok=True,
        vendor=str(vendor).strip()[:512] if vendor not in (None, "") else None,
        invoice_number=str(invoice_number).strip()[:256] if invoice_number not in (None, "") else None,
        amount=_parse_llm_amount(data.get("amount")),
        currency=str(currency).strip()[:16] if currency not in (None, "") else None,
        invoice_date=_parse_llm_date(data.get("invoice_date")),
        due_date=_parse_llm_date(data.get("due_date")),
        po_number=str(po_number).strip()[:256] if po_number not in (None, "") else None,
        field_confidence=field_confidence,
        overall_confidence=_clamp_confidence(overall, default=Decimal("0.4")),
        model=model,
    )
