"""LangChain tool wrappers over the internal HTTP gateway.

Roles:
  Orchestrator — search, annotations, invoices, chase, attachment meta, chunk search
  AttachmentReader — fetch_attachment_text only (gated; meta must say ok)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ap_inbox.config import Settings, get_settings

logger = logging.getLogger(__name__)

RoleName = Literal["orchestrator", "attachment_reader"]

ORCHESTRATOR_ALLOWLIST = frozenset(
    {
        "search_messages",
        "get_annotations",
        "get_invoice_fields",
        "get_chase_events",
        "list_attachment_meta",
        "search_chunks",
        "fetch_attachment_text",  # gated: must call meta first in practice; still least-privilege vs FS
    }
)

ATTACHMENT_READER_ALLOWLIST = frozenset({"fetch_attachment_text", "list_attachment_meta"})


class SearchMessagesArgs(BaseModel):
    q: str = Field(..., description="Search query over subjects, bodies, vendors, invoice numbers")
    limit: int = Field(12, ge=1, le=40)


class AnnotationArgs(BaseModel):
    message_id: str | None = Field(
        None,
        description="Front message UUID. Omit for inbox-wide counts by doc_type and status.",
    )
    doc_type: str | None = Field(
        None,
        description="Optional filter: invoice, statement, payment_reminder, remittance, escalation, final_notice, other.",
    )
    status: str | None = Field(None, description="Optional filter: ok, low_confidence, or error.")
    limit: int = Field(8, ge=1, le=40, description="Sample size when doc_type or status is set.")


class InvoiceArgs(BaseModel):
    vendor: str | None = Field(None, description="Vendor name filter")
    invoice_number: str | None = Field(None, description="Invoice number filter")
    status: str | None = Field(
        None,
        description="Row status filter. status=unreadable returns error_detail counts and a short sample, not every row.",
    )
    limit: int = Field(40, ge=1, le=200)


class ChaseArgs(BaseModel):
    vendor: str | None = Field(None, description="Vendor name filter")
    stage: str | None = Field(
        None,
        description="Chase stage: reminder, escalation, or final_notice. 'final' and 'final notice' alias to final_notice.",
    )
    present: bool | None = Field(
        None,
        description="Filter invoice_present_in_inbox. False means no exact invoice-number match.",
    )
    limit: int = Field(40, ge=1, le=200)


class AttachmentMetaArgs(BaseModel):
    fil_id: str | None = Field(
        None,
        description="Front attachment id starting with fil_. Omit to list attachments by status.",
    )
    status: str | None = Field(
        None,
        description="When fil_id is omitted, list rows with this status (missing_file, unreadable, ok).",
    )
    limit: int = Field(40, ge=1, le=200)


class FilIdArgs(BaseModel):
    fil_id: str = Field(..., description="Front attachment id starting with fil_")


class ChunkSearchArgs(BaseModel):
    q: str = Field(..., description="Semantic/keyword query over document chunks")
    limit: int = Field(8, ge=1, le=40)


def _headers(settings: Settings) -> dict[str, str]:
    return {"X-Internal-Token": settings.internal_api_token}


def _format_http_error(exc: httpx.HTTPStatusError) -> str:
    try:
        detail = exc.response.json()
    except Exception:  # noqa: BLE001
        detail = exc.response.text
    if isinstance(detail, dict) and "detail" in detail:
        payload = detail["detail"]
        if isinstance(payload, dict):
            return json.dumps(payload)
        return json.dumps({"error": True, "code": "http_error", "detail": str(payload)})
    return json.dumps({"error": True, "code": "http_error", "detail": str(detail)})


class InternalToolClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.internal_api_base_url.rstrip("/"),
            headers=_headers(self.settings),
            timeout=60.0,
        )
        self._meta_ok: set[str] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> str:
        try:
            resp = await self._client.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
            resp.raise_for_status()
            return json.dumps(resp.json())
        except httpx.HTTPStatusError as exc:
            return _format_http_error(exc)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": True, "code": "tool_error", "detail": str(exc)})

    async def search_messages(self, q: str, limit: int = 12) -> str:
        return await self._get("/internal/messages/search", {"q": q, "limit": limit})

    async def get_annotations(
        self,
        message_id: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        limit: int = 8,
    ) -> str:
        if message_id:
            return await self._get(f"/internal/messages/{message_id}/annotations")
        return await self._get(
            "/internal/annotations",
            {"doc_type": doc_type, "status": status, "limit": limit},
        )

    async def get_invoice_fields(
        self,
        vendor: str | None = None,
        invoice_number: str | None = None,
        status: str | None = None,
        limit: int = 40,
    ) -> str:
        return await self._get(
            "/internal/invoices",
            {"vendor": vendor, "invoice_number": invoice_number, "status": status, "limit": limit},
        )

    async def get_chase_events(
        self,
        vendor: str | None = None,
        stage: str | None = None,
        present: bool | None = None,
        limit: int = 40,
    ) -> str:
        return await self._get(
            "/internal/chase",
            {"vendor": vendor, "stage": stage, "present": present, "limit": limit},
        )

    async def list_attachment_meta(
        self,
        fil_id: str | None = None,
        status: str | None = None,
        limit: int = 40,
    ) -> str:
        if not fil_id:
            return await self._get("/internal/attachments", {"status": status, "limit": limit})
        raw = await self._get(f"/internal/attachments/{fil_id}")
        try:
            data = json.loads(raw)
            for item in data.get("items") or []:
                if item.get("status") == "ok" and not item.get("is_inline"):
                    self._meta_ok.add(fil_id)
        except Exception:  # noqa: BLE001
            pass
        return raw

    async def fetch_attachment_text(self, fil_id: str) -> str:
        # Capability gate: require prior successful meta check in this turn when possible
        if fil_id not in self._meta_ok:
            meta_raw = await self.list_attachment_meta(fil_id)
            try:
                meta = json.loads(meta_raw)
                if meta.get("error"):
                    return meta_raw
                items = meta.get("items") or []
                if not items:
                    return json.dumps(
                        {"error": True, "code": "missing_file", "detail": "no meta", "fil_id": fil_id}
                    )
                ok = any(i.get("status") == "ok" and not i.get("is_inline") for i in items)
                if not ok:
                    first = items[0]
                    code = first.get("status") or "tool_denied"
                    return json.dumps(
                        {
                            "error": True,
                            "code": code if code in {"missing_file", "unreadable"} else "tool_denied",
                            "detail": first.get("error_detail") or first.get("status"),
                            "fil_id": fil_id,
                        }
                    )
                self._meta_ok.add(fil_id)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"error": True, "code": "tool_error", "detail": str(exc), "fil_id": fil_id})
        return await self._get(f"/internal/attachments/{fil_id}/text")

    async def search_chunks(self, q: str, limit: int = 8) -> str:
        return await self._get("/internal/chunks/search", {"q": q, "limit": limit})


def build_tools_for_role(role: RoleName, client: InternalToolClient) -> list[StructuredTool]:
    allow = ORCHESTRATOR_ALLOWLIST if role == "orchestrator" else ATTACHMENT_READER_ALLOWLIST
    catalog: dict[str, StructuredTool] = {
        "search_messages": StructuredTool.from_function(
            coroutine=client.search_messages,
            name="search_messages",
            description=(
                "Search AP inbox messages by keywords. An invoice number is required when present "
                "and also matches attachment filenames (including a leading zero). "
                "Results include non-inline attachments with fil_id, filename, and status."
            ),
            args_schema=SearchMessagesArgs,
        ),
        "get_annotations": StructuredTool.from_function(
            coroutine=client.get_annotations,
            name="get_annotations",
            description=(
                "Message classification. Pass message_id for one annotation. "
                "Omit message_id for by_doc_type and by_status totals plus the low_confidence/error "
                "rationale histogram. Pass doc_type and/or status for a total and a short subject sample. "
                "Quote those totals. Do not relabel low-confidence other as invoice or reminder."
            ),
            args_schema=AnnotationArgs,
        ),
        "get_invoice_fields": StructuredTool.from_function(
            coroutine=client.get_invoice_fields,
            name="get_invoice_fields",
            description=(
                "Look up invoice fields by invoice_number, vendor, and/or status. "
                "Always returns total, returned, and truncated. "
                "A named number with total 0 has found=false — do not invent an amount. "
                "Pass the invoice number alone when the user names one. "
                "status=unreadable returns error_detail counts and a few sample rows with null amounts. "
                "A 6+ digit number also matches attachment filenames; shorter tokens are literal "
                "and must not be collapsed to their digits."
            ),
            args_schema=InvoiceArgs,
        ),
        "get_chase_events": StructuredTool.from_function(
            coroutine=client.get_chase_events,
            name="get_chase_events",
            description=(
                "List vendor chase events with subject, stage_counts, total, and truncated. "
                "Stages are reminder, escalation, and final_notice (final / final notice are aliases). "
                "invoice_present_in_inbox is an exact case-insensitive match against extracted invoice numbers. "
                "An empty referenced_invoice_numbers list means no number was extracted. "
                "If truncated, say so and do not invent rows."
            ),
            args_schema=ChaseArgs,
        ),
        "list_attachment_meta": StructuredTool.from_function(
            coroutine=client.list_attachment_meta,
            name="list_attachment_meta",
            description=(
                "Attachment metadata only. Pass fil_id for one file, or status (for example missing_file) "
                "to list rows with total and counts by filename. Never returns filesystem paths."
            ),
            args_schema=AttachmentMetaArgs,
        ),
        "fetch_attachment_text": StructuredTool.from_function(
            coroutine=client.fetch_attachment_text,
            name="fetch_attachment_text",
            description=(
                "Fetch chunked attachment text by fil_id. Requires status=ok and non-inline. "
                "Returns structured errors for missing_file / unreadable."
            ),
            args_schema=FilIdArgs,
        ),
        "search_chunks": StructuredTool.from_function(
            coroutine=client.search_chunks,
            name="search_chunks",
            description="Retrieve ranked document chunks (message body / attachment text) for evidence.",
            args_schema=ChunkSearchArgs,
        ),
    }
    tools = [catalog[name] for name in catalog if name in allow]
    logger.info("built %s tools for role=%s: %s", len(tools), role, [t.name for t in tools])
    return tools
