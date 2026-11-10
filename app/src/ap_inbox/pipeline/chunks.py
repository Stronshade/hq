"""DenoteWorker-only writer: overlapping chunks + nomic embeddings → agent.document_chunks."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ap_inbox.chunking import chunk_text
from ap_inbox.config import get_settings
from ap_inbox.embeddings import embed_texts_sync
from ap_inbox.models import DocumentChunk
from ap_inbox.pipeline.extract_invoice import extract_text_from_pdf

logger = logging.getLogger(__name__)


def _read_attachment_text(path: Path, max_chars: int) -> tuple[str | None, str | None]:
    """Return (text, error). Never silent on failure."""
    try:
        if path.suffix.lower() == ".pdf":
            text_out, err = extract_text_from_pdf(path)
            if err or not text_out:
                return None, err or "empty pdf text"
            return text_out[:max_chars], None
        if path.suffix.lower() in {".txt", ".csv", ".html", ".htm", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars], None
        return None, f"no text extractor for {path.suffix or 'unknown'} file type"
    except Exception as exc:  # noqa: BLE001
        return None, f"unreadable: {exc}"


def replace_chunks_for_message(
    session: Session,
    *,
    message_id: uuid.UUID,
    body_text: str | None,
    attachment_paths: list[tuple[str, Path]],
    embed: bool = True,
) -> dict[str, int]:
    """Idempotent replace of chunks for one message. Chat path must never call this."""
    settings = get_settings()
    counts = {"body_chunks": 0, "attachment_chunks": 0, "embed_failures": 0}

    session.execute(
        text("DELETE FROM agent.document_chunks WHERE message_id = :mid"),
        {"mid": message_id},
    )

    pending: list[tuple[str, uuid.UUID, str | None, int, str, int]] = []
    # (source_type, message_id, fil_id, chunk_index, text, token_count)

    for ch in chunk_text(body_text or "", settings=settings):
        # Use "" not NULL so UNIQUE (source_type, message_id, fil_id, chunk_index) works in Postgres
        pending.append(("message_body", message_id, "", ch.index, ch.text, ch.token_count))

    for fil_id, path in attachment_paths:
        att_text, err = _read_attachment_text(path, settings.llm_max_attachment_chars)
        if err or not att_text:
            logger.info("skip attachment chunks message=%s fil=%s: %s", message_id, fil_id, err)
            continue
        for ch in chunk_text(att_text, settings=settings):
            pending.append(("attachment_text", message_id, fil_id, ch.index, ch.text, ch.token_count))

    embeddings: list[list[float]] = [[] for _ in pending]
    if embed and pending:
        embeddings = embed_texts_sync([p[4] for p in pending], settings=settings)

    for row, emb in zip(pending, embeddings, strict=True):
        source_type, mid, fil_id, chunk_index, txt, token_count = row
        if embed and not emb:
            counts["embed_failures"] += 1
        session.add(
            DocumentChunk(
                id=uuid.uuid4(),
                source_type=source_type,
                message_id=mid,
                fil_id=fil_id,
                chunk_index=chunk_index,
                text=txt,
                token_count=token_count,
                embedding=emb or [],
                model=settings.embed_model if emb else None,
            )
        )
        if source_type == "message_body":
            counts["body_chunks"] += 1
        else:
            counts["attachment_chunks"] += 1

    return counts
