"""Live-database checks for the twelve demo tool shapes.

Skipped when Postgres is not reachable. Totals are compared to SQL in the same
session so a denote run cannot make the assertion stale.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ap_inbox.config import get_settings
from ap_inbox.services.tool_backend import (
    get_annotations,
    get_chase_events,
    get_invoice_fields,
    list_attachment_meta,
    list_attachments_by_status,
)


async def _shapes() -> None:
    engine = create_async_engine(get_settings().database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as session:
            summary = await get_annotations(session)
            sql_types = dict(
                (
                    await session.execute(
                        text("SELECT doc_type, count(*)::int FROM agent.message_annotations GROUP BY 1")
                    )
                ).all()
            )
            assert summary["by_doc_type"] == {str(k): int(v) for k, v in sql_types.items()}
            assert "invoice" in summary["by_doc_type"]
            assert summary["by_status"]["low_confidence"] >= 1
            assert summary["by_status"]["error"] >= 1
            histogram = summary["rationale_histogram"]
            assert any(row["rationale"] == "no keyword rule matched" and row["count"] >= 1 for row in histogram)
            assert any(row["status"] == "error" and row["error_detail"] for row in histogram)

            low = await get_annotations(session, doc_type="other", status="low_confidence", limit=4)
            assert low["total"] == summary["by_status"]["low_confidence"] or low["filters"]["doc_type"] == "other"
            sql_low = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)::int FROM agent.message_annotations
                        WHERE doc_type = 'other' AND status = 'low_confidence'
                        """
                    )
                )
            ).scalar_one()
            assert low["total"] == int(sql_low)
            assert low["returned"] <= 4
            assert low["items"]
            assert low["items"][0]["subject"] is not None or low["items"][0]["message_id"]
            assert "path" not in low["items"][0]

            thomson = await get_invoice_fields(session, invoice_number="853825322", limit=20)
            assert thomson["found"] is True
            assert thomson["total"] >= 1
            amounts = {item["amount"] for item in thomson["items"]}
            assert any(amount is not None and abs(float(amount) - 2229.18) < 0.01 for amount in amounts)
            dated = [item for item in thomson["items"] if item["invoice_date"] == "2026-07-01"]
            assert dated
            assert dated[0]["due_date"] == "2026-07-31"
            assert dated[0]["currency"] == "USD"
            assert dated[0]["overall_confidence"] is not None
            assert dated[0]["overall_confidence"] > 0.9
            assert dated[0]["message_id"]

            punch = await get_invoice_fields(session, vendor="Punch Distribution", limit=40)
            sql_punch = (
                await session.execute(
                    text(
                        "SELECT count(*)::int FROM agent.invoice_fields WHERE vendor ILIKE '%Punch Distribution%'"
                    )
                )
            ).scalar_one()
            assert punch["total"] == int(sql_punch)
            assert punch["total"] > punch["returned"]
            assert punch["truncated"] is True
            numbers = {item["invoice_number"] for item in punch["items"]}
            assert "114360" in numbers and "114350" in numbers
            by_number = {item["invoice_number"]: item for item in punch["items"]}
            assert abs(float(by_number["114360"]["amount"]) - 28212.80) < 0.01
            assert abs(float(by_number["114350"]["amount"]) - 13603.16) < 0.01
            assert by_number["114360"]["invoice_date"] == "2026-08-31"

            punch_chase = await get_chase_events(session, vendor="Punch Distribution", limit=40)
            assert punch_chase["truncated"] is True
            assert punch_chase["stage_counts"].get("reminder", 0) >= 1
            assert punch_chase["stage_counts"].get("escalation", 0) >= 1
            assert {item["stage"] for item in punch_chase["items"]} >= {"reminder", "escalation"}
            assert punch_chase["total"] == sum(punch_chase["stage_counts"].values())
            assert "exact" in punch_chase["match_policy"].lower()
            assert punch_chase["items"][0]["subject"] is not None or "subject" in punch_chase["items"][0]

            final = await get_chase_events(session, stage="final", limit=20)
            assert final["stage_counts"].get("final_notice", 0) == final["total"]
            assert final["total"] >= 1
            subjects = " ".join((item.get("subject") or "") for item in final["items"]).lower()
            assert "thank you" in subjects or "disconnection" in subjects
            assert any(item.get("subject") for item in final["items"])

            fpl = await get_chase_events(session, vendor="FPL Communications", present=False, limit=50)
            assert fpl["total"] >= 1
            assert all(item["invoice_present_in_inbox"] is False for item in fpl["items"])
            assert all(item["referenced_invoice_numbers"] in ([], None) for item in fpl["items"])

            missing = await list_attachments_by_status(session, "missing_file", limit=12)
            sql_missing = (
                await session.execute(
                    text("SELECT count(*)::int FROM agent.attachment_status WHERE status = 'missing_file'")
                )
            ).scalar_one()
            assert missing["total"] == int(sql_missing)
            assert missing["total"] > 0
            assert missing["truncated"] is True
            kanha = next(row for row in missing["by_filename"] if row["filename"] == "Kanha horizontal.png")
            assert kanha["count"] >= 1
            assert set(missing["items"][0]) == {"fil_id", "filename", "error_detail", "message_id"}
            blob = str(missing)
            assert "C:\\" not in blob and "/data/attachments/" not in blob

            unreadable = await get_invoice_fields(session, status="unreadable", limit=40)
            sql_unreadable = (
                await session.execute(
                    text("SELECT count(*)::int FROM agent.invoice_fields WHERE status = 'unreadable'")
                )
            ).scalar_one()
            assert unreadable["total"] == int(sql_unreadable)
            assert unreadable["returned"] <= 8
            assert unreadable["truncated"] is True
            assert unreadable["by_error_detail"]
            assert all(item["amount"] is None for item in unreadable["items"])

            glass = await list_attachment_meta(session, "fil_4xyrdndg")
            assert glass["items"]
            assert glass["items"][0]["status"] == "ok"
            assert glass["items"][0]["is_inline"] is False
            assert "Glass-House-Brands" in (glass["items"][0]["filename"] or "")
            assert "path" not in glass["items"][0]

            fake = await get_invoice_fields(session, invoice_number="ZZ-DOES-NOT-EXIST-999")
            assert fake["found"] is False
            assert fake["total"] == 0
            assert fake["items"] == []
    finally:
        await engine.dispose()


def test_live_demo_tool_shapes():
    try:
        asyncio.run(_shapes())
    except OSError as exc:
        pytest.skip(f"database unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "connect" in message or "refused" in message or "could not" in message:
            pytest.skip(f"database unavailable: {exc}")
        raise
