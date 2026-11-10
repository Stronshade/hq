# Deploy on Render (free tier)

One Git repo hosts two Render web services. Postgres is a third Render resource. DenoteWorker runs once on this PC, then the annotated database is restored into Render. The hosted API only reads that database.

```text
Browser  →  Render web service (web/)
         →  Render API service (app/)  →  Render Postgres
                                    ↘  Gemini API (Google AI Studio)
```

The Front dump and `attachments/` stay gitignored. They are not part of the git deploy.

Free-tier shape:

| Piece | Render plan | Limit that matters here |
|---|---|---|
| API web service | Free | 512 MB RAM, sleeps after 15 minutes idle, port **8000** |
| Web web service | Free | Same sleep behavior, port **3000** |
| Postgres | Free | 1 GB, one database per workspace, expires 30 days after creation |

Both web services share 750 free instance hours per month. Hours count only while a service is awake. Leave them idle between demos. An uptime monitor that hits both all month uses the hours up (two always-on services are about 1,440 hours).

The free database is deleted 14 days after it expires unless you upgrade. There are no backups on the free plan.

## 0. Push the repo

Render builds from Git. Push this repo to GitHub or GitLab and grant Render access.

Leave these untracked. They are already gitignored:

- `.env` (API keys)
- `ap_inbox_candidate.sql`
- `attachments/`
- the database dump you create in step 2

Create the API key in [Google AI Studio](https://aistudio.google.com/). Copy the chat model id shown there (for example `gemini-2.5-flash`). Embeddings use `gemini-embedding-001` on the same key.

Pick two service names before you create anything. Render's default URLs are:

- API: `https://<api-name>.onrender.com`
- Web: `https://<web-name>.onrender.com`

The API's `WEB_ORIGIN` has to be the web URL. Creating the web service second is fine as long as the name matches.

Use the same Render region for the database and both web services. The internal database host only resolves in that region.

## 1. Annotate on this PC

From the repo root, with Docker Desktop running and `ap_inbox_candidate.sql` extracted at the repo root.

Start Postgres and the API so Alembic creates the `agent` schema on boot:

```powershell
docker compose up -d --build postgres api
```

Import the Front dump. The script strips `\restrict` and `\unrestrict` lines. A raw `psql` of this dump stops on those lines.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\import_dump.ps1
```

Expect about 5705 rows. Confirm:

```powershell
curl.exe http://localhost:8000/health
```

`"status":"ok"` and a message count means the import landed.

Annotate without calling Gemini and without reading PDFs:

```powershell
docker compose run --rm --no-deps api python -m ap_inbox.pipeline.denote --no-llm-refine --skip-chunks --skip-attachments
```

That pass writes message classifications, invoice fields, and chase events from message text into `agent.*`. It does not fill `agent.document_chunks`, so semantic chunk search returns no rows. It also skips the attachment scan, so missing-file and unreadable gap rows are empty.

Invoice, chase, and "how many invoices" questions still answer from those tables. They use the preflight tools and do not need embeddings.

This pass walks every message. Let it finish and print a result dict before you dump the database.

## 2. Dump the local database

Write the dump outside the repo so it cannot be committed. `--no-owner --no-acl` keeps the local `ap` role out of the file. Render's database user cannot assume that role.

```powershell
docker compose exec -T postgres pg_dump -U ap -d ap_inbox --no-owner --no-acl -Fc -f /tmp/ap_inbox_render.dump
docker compose cp postgres:/tmp/ap_inbox_render.dump "$env:TEMP\ap_inbox_render.dump"
```

Custom format (`-Fc`) avoids PowerShell rewriting the file as UTF-16. Leave the file in `%TEMP%`.

## 3. Create Render Postgres and restore

In the Render dashboard, create a **PostgreSQL** instance on the **Free** plan, in the same region you will use for the web services.

Open the database and copy the **External Database URL**. It looks like:

```text
postgresql://USER:PASSWORD@dpg-xxxx.region-postgres.render.com/DBNAME
```

Restore from this PC. The external URL requires TLS. `PGPASSWORD` keeps the password out of the command line if you prefer; the URL form below is the one Render gives you. If the password contains `@`, `:`, or `/`, URL-encode it inside the connection string.

```powershell
docker run --rm -v "${env:TEMP}:/dump:ro" postgres:15 `
  pg_restore --no-owner --no-acl --exit-on-error `
  --dbname "postgresql://USER:PASSWORD@dpg-xxxx.region-postgres.render.com/DBNAME?sslmode=require" `
  /dump/ap_inbox_render.dump
```

`pg_restore` may print harmless warnings about the `postgres` role or comments. `--exit-on-error` still fails the command on a real error. A fresh free database is empty, so this restore creates `frontapp`, `agent`, and `alembic_version`.

Do not restore with the internal URL from your PC. That host only exists inside Render.

## 4. API web service

Create a **Web Service**. Connect the Git repo.

| Setting | Value |
|---|---|
| Language / runtime | Docker |
| Root directory | `app` |
| Dockerfile path | `Dockerfile` |
| Instance type | Free |
| Port | `8000` |

The image listens on 8000. Render's `PORT` value is ignored by this process, so the dashboard port has to be 8000.

Leave **Docker Command empty**. Render splits that field on spaces and passes the pieces to `alembic`, so `&&` becomes an argument and the process exits. The image command in [app/Dockerfile](../app/Dockerfile) already runs migrations and then uvicorn. Local Compose overrides that command so this PC still uses `--reload`.

### Environment

Copy the **Internal Database URL** from the Postgres instance. It starts with `postgresql://`. This image does not install the `psycopg` driver that plain `postgresql://` selects, so change the scheme and keep the user, password, host, port, and database name exactly as Render shows them. Internal connections stay on Render's private network and do not use `sslmode`.

```text
postgresql://USER:PASSWORD@HOST/DBNAME
```

becomes both of these:

```text
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST/DBNAME
DATABASE_URL_SYNC=postgresql+psycopg2://USER:PASSWORD@HOST/DBNAME
```

Alembic reads `DATABASE_URL_SYNC` when the container starts. Both variables have to be set. URL-encode special characters in the password.

Google AI Studio:

```text
LLM_PROVIDER=openai
LLM_API_KEY=your-ai-studio-key
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=the-chat-model-id-from-ai-studio
EMBED_MODEL=gemini-embedding-001
```

`LLM_BASE_URL` stops at `/openai`. The API client appends `/chat/completions` and `/embeddings`. A trailing `/v1` on that URL returns 404 from Google.

`LLM_PROVIDER` must be `openai` (or any value other than `ollama`). `ollama` adds a `num_ctx` field that Gemini rejects.

Leave `EMBED_BASE_URL` and `EMBED_API_KEY` unset so embeddings use the same key and host.

App URLs:

```text
WEB_ORIGIN=https://<web-name>.onrender.com
INTERNAL_API_TOKEN=a-long-random-secret
INTERNAL_API_BASE_URL=http://127.0.0.1:8000
```

`WEB_ORIGIN` is the web service origin with no path and no trailing slash. CORS allows that origin plus localhost. `INTERNAL_API_BASE_URL` stays on loopback: the orchestrator calls its own tools inside the API container.

Deploy. The first build installs Python dependencies and takes several minutes. Then open:

```text
https://<api-name>.onrender.com/health
```

A finished restore looks like:

```json
{"status":"ok","frontapp_messages":5705}
```

`degraded` means the `frontapp` tables are missing. Run the restore in step 3 again and check the external URL, the region, and `sslmode=require`.

The first request after idle takes about a minute while the free instance wakes. Render shows its own loading page during that minute.

## 5. Web web service

Create a second **Web Service** from the same repo.

| Setting | Value |
|---|---|
| Language / runtime | Docker |
| Root directory | `web` |
| Dockerfile path | `Dockerfile` |
| Instance type | Free |
| Port | `3000` |

Set this environment variable **before the first deploy**:

```text
NEXT_PUBLIC_API_URL=https://<api-name>.onrender.com
```

[web/Dockerfile](../web/Dockerfile) declares `ARG NEXT_PUBLIC_API_URL` and bakes it into the browser client at build time. Render passes environment variables into that build arg. If the live site still calls `http://localhost:8000`, set the variable, clear the build cache, and redeploy.

The service name has to match the host you put in `WEB_ORIGIN`. If Render assigns a different host, update `WEB_ORIGIN` on the API service and redeploy the API. CORS is read when the API process starts.

## 6. Try the demo

Open `https://<web-name>.onrender.com`. In the browser network panel, chat requests should go to `https://<api-name>.onrender.com/chat/stream`.

Ask:

1. Which vendors chase payment, and how far did they go (reminder / escalation / final notice)?
2. Show key fields for a recent invoice (vendor, number, amount, dates).
3. How many messages are classified as invoices vs payment reminders?

Wake the API with `/health` first if the tab has been idle. The web service sleeps on its own schedule too.

## What the free deploy leaves out

- **PDFs.** The free web service disk is wiped on sleep and redeploy, and free instances cannot mount a disk. `fetch_attachment_text` returns `missing_file`. Fields already stored in `agent.invoice_fields` still answer.
- **Chunk search.** `--skip-chunks` left `agent.document_chunks` empty. A question that misses the preflight tools still calls Gemini embeddings once, then finds no chunk rows. Filling those vectors means running denote again without `--skip-chunks` against `gemini-embedding-001`. That pass is one embeddings call per chunk and is larger than the free Gemini quota for this inbox. Vectors from the local `nomic-embed-text` model cannot be mixed with Gemini query vectors. Lengths differ, and cosine similarity then scores 0.
- **Auth.** `POST /chat/stream` has no login. Message text in the question and in tool results is sent to Google. Keep both `onrender.com` URLs private.
- **Database lifetime.** Export another dump before day 30 if you still need the data. Free Render Postgres does not survive past the grace period.

## When something fails

| What you see | What to do |
|---|---|
| `alembic: error: unrecognized arguments: && uvicorn ...` or `sh: 1: alembic upgrade head && uvicorn ...: not found` | Clear the Docker Command field so it is empty, then redeploy. The image command starts the API. |
| API build cannot find `requirements.txt` | Root directory is the repo root. Set it to `app`. |
| Web build cannot find `package.json` | Set the web service root directory to `web`. |
| `/health` is 502 for about a minute | The free instance is waking. Wait and retry. |
| `/health` stays `degraded` | The restore did not create `frontapp.frontapp_messages`. Check the external URL and `pg_restore` output. |
| `ModuleNotFoundError: No module named 'psycopg'` | `DATABASE_URL` is still `postgresql://`. Change it to `postgresql+asyncpg://` and set `DATABASE_URL_SYNC` to `postgresql+psycopg2://` with the same user, password, host, and database. |
| API logs `password authentication failed` | The internal URL was edited incorrectly, or a special character in the password was not URL-encoded. |
| API logs `could not translate host name` | The service is using the external host from inside Render, or it is in a different region from the database. Use the internal host in the same region. |
| Chat returns 401 or `invalid api key` | `LLM_API_KEY` does not match the AI Studio key. `LLM_BASE_URL` has to be `https://generativelanguage.googleapis.com/v1beta/openai`. |
| Chat returns 404 from Google | `LLM_BASE_URL` includes an extra `/v1`, or `LLM_MODEL` is not an id AI Studio serves. |
| Browser calls `localhost:8000` | `NEXT_PUBLIC_API_URL` was missing at build time. Set it, clear the build cache, redeploy the web service. |
| Browser shows a CORS error | `WEB_ORIGIN` on the API is not the web origin. Update it and redeploy the API. |
| Invoice and chase answers are empty | Denote did not finish before the dump, or the restore used an older dump from before denote. |
