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
| `get_annotations` | `GET /internal/messages/{id}/annotations` or `GET /internal/annotations` | Orchestrator | One message, or inbox totals by `doc_type` / `status` plus a rationale histogram |
| `get_invoice_fields` | `GET /internal/invoices` | Orchestrator | Extracted invoice fields with `total`, `returned`, `truncated`, optional `status` |
| `get_chase_events` | `GET /internal/chase` | Orchestrator | Chase stage, subject, `stage_counts`, and invoice-in-inbox |
| `list_attachment_meta` | `GET /internal/attachments/{fil_id}` or `GET /internal/attachments?status=` | Orchestrator, AttachmentReader | Meta only (no FS paths). Status list returns totals and filename counts |
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

Structured demo questions are preflighted onto these tools. Chunk prefetch is skipped for those questions so an unrelated “inbox” chunk cannot hide a `no_matches` result. The orchestrator emits one summary gap per kind (`low_confidence`, `missing_file`, `unreadable`, `no_matches`), not one event per row.

### Filters

- `get_annotations` without `message_id` returns `by_doc_type`, `by_status`, and the low-confidence/error rationale histogram. `doc_type` and/or `status` add `total` and a short subject sample.
- `get_invoice_fields` accepts `status`. A named number with `total=0` returns `found: false`. Digit-only matching runs only when the query contains a 6+ digit run (`ZZ-DOES-NOT-EXIST-999` does not collapse to `999`). `status=unreadable` returns `by_error_detail` and a few sample rows, not every failed PDF.
- `get_chase_events` accepts `stage` (`final` and `final notice` alias to `final_notice`), optional `present`, and always returns `stage_counts` (computed before `LIMIT`), `total`, `truncated`, `subject`, and `match_policy`. Present means exact case-insensitive equality against extracted invoice numbers. The page keeps at least one row of each stage.
- `list_attachment_meta` without `fil_id` lists `GET /internal/attachments?status=missing_file` with `total`, `by_filename`, and compact items (`fil_id`, filename, `error_detail`, `message_id`). No filesystem paths.

## Auth

- Header: `X-Internal-Token: <INTERNAL_API_TOKEN>`
- Default in `.env.example` is a **dev placeholder**, not a production secret.
- Chat LLM never receives the token; only the server-side tool client does.
