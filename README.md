# AP Inbox Agent

A small Accounts Payable inbox agent for the Eaze `ap@eaze.com` Front export. It classifies emails and attachments, extracts invoice fields, tracks payment-chase vendors, and answers questions over SSE chat (Next.js + FastAPI). Gaps (missing files, unreadable PDFs, low confidence) are surfaced explicitly.

**Stack:** Postgres 15 · Python FastAPI · LangChain tools · any OpenAI-compatible LLM · Next.js chat UI.

Chat, batch refine, and embeddings all call one `/v1` endpoint. You point it at a provider with an API key, a base URL, and model names. The default preset is local [Ollama](https://ollama.com/). The same settings work for OpenAI, Groq, Together, OpenRouter, vLLM, or LM Studio.

## What reviewers should open first

| Doc | What it is |
|-----|------------|
| [SOUL.md](SOUL.md) | Persona / principles |
| [AGENTS.md](AGENTS.md) | Agent roles & boundaries |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool catalog (LangChain + internal HTTP; not MCP) |
| [docs/skills/](docs/skills/) | Skill write-ups for each capability |
| [docs/architecture.drawio.xml](docs/architecture.drawio.xml) | Architecture diagram (draw.io) |

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (this guide runs Postgres, the API, and the UI in Compose)
- The data zip (see below)
- A model host, one of:
  - **Ollama on the host** (default), with a GPU if you have one
  - **An API key** for any server that implements `POST /v1/chat/completions` and `POST /v1/embeddings`

For the Ollama preset, pull the models before the first chat:

```bash
ollama pull qwen3:14b
ollama pull nomic-embed-text
# optional Modelfile tag with num_ctx=16384 — see ollama/Modelfile.qwen3-14b-16k
ollama create qwen3-14b-16k -f ollama/Modelfile.qwen3-14b-16k
```

## 1. Place the data

This repo does not contain `ap_inbox_candidate.sql` or `attachments/`. Those files are the real inbox export. Their text can be extracted, so they are gitignored and must stay out of git.

Clone the repo, then extract the data zip into the repo root so you have real files (a missing SQL path makes Compose create a directory and the import will fail):

```text
HQ/
  ap_inbox_candidate.sql
  attachments/
    fil_….pdf
    …
  docker-compose.yml
  app/
  web/
  …
```

## 2. Configure env

```bash
# macOS / Linux
cp .env.example .env

# Windows PowerShell
copy .env.example .env
```

`.env` is gitignored. Put real keys only there.

[`.env.example`](.env.example) ships the **local Ollama** preset, which needs no cloud key (`LLM_API_KEY=ollama`). Inside Docker the API reaches host Ollama at `OPENAI_BASE_URL_DOCKER` (`http://host.docker.internal:11434/v1`). Leave `LLM_BASE_URL` unset for that preset.

To use another provider, replace the LLM block in `.env` with one of the following. Base URLs must end in `/v1`. Chat uses `LLM_MODEL`. Embeddings use `EMBED_MODEL` on `EMBED_BASE_URL` when set, otherwise the same host and key as chat.

**OpenAI**

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
EMBED_MODEL=text-embedding-3-small
```

**Any other OpenAI-compatible host** (Groq, Together, OpenRouter, vLLM, LM Studio, a remote Ollama)

```bash
LLM_PROVIDER=openai
LLM_API_KEY=your-key
LLM_BASE_URL=https://your-host.example/v1
LLM_MODEL=your-chat-model
EMBED_MODEL=your-embed-model
# Only when embeddings live on a different host or key:
# EMBED_BASE_URL=https://your-embed-host.example/v1
# EMBED_API_KEY=your-embed-key
```

`LLM_PROVIDER=ollama` is the only value that sends Ollama-specific `num_ctx`. Every other value is a plain `/v1` call. `LLM_API_KEY` and `LLM_BASE_URL` override `OPENAI_API_KEY` and `OPENAI_BASE_URL` when both are set.

Changing `EMBED_MODEL` changes vector size. After a change, re-run denote without `--skip-chunks` so stored chunk vectors match the new model. Search compares those stored vectors to the query vector.

Provider variables:

| Variable | Role |
|----------|------|
| `LLM_PROVIDER` | `ollama` (default preset) or anything else for a plain `/v1` call |
| `LLM_API_KEY` | Chat API key. Falls back to `OPENAI_API_KEY` |
| `LLM_BASE_URL` | Chat base URL, including `/v1`. Falls back to `OPENAI_BASE_URL` |
| `LLM_MODEL` | Chat model. Falls back to `OPENAI_MODEL` |
| `EMBED_MODEL` | Embedding model |
| `EMBED_BASE_URL` | Optional embeddings base URL. Defaults to the chat base URL |
| `EMBED_API_KEY` | Optional embeddings key. Defaults to the chat key |
| `OPENAI_BASE_URL_DOCKER` | Host Ollama URL used by the API container when the provider is `ollama` and `LLM_BASE_URL` is unset |

## 3. Start services

From the repo root:

```bash
docker compose up -d --build postgres api
docker compose --profile web up -d --build web
```

- API: http://localhost:8000/health
- UI: http://localhost:3000
- API docs: http://localhost:8000/docs

The API image installs Python dependencies at build time. Rebuild (`--build`) after pulling changes that touch `app/requirements.txt`.

Alembic migrations run when the API container starts.

## 4. Import the SQL dump

```bash
# Windows
powershell -ExecutionPolicy Bypass -File .\scripts\import_dump.ps1

# macOS / Linux
bash ./scripts/import_dump.sh
```

Expect about **5705** rows in `frontapp.frontapp_messages`. The script prints the count when it finishes.

## 5. Confirm the database

```bash
curl http://localhost:8000/health
```

A finished import looks like:

```json
{"status":"ok","frontapp_messages":5705}
```

`status: degraded` means the `frontapp` tables are not there yet. Run the import in the previous step. The count is also:

```bash
docker compose exec postgres psql -U ap -d ap_inbox -c "SELECT count(*) FROM frontapp.frontapp_messages;"
```

## 6. Annotate (DenoteWorker)

Rules, invoice regex, and chase detection run in-process. `--llm-refine` is on by default and sends low-confidence rows to whatever chat model `.env` points at. `--no-llm-refine` skips that call.

Fast pass that skips PDF bytes and chunk embeddings (the dump still has to be imported; this does not cover attachment text):

```bash
docker compose run --rm --no-deps api \
  python -m ap_inbox.pipeline.denote --no-llm-refine --skip-chunks --skip-attachments
```

Full local index (attachment text and embeddings). This calls the embeddings endpoint once per chunk:

```bash
docker compose run --rm --no-deps api \
  python -m ap_inbox.pipeline.denote --no-llm-refine
```

Same pass, with the configured chat model refining low-confidence classify and invoice fields:

```bash
docker compose run --rm --no-deps api \
  python -m ap_inbox.pipeline.denote
```

## 7. Chat

Open http://localhost:3000 and ask about invoices, chasers, or gaps.

Or:

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"Which vendors are chasing payment?\"}"
```

SSE events: `status` · `token` · `citation` · `gap` · `done` · `error`.

The first Ollama call can sit while the 14B model loads into VRAM. A cloud model answers as soon as the provider accepts the key.

## Suggested demo questions

1. Which vendors chase payment, and how far did they go (reminder / escalation / final notice)?
2. Show key fields for a recent invoice (vendor, number, amount, dates).
3. Are there missing or unreadable attachments? What gaps do we know about?
4. How many messages are classified as invoices vs payment reminders?

## Architecture (short)

```text
Zip data → Postgres (frontapp.*) → DenoteWorker → agent.* annotations
                                              ↘ invoice_fields, chase, attachment_status

Next.js  --SSE-->  FastAPI Orchestrator  --LangChain tools-->  /internal/*  -->  Postgres + attachments (RO)
                         |
                 OpenAI-compatible LLM
                 (LLM_API_KEY + LLM_BASE_URL + LLM_MODEL)
```

Least privilege: the chat LLM never sees filesystem paths; attachment text goes through gated HTTP tools. Only DenoteWorker writes `agent.*`.

## Troubleshooting

| What you see | What to do |
|--------------|------------|
| Compose says `ap_inbox_candidate.sql` is not a directory, or the import says the dump was not found | Extract the zip into the repo root so `ap_inbox_candidate.sql` is a file and `attachments/` is a folder. If Compose already created an empty directory at that path, delete the directory and extract the file again. |
| `GET /health` returns `degraded` | Postgres is up and the dump is not imported. Run step 4. |
| API logs `connection refused` to port `11434` | Host Ollama is not running, or nothing is listening on `11434`. Start Ollama, or set `LLM_BASE_URL` to the provider you mean to use. |
| Port `11434` already allocated | The host Ollama daemon owns that port. Leave the Compose `local-llm` profile off. To use the CPU container instead, stop host Ollama, set `OPENAI_BASE_URL_DOCKER=http://ollama:11434/v1`, then `docker compose --profile local-llm up -d`. |
| Chat or denote returns `401` / `invalid api key` | `LLM_API_KEY` does not match `LLM_BASE_URL`. The model name has to be one that host serves (`LLM_MODEL`, `EMBED_MODEL`). |
| Chunk search misses text you know is in the corpus | Embeddings were built with a different `EMBED_MODEL` than the one in `.env` now. Re-run denote without `--skip-chunks`. |
| Denote embed warnings, empty chunk scores | The embeddings URL or model rejected the request. `EMBED_MODEL` must exist on `EMBED_BASE_URL` (or on `LLM_BASE_URL` when the embed URL is unset). |

## License / data

The candidate dump and attachment PDFs are sensitive (real inbox content whose text can be extracted). They are not in this repository and must not be committed or pushed. They are for this take-home only; do not redistribute them beyond the interview process.
