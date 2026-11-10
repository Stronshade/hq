"""Read-only query helpers for the internal tool gateway.

Chat / Orchestrator must never mutate agent.*; DenoteWorker owns writes.
LLM never receives filesystem paths — only fil_id-scoped metadata and text.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ap_inbox.chunking import chunk_text
from ap_inbox.config import get_settings
from ap_inbox.embeddings import cosine_similarity, embed_query
from ap_inbox.pipeline.extract_invoice import extract_text_from_pdf


class ToolError(Exception):
    """Structured tool failure — never an empty success."""

    def __init__(self, code: str, detail: str, *, fil_id: str | None = None, message_id: str | None = None):
        self.code = code
        self.detail = detail
        self.fil_id = fil_id
        self.message_id = message_id
        super().__init__(f"{code}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code,
            "detail": self.detail,
            "fil_id": self.fil_id,
            "message_id": self.message_id,
        }


def _safe_fil_id(fil_id: str) -> str:
    """Reject path traversal; only accept Front-style fil_* ids."""
    cleaned = (fil_id or "").strip()
    if not re.fullmatch(r"fil_[A-Za-z0-9]+", cleaned):
        raise ToolError("tool_denied", f"invalid fil_id: {fil_id!r}")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ToolError("tool_denied", "path traversal denied")
    return cleaned


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "what",
        "which",
        "where",
        "when",
        "have",
        "about",
        "invoice",
        "invoices",
        "show",
        "key",
        "fields",
        "field",
        "vendor",
        "amount",
        "currency",
        "date",
        "due",
        "confidence",
        "cite",
        "message",
        "number",
        "numbers",
        "that",
        "this",
        "your",
        "are",
        "was",
        "were",
        "not",
        "say",
        "missing",
        "please",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,}")
_DIGIT_RUN_RE = re.compile(r"\d{6,}")
_MESSAGE_FIELDS = [
    "m.subject",
    "m.body_text",
    "m.blurb",
    "m.recipient_from_name",
    "a.doc_type",
    "i.vendor",
    "i.invoice_number",
]
_INVOICE_NUMBER_SQL = "ltrim(regexp_replace(coalesce(i.invoice_number, ''), '\\D', '', 'g'), '0') = :{param}"
_INVOICE_FIELD_SQL = "ltrim(regexp_replace(coalesce(invoice_number, ''), '\\D', '', 'g'), '0') = :{param}"


def invoice_number_keys(number: str) -> list[str]:
    """Digit forms that should match, including one leading zero.

    ``853825322`` therefore also matches filename ``0853825322``.
    """
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 6:
        return []
    stripped = digits.lstrip("0") or "0"
    keys = [digits, stripped]
    if not digits.startswith("0"):
        keys.append("0" + digits)
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def number_matches_text(query_number: str, text_value: str) -> bool:
    """True when text contains the number, ignoring one leading zero."""
    if not query_number or not text_value:
        return False
    keys = invoice_number_keys(query_number) or [query_number]
    if any(key and key in text_value for key in keys):
        return True
    stripped = re.sub(r"\D", "", query_number).lstrip("0")
    if not stripped:
        return False
    return any(run.lstrip("0") == stripped for run in _DIGIT_RUN_RE.findall(text_value))


def query_tokens(q: str, *, word_limit: int = 8) -> tuple[list[str], list[str]]:
    """Significant words plus invoice-number keys.

    Digit runs of 6+ characters are always kept, including a leading-zero
    variant. Stopwords such as ``the`` / ``invoice`` are dropped so they
    cannot OR-match unrelated mail.
    """
    words: list[str] = []
    numbers: list[str] = []
    seen_words: set[str] = set()
    seen_numbers: set[str] = set()
    for tok in _TOKEN_RE.findall(q or ""):
        for run in _DIGIT_RUN_RE.findall(tok):
            for key in invoice_number_keys(run):
                if key not in seen_numbers:
                    seen_numbers.add(key)
                    numbers.append(key)
        if tok.isdigit():
            continue
        low = tok.lower()
        if low in _STOPWORDS or low in seen_words:
            continue
        seen_words.add(low)
        words.append(tok)
    return words[:word_limit], numbers


def _ilike_contains(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _number_groups(keys: list[str]) -> list[list[str]]:
    """OR variants of the same number; callers AND distinct numbers."""
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for key in keys:
        digits = re.sub(r"\D", "", key)
        if not digits:
            continue
        stripped = digits.lstrip("0") or "0"
        if stripped not in groups:
            groups[stripped] = []
            order.append(stripped)
        if key not in groups[stripped]:
            groups[stripped].append(key)
    return [groups[stripped] for stripped in order]


def _filename_exists(param: str) -> str:
    return f"""(
      EXISTS (
        SELECT 1 FROM agent.attachment_status s
        WHERE s.message_id = m.id
          AND coalesce(s.filename, '') ILIKE :{param} ESCAPE '\\'
      )
      OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
          CASE
            WHEN jsonb_typeof(m.attachments) = 'array' THEN m.attachments
            ELSE '[]'::jsonb
          END
        ) elem
        WHERE coalesce(elem->>'filename', '') ILIKE :{param} ESCAPE '\\'
      )
    )"""


def _match_clause(
    params: dict[str, Any],
    *,
    param: str,
    value: str,
    fields: list[str],
    include_filenames: bool,
) -> str:
    params[param] = _ilike_contains(value)
    parts = [f"coalesce({field}, '') ILIKE :{param} ESCAPE '\\'" for field in fields]
    if include_filenames:
        parts.append(_filename_exists(param))
    return "(" + " OR ".join(parts) + ")"


def _query_where(
    params: dict[str, Any],
    words: list[str],
    numbers: list[str],
    *,
    fields: list[str],
    include_filenames: bool,
    numeric_sql: str | None = None,
) -> str:
    """AND invoice-number groups. Other words are one OR group, not stopwords."""
    clauses: list[str] = []
    for gi, group in enumerate(_number_groups(numbers)):
        ors = [
            _match_clause(
                params,
                param=f"n{gi}_{vi}",
                value=variant,
                fields=fields,
                include_filenames=include_filenames,
            )
            for vi, variant in enumerate(group)
        ]
        if numeric_sql:
            stripped = re.sub(r"\D", "", group[0]).lstrip("0") or "0"
            key = f"ns{gi}"
            params[key] = stripped
            ors.append(numeric_sql.format(param=key))
        clauses.append("(" + " OR ".join(ors) + ")")
    if words:
        word_ors = [
            _match_clause(
                params,
                param=f"w{i}",
                value=word,
                fields=fields,
                include_filenames=include_filenames,
            )
            for i, word in enumerate(words)
        ]
        clauses.append("(" + " OR ".join(word_ors) + ")")
    return " AND ".join(clauses) if clauses else "TRUE"


def _attachments_json_sql() -> str:
    return """
      CASE
        WHEN jsonb_typeof(m.attachments) = 'array' THEN m.attachments
        ELSE '[]'::jsonb
      END
    """


async def _attachments_by_message(
    session: AsyncSession, message_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Non-inline attachments for search hits. Status is included when denote scanned the file."""
    if not message_ids:
        return {}
    msg_rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS message_id, attachments
                FROM frontapp.frontapp_messages
                WHERE id::text = ANY(:ids)
                """
            ),
            {"ids": message_ids},
        )
    ).mappings().all()
    status_rows = (
        await session.execute(
            text(
                """
                SELECT fil_id, message_id::text AS message_id, filename, is_inline, status, error_detail
                FROM agent.attachment_status
                WHERE message_id::text = ANY(:ids)
                """
            ),
            {"ids": message_ids},
        )
    ).mappings().all()
    status_by_message: dict[str, dict[str, Any]] = {}
    for row in status_rows:
        status_by_message.setdefault(str(row["message_id"]), {})[row["fil_id"]] = row

    from ap_inbox.pipeline.attachments import parse_attachment_json

    out: dict[str, list[dict[str, Any]]] = {}
    for row in msg_rows:
        mid = str(row["message_id"])
        by_fil = status_by_message.get(mid, {})
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            refs = parse_attachment_json(row["attachments"], uuid.UUID(mid))
        except ValueError:
            refs = []
        for ref in refs:
            if ref.is_inline:
                continue
            seen.add(ref.fil_id)
            st = by_fil.get(ref.fil_id)
            items.append(
                {
                    "fil_id": ref.fil_id,
                    "filename": ref.filename,
                    "status": st["status"] if st else None,
                    "error_detail": st["error_detail"] if st else None,
                }
            )
        for fil_id, st in by_fil.items():
            if fil_id in seen or st["is_inline"]:
                continue
            items.append(
                {
                    "fil_id": fil_id,
                    "filename": st["filename"],
                    "status": st["status"],
                    "error_detail": st["error_detail"],
                }
            )
        out[mid] = items
    return out


async def _front_attachment_row(session: AsyncSession, fil_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT m.id AS message_id,
                       elem->>'filename' AS filename,
                       elem->>'content_type' AS content_type,
                       CASE
                         WHEN lower(coalesce(elem->'metadata'->>'is_inline', 'false'))
                              IN ('true', 't', '1')
                         THEN true
                         ELSE false
                       END AS is_inline
                FROM frontapp.frontapp_messages m
                CROSS JOIN LATERAL jsonb_array_elements(
                  CASE
                    WHEN jsonb_typeof(m.attachments) = 'array' THEN m.attachments
                    ELSE '[]'::jsonb
                  END
                ) elem
                WHERE elem->>'id' = :fid
                LIMIT 1
                """
            ),
            {"fid": fil_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _ref_from_front(fil_id: str, front: dict[str, Any]):
    from ap_inbox.pipeline.attachments import AttachmentRef

    mid = front["message_id"]
    if not isinstance(mid, uuid.UUID):
        mid = uuid.UUID(str(mid))
    return AttachmentRef(
        fil_id=fil_id,
        message_id=mid,
        filename=front.get("filename"),
        content_type=front.get("content_type"),
        is_inline=bool(front.get("is_inline")),
    )


async def search_messages(session: AsyncSession, q: str, *, limit: int = 12) -> dict[str, Any]:
    words, numbers = query_tokens(q or "")
    params: dict[str, Any] = {"limit": limit}
    where = _query_where(
        params,
        words,
        numbers,
        fields=_MESSAGE_FIELDS,
        include_filenames=True,
        numeric_sql=_INVOICE_NUMBER_SQL,
    )
    sql = f"""
        SELECT * FROM (
          SELECT DISTINCT ON (m.id)
            m.id AS message_id,
            m.subject,
            m.blurb,
            left(coalesce(m.body_text, ''), 800) AS body_excerpt,
            m.author_email,
            m.recipient_from_name AS from_name,
            a.doc_type,
            a.confidence AS annotation_confidence,
            m.message_created_at
          FROM frontapp.frontapp_messages m
          LEFT JOIN agent.message_annotations a ON a.message_id = m.id
          LEFT JOIN agent.invoice_fields i ON i.message_id = m.id
          WHERE {where}
          ORDER BY m.id, m.message_created_at DESC NULLS LAST
        ) hits
        ORDER BY message_created_at DESC NULLS LAST
        LIMIT :limit
    """
    rows = (await session.execute(text(sql), params)).mappings().all()
    attachments = await _attachments_by_message(session, [str(r["message_id"]) for r in rows])
    return {
        "count": len(rows),
        "items": [
            {
                "message_id": str(r["message_id"]),
                "subject": r["subject"],
                "blurb": r["blurb"],
                "body_excerpt": r["body_excerpt"],
                "author_email": r["author_email"],
                "from_name": r["from_name"],
                "doc_type": r["doc_type"],
                "annotation_confidence": float(r["annotation_confidence"])
                if r["annotation_confidence"] is not None
                else None,
                "attachments": attachments.get(str(r["message_id"]), []),
            }
            for r in rows
        ],
    }


async def get_annotations(session: AsyncSession, message_id: str) -> dict[str, Any]:
    try:
        mid = uuid.UUID(message_id)
    except ValueError as exc:
        raise ToolError("tool_denied", f"invalid message_id: {message_id}") from exc

    row = (
        await session.execute(
            text(
                """
                SELECT message_id, doc_type, confidence, rationale, model, status, error_detail
                FROM agent.message_annotations
                WHERE message_id = :mid
                """
            ),
            {"mid": mid},
        )
    ).mappings().first()
    if not row:
        return {"found": False, "message_id": message_id, "annotation": None}
    return {
        "found": True,
        "message_id": message_id,
        "annotation": {
            "doc_type": row["doc_type"],
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "rationale": row["rationale"],
            "model": row["model"],
            "status": row["status"],
            "error_detail": row["error_detail"],
        },
    }


def _invoice_item(row: Any, *, filename: str | None = None) -> dict[str, Any]:
    item = {
        "message_id": str(row["message_id"]),
        "attachment_fil_id": row["attachment_fil_id"],
        "vendor": row["vendor"],
        "invoice_number": row["invoice_number"],
        "amount": str(row["amount"]) if row["amount"] is not None else None,
        "currency": row["currency"],
        "invoice_date": row["invoice_date"].isoformat() if row["invoice_date"] else None,
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "po_number": row["po_number"],
        "overall_confidence": float(row["overall_confidence"]) if row["overall_confidence"] is not None else None,
        "status": row["status"],
        "error_detail": row["error_detail"],
    }
    if filename:
        item["filename"] = filename
    return item


async def _filename_invoice_hits(
    session: AsyncSession,
    invoice_number: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Messages whose attachment filename contains the number, even with no extracted fields."""
    _words, numbers = query_tokens(invoice_number)
    if not numbers:
        numbers = [invoice_number]
    params: dict[str, Any] = {"limit": limit}
    filename_ors: list[str] = []
    for gi, group in enumerate(_number_groups(numbers)):
        for vi, variant in enumerate(group):
            key = f"fn{gi}_{vi}"
            params[key] = _ilike_contains(variant)
            filename_ors.append(f"coalesce({{alias}}, '') ILIKE :{key} ESCAPE '\\'")
    if not filename_ors:
        return []
    json_clause = " OR ".join(part.format(alias="elem->>'filename'") for part in filename_ors)
    status_clause = " OR ".join(part.format(alias="s.filename") for part in filename_ors)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT message_id, fil_id, filename FROM (
                  SELECT m.id::text AS message_id,
                         elem->>'id' AS fil_id,
                         elem->>'filename' AS filename
                  FROM frontapp.frontapp_messages m
                  CROSS JOIN LATERAL jsonb_array_elements({_attachments_json_sql()}) elem
                  WHERE lower(coalesce(elem->'metadata'->>'is_inline', 'false'))
                        NOT IN ('true', 't', '1')
                    AND ({json_clause})
                  UNION
                  SELECT s.message_id::text AS message_id,
                         s.fil_id,
                         s.filename
                  FROM agent.attachment_status s
                  WHERE s.is_inline IS NOT TRUE
                    AND ({status_clause})
                ) hits
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        fil_id = row["fil_id"]
        if not fil_id:
            continue
        key = (str(row["message_id"]), str(fil_id))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            _invoice_item(
                {
                    "message_id": row["message_id"],
                    "attachment_fil_id": fil_id,
                    "vendor": None,
                    "invoice_number": None,
                    "amount": None,
                    "currency": None,
                    "invoice_date": None,
                    "due_date": None,
                    "po_number": None,
                    "overall_confidence": None,
                    "status": "not_extracted",
                    "error_detail": "invoice number matched an attachment filename; fields were not extracted",
                },
                filename=row["filename"],
            )
        )
    return items


async def get_invoice_fields(
    session: AsyncSession,
    *,
    vendor: str | None = None,
    invoice_number: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    # A provided invoice number is sufficient. Do not AND a vendor string that
    # would drop a filename hit whose extracted vendor is different.
    if invoice_number:
        _words, numbers = query_tokens(invoice_number)
        if not numbers:
            numbers = [invoice_number]
        where = _query_where(
            params,
            [],
            numbers,
            fields=["invoice_number"],
            include_filenames=False,
            numeric_sql=_INVOICE_FIELD_SQL,
        )
    else:
        where = "TRUE"
        if vendor:
            where = "vendor ILIKE :vendor ESCAPE '\\'"
            params["vendor"] = _ilike_contains(vendor)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT message_id, attachment_fil_id, vendor, invoice_number, amount, currency,
                       invoice_date, due_date, po_number, overall_confidence, status, error_detail
                FROM agent.invoice_fields
                WHERE {where}
                ORDER BY invoice_date DESC NULLS LAST, created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    items = [_invoice_item(r) for r in rows]
    if invoice_number and not items:
        items = await _filename_invoice_hits(session, invoice_number, limit=limit)
    return {"count": len(items), "items": items}


async def get_chase_events(
    session: AsyncSession,
    *,
    vendor: str | None = None,
    stage: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    where = ["1=1"]
    if vendor:
        where.append("vendor ILIKE :vendor")
        params["vendor"] = f"%{vendor}%"
    if stage:
        where.append("stage = :stage")
        params["stage"] = stage
    rows = (
        await session.execute(
            text(
                f"""
                SELECT message_id, vendor, stage, referenced_invoice_numbers,
                       invoice_present_in_inbox, confidence, rationale, status, error_detail
                FROM agent.vendor_chase_events
                WHERE {" AND ".join(where)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    return {
        "count": len(rows),
        "items": [
            {
                "message_id": str(r["message_id"]),
                "vendor": r["vendor"],
                "stage": r["stage"],
                "referenced_invoice_numbers": r["referenced_invoice_numbers"],
                "invoice_present_in_inbox": r["invoice_present_in_inbox"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "rationale": r["rationale"],
                "status": r["status"],
                "error_detail": r["error_detail"],
            }
            for r in rows
        ],
    }


async def list_attachment_meta(session: AsyncSession, fil_id: str) -> dict[str, Any]:
    fid = _safe_fil_id(fil_id)
    rows = (
        await session.execute(
            text(
                """
                SELECT fil_id, message_id, filename, content_type, is_inline, status, error_detail
                FROM agent.attachment_status
                WHERE fil_id = :fid
                """
            ),
            {"fid": fid},
        )
    ).mappings().all()
    if not rows:
        front = await _front_attachment_row(session, fid)
        if front is not None:
            from ap_inbox.pipeline.attachments import classify_disk_status

            ref = _ref_from_front(fid, front)
            status, _path, error = classify_disk_status(ref)
            rows = [
                {
                    "fil_id": fid,
                    "message_id": ref.message_id,
                    "filename": ref.filename,
                    "content_type": ref.content_type,
                    "is_inline": ref.is_inline,
                    "status": status,
                    "error_detail": error,
                }
            ]
    # Never expose path to the LLM / tool caller
    return {
        "fil_id": fid,
        "count": len(rows),
        "items": [
            {
                "fil_id": r["fil_id"],
                "message_id": str(r["message_id"]),
                "filename": r["filename"],
                "content_type": r["content_type"],
                "is_inline": r["is_inline"],
                "status": r["status"],
                "error_detail": r["error_detail"],
            }
            for r in rows
        ],
    }


async def fetch_attachment_text(session: AsyncSession, fil_id: str) -> dict[str, Any]:
    """AttachmentReader role: text only after meta says status=ok and not inline."""
    settings = get_settings()
    fid = _safe_fil_id(fil_id)
    row = (
        await session.execute(
            text(
                """
                SELECT fil_id, message_id, filename, content_type, is_inline, path, status, error_detail
                FROM agent.attachment_status
                WHERE fil_id = :fid
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"fid": fid},
        )
    ).mappings().first()
    if not row:
        front = await _front_attachment_row(session, fid)
        if front is None:
            raise ToolError("missing_file", f"no attachment_status row for {fid}", fil_id=fid)
        from ap_inbox.pipeline.attachments import classify_disk_status

        ref = _ref_from_front(fid, front)
        status, path_str, error = classify_disk_status(ref)
        row = {
            "fil_id": fid,
            "message_id": ref.message_id,
            "filename": ref.filename,
            "content_type": ref.content_type,
            "is_inline": ref.is_inline,
            "path": path_str,
            "status": status,
            "error_detail": error,
        }

    mid = str(row["message_id"])
    if row["is_inline"]:
        raise ToolError("tool_denied", "inline attachments are not readable via this tool", fil_id=fid, message_id=mid)
    if row["status"] == "missing_file":
        raise ToolError("missing_file", row["error_detail"] or "file missing on disk", fil_id=fid, message_id=mid)
    if row["status"] == "unreadable":
        raise ToolError("unreadable", row["error_detail"] or "file unreadable", fil_id=fid, message_id=mid)
    if row["status"] != "ok":
        raise ToolError(
            "tool_denied",
            f"attachment status={row['status']}: {row['error_detail'] or 'not readable'}",
            fil_id=fid,
            message_id=mid,
        )

    # Prefer precomputed chunks (no FS path leakage to caller)
    chunks = (
        await session.execute(
            text(
                """
                SELECT chunk_index, text, token_count
                FROM agent.document_chunks
                WHERE fil_id = :fid AND source_type = 'attachment_text'
                ORDER BY chunk_index
                LIMIT 40
                """
            ),
            {"fid": fid},
        )
    ).mappings().all()
    if chunks:
        texts = [c["text"] for c in chunks]
        joined = "\n".join(texts)
        return {
            "fil_id": fid,
            "message_id": mid,
            "filename": row["filename"],
            "source": "document_chunks",
            "token_count": sum(int(c["token_count"] or 0) for c in chunks),
            "chunks": [
                {"chunk_index": c["chunk_index"], "text": c["text"], "token_count": c["token_count"]}
                for c in chunks
            ],
            "text": joined[: settings.llm_max_attachment_chars],
        }

    # Fallback: read via server-side path only (never return path)
    path_str = row["path"]
    if not path_str:
        raise ToolError("missing_file", "status=ok but path is null", fil_id=fid, message_id=mid)
    path = Path(path_str)
    # Harden: must remain under attachments_dir
    root = Path(settings.attachments_dir).resolve()
    try:
        resolved = path.resolve()
        if root not in resolved.parents and resolved != root:
            raise ToolError("tool_denied", "attachment path escapes attachments_dir", fil_id=fid, message_id=mid)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("unreadable", f"path resolve failed: {exc}", fil_id=fid, message_id=mid) from exc

    if not resolved.is_file():
        raise ToolError("missing_file", "file not found on disk", fil_id=fid, message_id=mid)

    try:
        if resolved.suffix.lower() == ".pdf":
            text_out, err = extract_text_from_pdf(resolved)
            if err or not text_out:
                raise ToolError("unreadable", err or "empty pdf text", fil_id=fid, message_id=mid)
        else:
            text_out = resolved.read_text(encoding="utf-8", errors="replace")
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError("unreadable", str(exc), fil_id=fid, message_id=mid) from exc

    clipped = text_out[: settings.llm_max_attachment_chars]
    pieces = chunk_text(clipped, settings=settings)
    return {
        "fil_id": fid,
        "message_id": mid,
        "filename": row["filename"],
        "source": "live_extract",
        "token_count": sum(p.token_count for p in pieces),
        "chunks": [{"chunk_index": p.index, "text": p.text, "token_count": p.token_count} for p in pieces],
        "text": clipped,
    }


async def retrieve_relevant_chunks(
    session: AsyncSession,
    question: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Hybrid: SQL prefilter + in-app cosine over JSONB embeddings."""
    settings = get_settings()
    cap = limit or settings.llm_max_chunks_per_turn
    query_vec = await embed_query(question, settings=settings)

    # Invoice-like numbers are required. Stopwords must not pull unrelated chunks.
    words, numbers = query_tokens(question or "", word_limit=6)
    params: dict[str, Any] = {"limit": max(cap * 8, 40)}
    where = _query_where(
        params,
        [] if numbers else words,
        numbers,
        fields=["c.text"],
        include_filenames=False,
    )

    rows = (
        await session.execute(
            text(
                f"""
                SELECT c.id, c.source_type, c.message_id, c.fil_id, c.chunk_index,
                       c.text, c.token_count, c.embedding
                FROM agent.document_chunks c
                WHERE {where}
                ORDER BY c.created_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        emb = r["embedding"] or []
        if isinstance(emb, list) and emb and query_vec:
            score = cosine_similarity(query_vec, [float(x) for x in emb])
        else:
            score = 0.0
        scored.append(
            (
                score,
                {
                    "id": str(r["id"]),
                    "source_type": r["source_type"],
                    "message_id": str(r["message_id"]),
                    "fil_id": r["fil_id"],
                    "chunk_index": r["chunk_index"],
                    "text": r["text"],
                    "token_count": r["token_count"],
                    "score": round(score, 4),
                },
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:cap]]
