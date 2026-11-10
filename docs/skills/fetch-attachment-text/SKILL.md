# Skill: fetch-attachment-text

## When to use

Need extracted text for a specific `fil_id` after meta says `status=ok` and not inline.

## Tool

- Name: `fetch_attachment_text`
- HTTP: `GET /internal/attachments/{fil_id}/text`
- Gated: least-privilege; structured errors required.

## Outputs

- Chunked text and/or truncated payload under context budget.

## Failure modes

- `missing_file`, `unreadable`, empty payload → treated as errors (never silent success).
