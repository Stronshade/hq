# Skill: get-chase-events

## When to use

Answer “who is chasing us?” and “reminder vs escalation vs final notice?” plus whether the invoice is in the inbox.

## Tool

- Name: `get_chase_events`
- HTTP: `GET /internal/chase?vendor=&stage=&limit=`

## Outputs

- Chase events with `invoice_present_in_inbox` boolean.

## Failure modes

- Empty list if no chase annotations; say so.
