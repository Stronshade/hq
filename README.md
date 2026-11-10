# AP Inbox Agent

A small Accounts Payable inbox agent for the Eaze `ap@eaze.com` Front export. It classifies emails and attachments, extracts invoice fields, tracks payment-chase vendors, and answers questions over SSE chat (Next.js + FastAPI). Gaps (missing files, unreadable PDFs, low confidence) are surfaced explicitly—never silently.

**Stack:** Postgres 15 · Python FastAPI · LangChain tools · local Ollama (`qwen3-14b-16k`) · Next.js chat UI.

## What reviewers should open first

| Doc | What it is |
|-----|------------|
| [SOUL.md](SOUL.md) | Persona / principles |
| [AGENTS.md](AGENTS.md) | Agent roles & boundaries |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool catalog (LangChain + internal HTTP; not MCP) |
| [docs/skills/](docs/skills/) | Skill write-ups for each capability |
| [docs/architecture.drawio.xml](docs/architecture.drawio.xml) | Architecture diagram (draw.io) |

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- Host [Ollama](https://ollama.com/) with GPU preferred:
  ```bash
  ollama pull qwen3:14b
  ollama pull nomic-embed-text
  # optional Modelfile tag with num_ctx=16384 — see ollama/Modelfile.qwen3-14b-16k
  ```
- Data zip containing:
  - `ap_inbox_candidate.sql`
  - `attachments/` (files named `fil_*__…`)

## 1. Place the data

This GitHub repo does **not** contain `ap_inbox_candidate.sql` or `attachments/`. Those files are the real inbox export (messages and invoice PDFs). Their text can be extracted, so they are gitignored and must not be committed or pushed.

Clone this repo, then **extract the data zip into the repo root** (locally only) so you have:

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

## 2. Configure env (no cloud API keys)

```bash
# macOS / Linux
cp .env.example .env

# Windows PowerShell
copy .env.example .env
```

`.env` uses local placeholders only (`OPENAI_API_KEY=ollama`). Do **not** put real OpenAI keys in the submission package.

For Docker API → **host GPU Ollama**, compose should use `host.docker.internal:11434` (see `.env.example`). Avoid publishing the compose `ollama` service on `11434` while the host daemon already owns that port.

## 3. Start services

```bash
docker compose up -d postgres api
docker compose --profile web up -d --build web
```

- API: http://localhost:8000/health  
- UI: http://localhost:3000  

## 4. Import the SQL dump

```bash
# Windows
.\scripts\import_dump.ps1

# macOS / Linux
./scripts/import_dump.sh
```

Expect ~5705 messages in `frontapp.frontapp_messages`.

## 5. Migrate + annotate (DenoteWorker)

Alembic migrations run on API startup.

The dump and PDFs are not in this repository. A fast rules-only pass that **skips** attachment bytes and chunk embeddings does not cover the full corpus:

```bash
docker compose run --rm --no-deps api \
  python -m ap_inbox.pipeline.denote --no-llm-refine --skip-chunks --skip-attachments
```

To index **all** of the local data (attachment text and chunk search), run denote **without** `--skip-attachments` and **without** `--skip-chunks`. That only works on a machine that has the zip extracted:

```bash
docker compose run --rm --no-deps api \
  python -m ap_inbox.pipeline.denote --no-llm-refine
```

`--llm-refine` is optional (uses Ollama). Omitting the two skip flags is what reads the PDFs and builds embeddings.

## 6. Chat

Open http://localhost:3000 and ask about invoices, chasers, or gaps.

Or:

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"Which vendors are chasing payment?\"}"
```

SSE events: `status` · `token` · `citation` · `gap` · `done` · `error`.

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
                      Ollama (host GPU)
```

Least privilege: the chat LLM never sees filesystem paths; attachment text goes through gated HTTP tools. Only DenoteWorker writes `agent.*`.

## Cost

Runs fully on **local** Ollama—no cloud LLM bill. Cost is electricity / GPU time. First chat call may be slow while the 14B model loads into VRAM.

## License / data

The candidate dump and attachment PDFs are sensitive (real inbox content whose text can be extracted). They are **not** in this repository and must not be committed or pushed to GitHub. They are for this take-home only; do not redistribute them beyond the interview process.
