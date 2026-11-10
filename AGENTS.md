# AGENTS — Roles and boundaries

This project uses a small multi-role design. Chat uses a **LangChain tool-calling Orchestrator** (not LangGraph, not MCP). Batch writes use a separate **DenoteWorker**.

## Models

| Use | Model | Notes |
|-----|-------|-------|
| Chat + optional LLM refine | `LLM_MODEL` (default preset `qwen3-14b-16k`) | OpenAI-compatible `/v1/chat/completions`. Ollama preset sends `num_ctx=16384`. |
| Embeddings (optional chunks) | `EMBED_MODEL` (default preset `nomic-embed-text`) | Same host as chat unless `EMBED_BASE_URL` is set. Used when denote runs without `--skip-chunks`. |

Chat and DenoteWorker call whatever OpenAI-compatible endpoint `LLM_API_KEY` + `LLM_BASE_URL` point at (OpenAI, Groq, Together, OpenRouter, vLLM, LM Studio, or Ollama `/v1`). Ollama remains the default preset. Prefer host GPU Ollama when using that preset; Docker CPU Ollama is much slower.

## Roles

### Orchestrator (chat)

- **Entry:** `POST /chat/stream` → SSE to Next.js.
- **May:** call allowlisted tools (`search_messages`, `get_annotations`, `get_invoice_fields`, `get_chase_events`, `list_attachment_meta`, `search_chunks`, gated `fetch_attachment_text`).
- **Must not:** write `agent.*`, invent file bytes, browse the attachments directory, run raw SQL.
- **Prompt source:** `app/src/ap_inbox/llm/prompts.py` (`ORCHESTRATOR_SYSTEM_PROMPT`).

### DenoteWorker (batch)

- **Entry:** `python -m ap_inbox.pipeline.denote`
- **Does:** scan attachment status → classify messages/attachments → extract invoice fields → detect chase → optional chunk/embed.
- **Writes:** `agent.message_annotations`, `agent.invoice_fields`, `agent.vendor_chase_events`, `agent.attachment_status`, `agent.document_chunks`, `agent.pipeline_runs`.
- **Flags:** `--no-llm-refine` (fast rules), `--llm-refine`, `--skip-chunks`, `--skip-attachments`, `--resume-unannotated`, `--limit`.

### AttachmentReader (capability)

- Logical role for gated attachment text: `list_attachment_meta` then `fetch_attachment_text` only when `status=ok` and not inline.
- Enforced via tool allowlists + gateway checks (no path leakage).

## Source of truth

| Layer | Schema / store | Mutability |
|-------|----------------|------------|
| Raw Front export | `frontapp.*` | Immutable after import |
| Annotated AP knowledge | `agent.*` | DenoteWorker only |
| Binaries | `attachments/fil_*__filename` | Read-only mount |

The LLM’s memory is **not** authoritative; tools + Postgres are.

## Failure taxonomy

Always surface when applicable:

`missing_file` · `unreadable` · `low_confidence` · `tool_denied` · `tool_error` · `http_error` · `context_overflow` · `no_matches`
