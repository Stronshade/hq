# Tools

This agent does **not** use MCP servers. Tools are **LangChain `StructuredTool`s** that call an **internal FastAPI gateway** (`/internal/*`) with header `X-Internal-Token`. That is the MCP-equivalent surface for this take-home: explicit tool schemas, least-privilege allowlists, structured errors.

Implementation:

- Wrappers: [`app/src/ap_inbox/llm/tools.py`](../app/src/ap_inbox/llm/tools.py)
- Routes: [`app/src/ap_inbox/routers/internal.py`](../app/src/ap_inbox/routers/internal.py)
- Backend: [`app/src/ap_inbox/services/tool_backend.py`](../app/src/ap_inbox/services/tool_backend.py)

## Catalog

| Tool | HTTP | Role allowlist | Purpose |
|------|------|----------------|---------|
| `search_messages` | `GET /internal/messages/search` | Orchestrator | Keyword search over subjects/bodies/vendors/invoices |
| `get_annotations` | `GET /internal/messages/{id}/annotations` | Orchestrator | Message `doc_type` + confidence |
| `get_invoice_fields` | `GET /internal/invoices` | Orchestrator | Extracted invoice fields |
| `get_chase_events` | `GET /internal/chase` | Orchestrator | Chase stage + invoice-in-inbox |
| `list_attachment_meta` | `GET /internal/attachments/{fil_id}` | Orchestrator, AttachmentReader | Meta only (no FS paths) |
| `fetch_attachment_text` | `GET /internal/attachments/{fil_id}/text` | Orchestrator (gated), AttachmentReader | Chunked text or structured error |
| `search_chunks` | `GET /internal/chunks/search` | Orchestrator | Ranked body/attachment chunks |

## Gap / error codes returned by tools

| Code | Meaning |
|------|---------|
| `missing_file` | `fil_*` referenced in SQL but file not on disk |
| `unreadable` | File present but text extract failed / empty PDF |
| `low_confidence` | Weak classify/extract |
| `tool_denied` | Role or validation rejected the call |
| `http_error` / `tool_error` | Gateway or unexpected failure |

Empty “success” payloads for attachment text are treated as failures in the orchestrator.

## Auth

- Header: `X-Internal-Token: <INTERNAL_API_TOKEN>`
- Default in `.env.example` is a **dev placeholder**, not a production secret.
- Chat LLM never receives the token; only the server-side tool client does.
