"""Resolve Front `fil_*` attachment ids against the on-disk attachments/ folder."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ap_inbox.config import get_settings
from ap_inbox.models import AttachmentStatus


@dataclass
class AttachmentRef:
    fil_id: str
    message_id: uuid.UUID
    filename: str | None
    content_type: str | None
    is_inline: bool
    size: int | None = None


def _attachments_dir() -> Path:
    return Path(get_settings().attachments_dir).resolve()


def find_attachment_path(fil_id: str, filename: str | None = None) -> Path | None:
    """Locate `attachments/fil_{id}__{filename}` (prefix match on fil_id)."""
    root = _attachments_dir()
    if not root.is_dir():
        return None
    # Exact preferred: fil_xxx__filename
    if filename:
        candidate = root / f"{fil_id}__{filename}"
        if candidate.is_file():
            return candidate
    matches = sorted(root.glob(f"{fil_id}__*"))
    if matches:
        return matches[0]
    # Rare: file named exactly the fil_id
    bare = root / fil_id
    if bare.is_file():
        return bare
    return None


def parse_attachment_json(raw: Any, message_id: uuid.UUID) -> list[AttachmentRef]:
    if not raw:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[AttachmentRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fil_id = item.get("id")
        if not fil_id or not str(fil_id).startswith("fil_"):
            continue
        meta = item.get("metadata") or {}
        is_inline = bool(meta.get("is_inline", False)) if isinstance(meta, dict) else False
        out.append(
            AttachmentRef(
                fil_id=str(fil_id),
                message_id=message_id,
                filename=item.get("filename"),
                content_type=item.get("content_type"),
                is_inline=is_inline,
                size=item.get("size"),
            )
        )
    return out


def classify_disk_status(ref: AttachmentRef) -> tuple[str, str | None, str | None]:
    """Return (status, path_str|None, error_detail|None)."""
    if ref.is_inline:
        path = find_attachment_path(ref.fil_id, ref.filename)
        if path is None:
            return "skipped_inline", None, "inline attachment has no on-disk file (expected for many CID images)"
        return "ok", str(path), None

    path = find_attachment_path(ref.fil_id, ref.filename)
    if path is None:
        return "missing_file", None, f"no file matching {ref.fil_id}__* under attachments/"
    try:
        if path.stat().st_size == 0:
            return "unreadable", str(path), "zero-byte file"
    except OSError as exc:
        return "unreadable", str(path), f"stat failed: {exc}"
    return "ok", str(path), None


def fetch_message_attachments(session: Session, limit: int | None = None) -> list[AttachmentRef]:
    sql = """
        SELECT id, attachments
        FROM frontapp.frontapp_messages
        WHERE attachments IS NOT NULL
          AND jsonb_typeof(attachments) = 'array'
          AND jsonb_array_length(attachments) > 0
        ORDER BY message_created_at NULLS LAST
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = session.execute(text(sql)).mappings().all()
    refs: list[AttachmentRef] = []
    for row in rows:
        refs.extend(parse_attachment_json(row["attachments"], row["id"]))
    return refs


def upsert_attachment_status(session: Session, ref: AttachmentRef) -> AttachmentStatus:
    status, path, error = classify_disk_status(ref)
    stmt = (
        insert(AttachmentStatus)
        .values(
            id=uuid.uuid4(),
            fil_id=ref.fil_id,
            message_id=ref.message_id,
            filename=ref.filename,
            content_type=ref.content_type,
            is_inline=ref.is_inline,
            path=path,
            status=status,
            error_detail=error,
        )
        .on_conflict_do_update(
            constraint="uq_attachment_status_fil_message",
            set_={
                "filename": ref.filename,
                "content_type": ref.content_type,
                "is_inline": ref.is_inline,
                "path": path,
                "status": status,
                "error_detail": error,
            },
        )
        .returning(AttachmentStatus)
    )
    return session.execute(stmt).scalar_one()


def scan_attachments(session: Session, limit: int | None = None) -> dict[str, int]:
    """Scan all Front attachment refs and upsert agent.attachment_status. Never silent."""
    refs = fetch_message_attachments(session, limit=limit)
    counts = {"total": 0, "ok": 0, "missing_file": 0, "unreadable": 0, "skipped_inline": 0, "errors": 0}
    for ref in refs:
        counts["total"] += 1
        try:
            row = upsert_attachment_status(session, ref)
            counts[row.status] = counts.get(row.status, 0) + 1
        except Exception as exc:  # noqa: BLE001 — record failure, continue
            counts["errors"] += 1
            # Best-effort gap row even if upsert path itself fails partially
            try:
                session.rollback()
                stmt = insert(AttachmentStatus).values(
                    id=uuid.uuid4(),
                    fil_id=ref.fil_id,
                    message_id=ref.message_id,
                    filename=ref.filename,
                    content_type=ref.content_type,
                    is_inline=ref.is_inline,
                    path=None,
                    status="unreadable",
                    error_detail=f"scan exception: {exc}",
                ).on_conflict_do_update(
                    constraint="uq_attachment_status_fil_message",
                    set_={"status": "unreadable", "error_detail": f"scan exception: {exc}"},
                )
                session.execute(stmt)
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
    session.commit()
    return counts


def list_gaps(session: Session, statuses: list[str] | None = None) -> list[AttachmentStatus]:
    wanted = statuses or ["missing_file", "unreadable"]
    q = select(AttachmentStatus).where(AttachmentStatus.status.in_(wanted)).order_by(AttachmentStatus.fil_id)
    return list(session.scalars(q).all())
