"""Internal HTTP tool gateway — compose network + X-Internal-Token.

Not for browser clients. LangChain tools call these endpoints via httpx.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ap_inbox.config import get_settings
from ap_inbox.db import get_db
from ap_inbox.services import tool_backend as tb

router = APIRouter(prefix="/internal", tags=["internal"])


def require_internal_token(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")) -> None:
    expected = get_settings().internal_api_token
    if not x_internal_token or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Internal-Token")


@router.get("/messages/search")
async def messages_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(12, ge=1, le=40),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    return await tb.search_messages(db, q, limit=limit)


@router.get("/messages/{message_id}/annotations")
async def message_annotations(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    try:
        return await tb.get_annotations(db, message_id)
    except tb.ToolError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@router.get("/annotations")
async def annotations_summary(
    doc_type: str | None = None,
    status: str | None = None,
    limit: int = Query(8, ge=1, le=40),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    return await tb.get_annotations(db, doc_type=doc_type, status=status, limit=limit)


@router.get("/invoices")
async def invoices(
    vendor: str | None = None,
    invoice_number: str | None = None,
    status: str | None = None,
    limit: int = Query(40, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    return await tb.get_invoice_fields(
        db,
        vendor=vendor,
        invoice_number=invoice_number,
        status=status,
        limit=limit,
    )


@router.get("/chase")
async def chase(
    vendor: str | None = None,
    stage: str | None = None,
    present: bool | None = None,
    limit: int = Query(40, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    return await tb.get_chase_events(db, vendor=vendor, stage=stage, present=present, limit=limit)


@router.get("/attachments")
async def attachments_by_status(
    status: str = Query(..., min_length=1),
    limit: int = Query(40, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    try:
        return await tb.list_attachments_by_status(db, status, limit=limit)
    except tb.ToolError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@router.get("/attachments/{fil_id}")
async def attachment_meta(
    fil_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    try:
        return await tb.list_attachment_meta(db, fil_id)
    except tb.ToolError as exc:
        status = 404 if exc.code == "missing_file" else 403 if exc.code == "tool_denied" else 400
        raise HTTPException(status_code=status, detail=exc.to_dict()) from exc


@router.get("/attachments/{fil_id}/text")
async def attachment_text(
    fil_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    try:
        return await tb.fetch_attachment_text(db, fil_id)
    except tb.ToolError as exc:
        status = {
            "missing_file": 404,
            "unreadable": 422,
            "tool_denied": 403,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=exc.to_dict()) from exc


@router.get("/chunks/search")
async def chunks_search(
    q: str = Query(..., min_length=1),
    limit: int | None = Query(None, ge=1, le=40),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_internal_token),
) -> dict[str, Any]:
    items = await tb.retrieve_relevant_chunks(db, q, limit=limit)
    return {"count": len(items), "items": items}
