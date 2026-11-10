"""Hybrid retrieval: SQL filters + in-app cosine over document_chunks (JSONB embeddings).

Kept for backwards-compatible callers; chat SSE now prefers the orchestrator tool path.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ap_inbox.config import get_settings
from ap_inbox.embeddings import cosine_similarity, embed_query
from ap_inbox.services.tool_backend import retrieve_relevant_chunks


@dataclass
class RetrievedMessage:
    message_id: uuid.UUID
    subject: str | None
    blurb: str | None
    body_excerpt: str
    author_email: str | None
    from_name: str | None
    doc_type: str | None
    annotation_confidence: float | None
    invoice_numbers: list[str]
    vendor: str | None


@dataclass
class GapNote:
    kind: str
    fil_id: str | None
    message_id: str | None
    detail: str


def _tokens(question: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,}", question)
    stop = {"the", "and", "for", "with", "from", "what", "which", "where", "when", "have", "about", "invoice", "invoices"}
    return [t for t in toks if t.lower() not in stop][:8]


async def retrieve_context(session: AsyncSession, question: str, limit: int = 12) -> tuple[list[RetrievedMessage], list[GapNote]]:
    tokens = _tokens(question)
    gaps: list[GapNote] = []
    settings = get_settings()

    # Prefer chunk embedding rank when table is populated
    try:
        ranked = await retrieve_relevant_chunks(session, question, limit=min(limit, settings.llm_max_chunks_per_turn))
        if ranked:
            msg_ids = []
            seen: set[str] = set()
            for ch in ranked:
                mid = ch.get("message_id")
                if mid and mid not in seen:
                    seen.add(mid)
                    msg_ids.append(mid)
            if msg_ids:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT
                              m.id AS message_id,
                              m.subject,
                              m.blurb,
                              left(coalesce(m.body_text, ''), 1200) AS body_excerpt,
                              m.author_email,
                              m.recipient_from_name AS from_name,
                              a.doc_type,
                              a.confidence AS annotation_confidence,
                              i.invoice_number,
                              i.vendor
                            FROM frontapp.frontapp_messages m
                            LEFT JOIN agent.message_annotations a ON a.message_id = m.id
                            LEFT JOIN agent.invoice_fields i ON i.message_id = m.id AND i.invoice_number IS NOT NULL
                            WHERE m.id::text = ANY(:ids)
                            """
                        ),
                        {"ids": msg_ids},
                    )
                ).mappings().all()
                by_id: dict[uuid.UUID, RetrievedMessage] = {}
                for row in rows:
                    mid = row["message_id"]
                    inv = row.get("invoice_number")
                    if mid not in by_id:
                        by_id[mid] = RetrievedMessage(
                            message_id=mid,
                            subject=row.get("subject"),
                            blurb=row.get("blurb"),
                            body_excerpt=row.get("body_excerpt") or "",
                            author_email=row.get("author_email"),
                            from_name=row.get("from_name"),
                            doc_type=row.get("doc_type"),
                            annotation_confidence=float(row["annotation_confidence"])
                            if row.get("annotation_confidence") is not None
                            else None,
                            invoice_numbers=[inv] if inv else [],
                            vendor=row.get("vendor"),
                        )
                    elif inv and inv not in by_id[mid].invoice_numbers:
                        by_id[mid].invoice_numbers.append(inv)
                # Preserve chunk rank order
                ordered = [by_id[uuid.UUID(mid)] for mid in msg_ids if uuid.UUID(mid) in by_id]
                att_gaps = await _attachment_gaps(session, list(by_id.keys()))
                return ordered[:limit], att_gaps
    except Exception as exc:  # noqa: BLE001
        gaps.append(GapNote(kind="retrieval_error", fil_id=None, message_id=None, detail=f"chunk retrieval: {exc}"))

    params: dict[str, Any] = {"limit": limit}
    where_bits = ["1=1"]
    if tokens:
        ors = []
        for i, tok in enumerate(tokens):
            key = f"t{i}"
            params[key] = f"%{tok}%"
            ors.append(
                f"(m.subject ILIKE :{key} OR m.body_text ILIKE :{key} OR m.blurb ILIKE :{key} "
                f"OR m.recipient_from_name ILIKE :{key} OR m.author_email ILIKE :{key} "
                f"OR a.doc_type ILIKE :{key} OR i.vendor ILIKE :{key} OR i.invoice_number ILIKE :{key})"
            )
        where_bits = ["(" + " OR ".join(ors) + ")"]

    sql = f"""
        SELECT
          m.id AS message_id,
          m.subject,
          m.blurb,
          left(coalesce(m.body_text, ''), 1200) AS body_excerpt,
          m.author_email,
          m.recipient_from_name AS from_name,
          a.doc_type,
          a.confidence AS annotation_confidence,
          i.invoice_number,
          i.vendor
        FROM frontapp.frontapp_messages m
        LEFT JOIN agent.message_annotations a ON a.message_id = m.id
        LEFT JOIN agent.invoice_fields i ON i.message_id = m.id AND i.invoice_number IS NOT NULL
        WHERE {" AND ".join(where_bits)}
        ORDER BY m.message_created_at DESC NULLS LAST
        LIMIT :limit
    """
    try:
        rows = (await session.execute(text(sql), params)).mappings().all()
    except Exception as exc:  # noqa: BLE001 — agent schema may not exist yet
        gaps.append(GapNote(kind="retrieval_error", fil_id=None, message_id=None, detail=str(exc)))
        fallback = """
            SELECT id AS message_id, subject, blurb,
                   left(coalesce(body_text, ''), 1200) AS body_excerpt,
                   author_email, recipient_from_name AS from_name
            FROM frontapp.frontapp_messages
            ORDER BY message_created_at DESC NULLS LAST
            LIMIT :limit
        """
        rows = (await session.execute(text(fallback), {"limit": limit})).mappings().all()

    by_id: dict[uuid.UUID, RetrievedMessage] = {}
    for row in rows:
        mid = row["message_id"]
        inv = row.get("invoice_number")
        if mid not in by_id:
            by_id[mid] = RetrievedMessage(
                message_id=mid,
                subject=row.get("subject"),
                blurb=row.get("blurb"),
                body_excerpt=row.get("body_excerpt") or "",
                author_email=row.get("author_email"),
                from_name=row.get("from_name"),
                doc_type=row.get("doc_type"),
                annotation_confidence=float(row["annotation_confidence"]) if row.get("annotation_confidence") is not None else None,
                invoice_numbers=[inv] if inv else [],
                vendor=row.get("vendor"),
            )
        else:
            if inv and inv not in by_id[mid].invoice_numbers:
                by_id[mid].invoice_numbers.append(inv)

    gaps.extend(await _attachment_gaps(session, list(by_id.keys())))
    return list(by_id.values()), gaps


async def _attachment_gaps(session: AsyncSession, message_ids: list[uuid.UUID]) -> list[GapNote]:
    gaps: list[GapNote] = []
    if not message_ids:
        return gaps
    try:
        gap_rows = (
            await session.execute(
                text(
                    """
                    SELECT fil_id, message_id, status, error_detail
                    FROM agent.attachment_status
                    WHERE message_id::text = ANY(:ids)
                      AND status IN ('missing_file', 'unreadable', 'low_confidence')
                    LIMIT 50
                    """
                ),
                {"ids": [str(mid) for mid in message_ids]},
            )
        ).mappings().all()
        for g in gap_rows:
            gaps.append(
                GapNote(
                    kind=g["status"],
                    fil_id=g["fil_id"],
                    message_id=str(g["message_id"]),
                    detail=g["error_detail"] or g["status"],
                )
            )
    except Exception as exc:  # noqa: BLE001
        gaps.append(GapNote(kind="gap_query_error", fil_id=None, message_id=None, detail=str(exc)))
    return gaps


def format_context_blocks(messages: list[RetrievedMessage]) -> list[str]:
    blocks: list[str] = []
    for m in messages:
        blocks.append(
            "\n".join(
                [
                    f"message_id: {m.message_id}",
                    f"subject: {m.subject or ''}",
                    f"from: {m.from_name or ''} <{m.author_email or ''}>",
                    f"doc_type: {m.doc_type or 'unknown'} (confidence={m.annotation_confidence})",
                    f"vendor: {m.vendor or ''}",
                    f"invoice_numbers: {', '.join(m.invoice_numbers) or 'none'}",
                    f"blurb: {m.blurb or ''}",
                    f"body: {m.body_excerpt}",
                ]
            )
        )
    return blocks


async def rank_chunks_by_query(
    session: AsyncSession,
    question: str,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Direct cosine rank helper (also used by eval harness)."""
    _ = (cosine_similarity, embed_query)  # re-export path for tests
    return await retrieve_relevant_chunks(session, question, limit=limit)
