"""Context accounting — refuse silent truncation."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from ap_inbox.chunking import count_tokens
from ap_inbox.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class ContextAccount:
    num_ctx: int
    prompt_tokens: int
    reserved_completion: int
    soft_prompt_budget: int
    truncated: bool
    model: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def account_prompt(
    parts: list[str],
    *,
    settings: Settings | None = None,
    model: str | None = None,
) -> ContextAccount:
    s = settings or get_settings()
    prompt_tokens = sum(count_tokens(p) for p in parts if p)
    soft = s.llm_soft_prompt_budget
    truncated = prompt_tokens > soft
    acct = ContextAccount(
        num_ctx=s.llm_num_ctx,
        prompt_tokens=prompt_tokens,
        reserved_completion=s.llm_reserved_completion,
        soft_prompt_budget=soft,
        truncated=truncated,
        model=model or s.chat_model,
        detail="prompt exceeds soft budget" if truncated else None,
    )
    logger.info("context_account %s", acct.to_dict())
    return acct


def clip_chunks_to_budget(
    chunks: list[dict[str, Any]],
    *,
    remaining_tokens: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Fit retrieved chunks under remaining doc budget. Returns (kept, truncated)."""
    kept: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for ch in chunks:
        cost = int(ch.get("token_count") or count_tokens(ch.get("text") or ""))
        if used + cost > remaining_tokens:
            truncated = True
            break
        kept.append(ch)
        used += cost
    return kept, truncated
