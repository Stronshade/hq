"""Batch denote pipeline: attachments → classify → extract → chase.

Idempotent upserts. Every failure writes a gap / error status row — never silent.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ap_inbox.db import SessionLocal
from ap_inbox.models import (
    AttachmentStatus,
    InvoiceField,
    MessageAnnotation,
    PipelineRun,
    VendorChaseEvent,
)
from ap_inbox.pipeline.attachments import scan_attachments
from ap_inbox.pipeline.chase import detect_chase
from ap_inbox.pipeline.chunks import replace_chunks_for_message
from ap_inbox.pipeline.classify import ClassificationResult, classify_attachment, classify_message
from ap_inbox.pipeline.extract_invoice import extract_from_message, extract_text_from_pdf


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _upsert_annotation(session: Session, message_id: uuid.UUID, result) -> None:
    stmt = (
        insert(MessageAnnotation)
        .values(
            id=uuid.uuid4(),
            message_id=message_id,
            doc_type=result.doc_type,
            confidence=result.confidence,
            rationale=result.rationale,
            model=result.model,
            status=result.status,
            error_detail=result.error_detail,
        )
        .on_conflict_do_update(
            constraint="uq_message_annotations_message_id",
            set_={
                "doc_type": result.doc_type,
                "confidence": result.confidence,
                "rationale": result.rationale,
                "model": result.model,
                "status": result.status,
                "error_detail": result.error_detail,
                "updated_at": _utcnow(),
            },
        )
    )
    session.execute(stmt)


def _replace_invoice_fields(session: Session, message_id: uuid.UUID, extractions) -> int:
    session.execute(
        text("DELETE FROM agent.invoice_fields WHERE message_id = :mid"),
        {"mid": message_id},
    )
    n = 0
    for ext in extractions:
        session.add(
            InvoiceField(
                id=uuid.uuid4(),
                message_id=message_id,
                attachment_fil_id=ext.attachment_fil_id,
                vendor=ext.vendor,
                invoice_number=ext.invoice_number,
                amount=ext.amount,
                currency=ext.currency,
                invoice_date=ext.invoice_date,
                due_date=ext.due_date,
                po_number=ext.po_number,
                field_confidence=ext.field_confidence or {},
                overall_confidence=ext.overall_confidence or Decimal("0"),
                status=ext.status,
                error_detail=ext.error_detail,
                raw_excerpt=ext.raw_excerpt,
            )
        )
        n += 1
    return n


def _replace_chase(session: Session, message_id: uuid.UUID, chase) -> None:
    session.execute(
        text("DELETE FROM agent.vendor_chase_events WHERE message_id = :mid"),
        {"mid": message_id},
    )
    if chase is None:
        return
    session.add(
        VendorChaseEvent(
            id=uuid.uuid4(),
            message_id=message_id,
            vendor=chase.vendor,
            stage=chase.stage,
            referenced_invoice_numbers=chase.referenced_invoice_numbers or [],
            invoice_present_in_inbox=chase.invoice_present_in_inbox,
            confidence=chase.confidence,
            rationale=chase.rationale,
            status=chase.status,
            error_detail=chase.error_detail,
        )
    )


def _known_invoice_numbers(session: Session) -> set[str]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT invoice_number
            FROM agent.invoice_fields
            WHERE invoice_number IS NOT NULL AND status IN ('ok', 'low_confidence')
            """
        )
    ).scalars().all()
    return {r for r in rows if r}


def _attachment_rows_for_message(session: Session, message_id: uuid.UUID) -> list[AttachmentStatus]:
    return list(
        session.scalars(
            select(AttachmentStatus).where(
                AttachmentStatus.message_id == message_id,
                AttachmentStatus.status == "ok",
                AttachmentStatus.path.is_not(None),
            )
        ).all()
    )


def _attachment_paths_for_message(session: Session, message_id: uuid.UUID) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for row in _attachment_rows_for_message(session, message_id):
        p = Path(row.path) if row.path else None
        if p and p.is_file():
            out.append((row.fil_id, p))
    return out


def _attachment_text_excerpt(path: Path, *, max_chars: int = 6000) -> str | None:
    try:
        if path.suffix.lower() == ".pdf":
            text, err = extract_text_from_pdf(path)
            if err or not text:
                return None
            return text[:max_chars]
        if path.suffix.lower() in {".txt", ".csv", ".html", ".htm"}:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:  # noqa: BLE001
        return None
    return None


def _persist_attachment_doc(
    session: Session,
    row: AttachmentStatus,
    result: ClassificationResult,
) -> None:
    row.doc_type = result.doc_type
    row.doc_confidence = result.confidence
    row.doc_rationale = result.rationale
    row.doc_model = result.model
    row.updated_at = _utcnow()
    session.add(row)


def _classify_attachments_for_message(
    session: Session,
    message_id: uuid.UUID,
    *,
    use_llm: bool,
) -> dict[str, str]:
    """Classify non-inline ok attachments; persist doc_* and return fil_id -> doc_type."""
    doc_types: dict[str, str] = {}
    rows = session.scalars(
        select(AttachmentStatus).where(
            AttachmentStatus.message_id == message_id,
            AttachmentStatus.is_inline.is_(False),
        )
    ).all()
    for row in rows:
        if row.status != "ok" or not row.path:
            continue
        path = Path(row.path)
        excerpt = _attachment_text_excerpt(path) if path.is_file() else None
        result = classify_attachment(
            row.fil_id,
            row.filename,
            row.content_type,
            excerpt,
            use_llm=use_llm,
        )
        _persist_attachment_doc(session, row, result)
        doc_types[row.fil_id] = result.doc_type
    return doc_types


def _messages_sql(*, limit: int | None, resume_unannotated: bool) -> str:
    if resume_unannotated:
        sql = """
            SELECT m.id, m.subject, m.body_text, m.author_email, m.recipient_from_name, m.attachments
            FROM frontapp.frontapp_messages m
            LEFT JOIN agent.message_annotations a ON a.message_id = m.id
            WHERE a.id IS NULL OR a.status = 'error'
            ORDER BY m.message_created_at NULLS LAST
        """
    else:
        sql = """
            SELECT id, subject, body_text, author_email, recipient_from_name, attachments
            FROM frontapp.frontapp_messages
            ORDER BY message_created_at NULLS LAST
        """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def run_denote(
    session: Session,
    *,
    limit: int | None = None,
    skip_attachments: bool = False,
    llm_refine: bool = True,
    skip_chunks: bool = False,
    resume_unannotated: bool = False,
) -> dict:
    run = PipelineRun(id=uuid.uuid4(), status="running", counts={}, failures=[])
    session.add(run)
    session.commit()

    counts: dict[str, int] = {
        "messages": 0,
        "annotated": 0,
        "attachment_docs": 0,
        "invoice_rows": 0,
        "chase_events": 0,
        "body_chunks": 0,
        "attachment_chunks": 0,
        "embed_failures": 0,
        "failures": 0,
        "llm_refine": 1 if llm_refine else 0,
        "skip_chunks": 1 if skip_chunks else 0,
        "resume_unannotated": 1 if resume_unannotated else 0,
    }
    failures: list[dict] = []

    try:
        if not skip_attachments:
            att_counts = scan_attachments(session, limit=None)
            counts["attachments_scanned"] = att_counts.get("total", 0)
            counts["attachments_missing"] = att_counts.get("missing_file", 0)
            counts["attachments_unreadable"] = att_counts.get("unreadable", 0)
            counts["attachments_ok"] = att_counts.get("ok", 0)

        messages = session.execute(
            text(_messages_sql(limit=limit, resume_unannotated=resume_unannotated))
        ).mappings().all()

        total_msgs = len(messages)
        # First pass: classify + extract (builds invoice_fields for chase join)
        for row in messages:
            counts["messages"] += 1
            mid = row["id"]
            if counts["messages"] == 1 or counts["messages"] % 10 == 0:
                print(
                    f"denote progress {counts['messages']}/{total_msgs} "
                    f"annotated={counts['annotated']} invoice_rows={counts['invoice_rows']} "
                    f"failures={counts['failures']}",
                    flush=True,
                )
            try:
                filenames: list[str] = []
                att = row["attachments"]
                if isinstance(att, list):
                    filenames = [a.get("filename") for a in att if isinstance(a, dict) and a.get("filename")]

                classification = classify_message(
                    mid,
                    row["subject"],
                    row["body_text"],
                    filenames,
                    use_llm=llm_refine,
                )
                _upsert_annotation(session, mid, classification)
                counts["annotated"] += 1

                att_doc_types = _classify_attachments_for_message(session, mid, use_llm=llm_refine)
                counts["attachment_docs"] += len(att_doc_types)

                paths = _attachment_paths_for_message(session, mid)
                extractions = extract_from_message(
                    message_id=mid,
                    subject=row["subject"],
                    body=row["body_text"],
                    author_email=row["author_email"],
                    from_name=row["recipient_from_name"],
                    attachment_paths=paths,
                    doc_type=classification.doc_type,
                    attachment_doc_types=att_doc_types,
                    use_llm=llm_refine,
                )
                counts["invoice_rows"] += _replace_invoice_fields(session, mid, extractions)

                if not skip_chunks:
                    try:
                        chunk_counts = replace_chunks_for_message(
                            session,
                            message_id=mid,
                            body_text=row["body_text"],
                            attachment_paths=paths,
                            embed=True,
                        )
                        counts["body_chunks"] += chunk_counts.get("body_chunks", 0)
                        counts["attachment_chunks"] += chunk_counts.get("attachment_chunks", 0)
                        counts["embed_failures"] += chunk_counts.get("embed_failures", 0)
                    except Exception as chunk_exc:  # noqa: BLE001
                        counts["failures"] += 1
                        failures.append(
                            {"message_id": str(mid), "stage": "chunk_embed", "error": str(chunk_exc)}
                        )

                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                counts["failures"] += 1
                failures.append({"message_id": str(mid), "stage": "classify_extract", "error": str(exc)})
                try:
                    _upsert_annotation(
                        session,
                        mid,
                        type("R", (), {
                            "doc_type": "other",
                            "confidence": Decimal("0"),
                            "rationale": "pipeline failure",
                            "model": "denote",
                            "status": "error",
                            "error_detail": str(exc),
                        })(),
                    )
                    session.commit()
                except Exception:  # noqa: BLE001
                    session.rollback()

        known = _known_invoice_numbers(session)

        # Second pass: chase detection using known invoice numbers
        anns = {
            a.message_id: a
            for a in session.scalars(select(MessageAnnotation)).all()
        }
        for row in messages:
            mid = row["id"]
            ann = anns.get(mid)
            if ann is None:
                continue
            try:
                classification = ClassificationResult(
                    doc_type=ann.doc_type,
                    confidence=Decimal(str(ann.confidence)),
                    rationale=ann.rationale or "",
                    model=ann.model or "rules-v1",
                    status=ann.status,
                    error_detail=ann.error_detail,
                )
                vendor_hint = row["recipient_from_name"] or row["author_email"]
                chase = detect_chase(
                    classification=classification,
                    subject=row["subject"],
                    body=row["body_text"],
                    vendor_hint=vendor_hint,
                    known_invoice_numbers=known,
                )
                _replace_chase(session, mid, chase)
                if chase is not None and chase.status != "error":
                    counts["chase_events"] += 1
                elif chase is not None and chase.status == "error":
                    counts["failures"] += 1
                    failures.append({"message_id": str(mid), "stage": "chase", "error": chase.error_detail})
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                counts["failures"] += 1
                failures.append({"message_id": str(mid), "stage": "chase", "error": str(exc)})

        run.status = "completed" if counts["failures"] == 0 else "completed_with_errors"
        run.counts = counts
        run.failures = failures[:500]
        run.finished_at = _utcnow()
        session.add(run)
        session.commit()
        return {"run_id": str(run.id), "status": run.status, "counts": counts, "failure_sample": failures[:20]}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run.status = "failed"
        run.detail = str(exc)
        run.failures = failures + [{"stage": "fatal", "error": str(exc)}]
        run.counts = counts
        run.finished_at = _utcnow()
        session.add(run)
        session.commit()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AP Inbox denote pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit messages processed")
    parser.add_argument("--skip-attachments", action="store_true")
    parser.add_argument(
        "--llm-refine",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Escalate low-confidence classify/extract to the configured chat model (default: on)",
    )
    parser.add_argument(
        "--skip-chunks",
        action="store_true",
        help="Skip chunk/embed pass (faster reclassify/extract)",
    )
    parser.add_argument(
        "--resume-unannotated",
        action="store_true",
        help="Only messages missing annotations or with status=error",
    )
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        result = run_denote(
            session,
            limit=args.limit,
            skip_attachments=args.skip_attachments,
            llm_refine=args.llm_refine,
            skip_chunks=args.skip_chunks,
            resume_unannotated=args.resume_unannotated,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"denote failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
