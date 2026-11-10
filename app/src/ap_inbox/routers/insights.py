"""Insight endpoints: vendors/chasers, invoices, gaps."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ap_inbox.db import get_db

router = APIRouter(tags=["insights"])


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    try:
        msg_count = (
            await db.execute(text("SELECT count(*) FROM frontapp.frontapp_messages"))
        ).scalar_one()
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "frontapp_messages": None, "error": str(exc)}
    return {"status": "ok", "frontapp_messages": int(msg_count)}


@router.get("/gaps")
async def list_gaps(
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(None, description="Comma-separated statuses"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    statuses = [s.strip() for s in status.split(",")] if status else ["missing_file", "unreadable", "low_confidence"]
    rows = (
        await db.execute(
            text(
                """
                SELECT fil_id, message_id, filename, content_type, is_inline, path, status, error_detail
                FROM agent.attachment_status
                WHERE status = ANY(:statuses)
                ORDER BY status, fil_id
                LIMIT :limit
                """
            ),
            {"statuses": statuses, "limit": limit},
        )
    ).mappings().all()
    return {
        "count": len(rows),
        "items": [
            {
                "fil_id": r["fil_id"],
                "message_id": str(r["message_id"]),
                "filename": r["filename"],
                "content_type": r["content_type"],
                "is_inline": r["is_inline"],
                "path": r["path"],
                "status": r["status"],
                "error_detail": r["error_detail"],
            }
            for r in rows
        ],
    }


@router.get("/invoices")
async def list_invoices(
    db: AsyncSession = Depends(get_db),
    vendor: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    where = "WHERE invoice_number IS NOT NULL"
    if vendor:
        where += " AND vendor ILIKE :vendor"
        params["vendor"] = f"%{vendor}%"
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, message_id, attachment_fil_id, vendor, invoice_number, amount, currency,
                       invoice_date, due_date, po_number, overall_confidence, status, error_detail
                FROM agent.invoice_fields
                {where}
                ORDER BY invoice_date DESC NULLS LAST, created_at DESC
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
                "id": str(r["id"]),
                "message_id": str(r["message_id"]),
                "attachment_fil_id": r["attachment_fil_id"],
                "vendor": r["vendor"],
                "invoice_number": r["invoice_number"],
                "amount": str(r["amount"]) if r["amount"] is not None else None,
                "currency": r["currency"],
                "invoice_date": r["invoice_date"].isoformat() if r["invoice_date"] else None,
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "po_number": r["po_number"],
                "overall_confidence": float(r["overall_confidence"]) if r["overall_confidence"] is not None else None,
                "status": r["status"],
                "error_detail": r["error_detail"],
            }
            for r in rows
        ],
    }


@router.get("/vendors/chasers")
async def list_chasers(
    db: AsyncSession = Depends(get_db),
    stage: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    where = "WHERE 1=1"
    if stage:
        where += " AND stage = :stage"
        params["stage"] = stage
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, message_id, vendor, stage, referenced_invoice_numbers,
                       invoice_present_in_inbox, confidence, rationale, status, error_detail
                FROM agent.vendor_chase_events
                {where}
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
                "id": str(r["id"]),
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
