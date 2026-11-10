# Skill: list-attachment-meta

## When to use

Before reading attachment contents: check status (`ok`, `missing_file`, `unreadable`, `skipped_inline`) and filename. **Never returns filesystem paths to the LLM.**

## Tool

- Name: `list_attachment_meta`
- HTTP: `GET /internal/attachments/{fil_id}`

## Failure modes

- Missing / unreadable statuses become SSE `gap` events in chat.
