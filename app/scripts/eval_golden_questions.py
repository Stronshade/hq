#!/usr/bin/env python3
"""Golden-question eval harness for interview narrative.

Runs questions against POST /chat/stream (or internal tools) and checks expected
gap mentions / answer keywords. Chat remains read-only.

Usage (API up + models pulled):
  python -m scripts.eval_golden_questions
  python scripts/eval_golden_questions.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class GoldenCase:
    id: str
    question: str
    expect_keywords: list[str]
    expect_gap_kinds: list[str]
    notes: str = ""


CASES: list[GoldenCase] = [
    GoldenCase(
        id="chasers",
        question="Which vendors are chasing unpaid invoices or sending payment reminders?",
        expect_keywords=["chase", "vendor", "reminder"],
        expect_gap_kinds=[],
        notes="Should surface vendor_chase_events via tools",
    ),
    GoldenCase(
        id="invoice_fields",
        question="What invoice numbers and amounts do we have extracted in the inbox?",
        expect_keywords=["invoice"],
        expect_gap_kinds=[],
        notes="Should call get_invoice_fields / search",
    ),
    GoldenCase(
        id="missing_pdf",
        question="Are there any missing or unreadable PDF attachments we should know about?",
        expect_keywords=["missing", "attachment"],
        expect_gap_kinds=["missing_file", "unreadable"],
        notes="Gap taxonomy: missing_file / unreadable should appear when present in data",
    ),
]


def _consume_sse(resp: httpx.Response) -> dict[str, Any]:
    tokens: list[str] = []
    gaps: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    done: dict[str, Any] | None = None
    event = None
    data_lines: list[str] = []

    def flush():
        nonlocal event, data_lines, done
        if event is None:
            data_lines = []
            return
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        if event == "token":
            tokens.append(str(payload.get("text") or ""))
        elif event == "gap":
            gaps.append(payload)
        elif event == "status":
            statuses.append(payload)
        elif event == "done":
            done = payload
        event = None
        data_lines = []

    for line in resp.iter_lines():
        if line == "":
            flush()
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    flush()
    return {
        "answer": "".join(tokens),
        "gaps": gaps,
        "statuses": statuses,
        "done": done or {},
    }


def run_case(client: httpx.Client, base_url: str, case: GoldenCase, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/stream",
        json={"question": case.question, "limit": 12},
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        result = _consume_sse(resp)
    elapsed = time.perf_counter() - t0
    answer_l = result["answer"].lower()
    gap_kinds = {g.get("kind") for g in result["gaps"]}

    kw_hits = [k for k in case.expect_keywords if k.lower() in answer_l or any(k.lower() in str(g).lower() for g in result["gaps"])]
    # Soft: keyword may appear in statuses/tools path for local models that waffle
    if not kw_hits:
        blob = json.dumps(result).lower()
        kw_hits = [k for k in case.expect_keywords if k.lower() in blob]

    gap_ok = True
    missing_gaps: list[str] = []
    if case.expect_gap_kinds:
        # Pass if ANY expected gap kind appears OR the corpus has none (noted)
        if not (gap_kinds & set(case.expect_gap_kinds)):
            # Soft pass when model verbally mentions missing without structured gap
            verbal = any(k.replace("_", " ") in answer_l or k in answer_l for k in case.expect_gap_kinds)
            if not verbal:
                gap_ok = False
                missing_gaps = case.expect_gap_kinds

    passed = bool(kw_hits) and gap_ok
    return {
        "id": case.id,
        "passed": passed,
        "elapsed_s": round(elapsed, 2),
        "keyword_hits": kw_hits,
        "gap_kinds": sorted(gap_kinds),
        "missing_expected_gaps": missing_gaps,
        "answer_confidence": (result.get("done") or {}).get("answer_confidence"),
        "answer_preview": result["answer"][:400],
        "notes": case.notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AP Inbox golden-question eval")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--case", action="append", help="Run only these case ids")
    args = parser.parse_args(argv)

    cases = CASES
    if args.case:
        wanted = set(args.case)
        cases = [c for c in CASES if c.id in wanted]

    print(f"Eval against {args.base_url} ({len(cases)} cases)")
    results = []
    with httpx.Client() as client:
        # health first
        try:
            h = client.get(f"{args.base_url.rstrip('/')}/health", timeout=10.0)
            print("health:", h.status_code, h.text[:200])
        except Exception as exc:  # noqa: BLE001
            print(f"health check failed: {exc}", file=sys.stderr)
            return 2

        for case in cases:
            print(f"\n=== {case.id}: {case.question}")
            try:
                r = run_case(client, args.base_url, case, args.timeout)
            except Exception as exc:  # noqa: BLE001
                r = {"id": case.id, "passed": False, "error": str(exc)}
            results.append(r)
            print(json.dumps(r, indent=2)[:1200])

    passed = sum(1 for r in results if r.get("passed"))
    summary = {"passed": passed, "total": len(results), "results": results}
    print("\n=== SUMMARY ===")
    print(json.dumps({"passed": passed, "total": len(results)}, indent=2))
    out_path = "eval_results.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"wrote {out_path}")
    except OSError:
        pass
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
