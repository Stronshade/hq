# Skill: chat-sse

## When to use

Human Q&A about the inbox (interview demo).

## Surface

- UI: Next.js at http://localhost:3000
- API: `POST /chat/stream` with JSON `{ "question": "..." }`
- Transport: Server-Sent Events (`text/event-stream`)
- Demo questions are preflighted onto the matching read-only tool before the model writes. Chunk prefetch is skipped for those questions. Gaps are one summary per kind.

## Events

| Event | Payload |
|-------|---------|
| `status` | `{ stage, detail }` |
| `token` | `{ text }` |
| `citation` | `{ message_id, confidence, note }` |
| `gap` | `{ kind, fil_id?, message_id?, detail }` |
| `done` | `{ answer_confidence, run_id, trace? }` |
| `error` | `{ message, recoverable }` |

Client: `web/lib/sse.ts` (POST + ReadableStream parser; not raw `EventSource`).
