# Skill: classify-inbox

## When to use

Batch-label each email (and non-inline attachment) as `invoice`, `statement`, `payment_reminder`, `remittance`, `escalation`, `final_notice`, or `other`.

## How it works

1. **Rules first** (`rules-v1`) on subject, body excerpt, filenames.
2. Optional **`--llm-refine`**: escalate low-confidence / weak `other` to local Qwen JSON classify.
3. Persist to `agent.message_annotations` and attachment `doc_*` columns on `agent.attachment_status`.

## Inputs

- Front message rows + attachment filenames / PDF text excerpts when available.

## Outputs

- `doc_type`, `confidence`, `rationale`, `model`, `status` (`ok` | `low_confidence` | `error`).

## Failure modes

- Empty text → `other` + `low_confidence` with detail.
- LLM timeout/parse error → keep rules result or `error` with `error_detail` (never silent).
