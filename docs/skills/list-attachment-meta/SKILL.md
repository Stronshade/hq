# Skill: list-attachment-meta

## When to use

Before reading attachment contents: check status (`ok`, `missing_file`, `unreadable`, `skipped_inline`) and filename. Also list every attachment in one status, for example “which files are missing from disk?”

**Never returns filesystem paths.**

## Tool

- Name: `list_attachment_meta`
- HTTP: `GET /internal/attachments/{fil_id}`
- HTTP (no id): `GET /internal/attachments?status=missing_file&limit=`

## Outputs

- One id: `{ fil_id, count, items: [{ fil_id, message_id, filename, content_type, is_inline, status, error_detail }] }`
- Status list: `total`, `returned`, `truncated`, `by_filename`, and compact items (`fil_id`, filename, `error_detail`, `message_id`).

Quote `total`, the filename counts, and `error_detail`. Do not guess file contents.

## Failure modes

- A bad status on one `fil_id`, or a status list with rows, becomes one SSE `gap` (`missing_file`, `unreadable`, or `low_confidence`), not one event per row.
- Fetch text only when `status=ok` and not inline. Otherwise stop.
