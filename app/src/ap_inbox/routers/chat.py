"""POST /chat/stream — SSE chat via LangChain orchestrator + internal tools.

Preserves the existing Next.js SSE event contract:
  status | gap | citation | token | error | done
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ap_inbox.llm.orchestrator import run_orchestrator

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    limit: int = Field(12, ge=1, le=40)


def _event(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data)}


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> EventSourceResponse:
    run_id = str(uuid.uuid4())

    async def gen():
        try:
            async for ev in run_orchestrator(body.question):
                # Ensure done always carries a run_id for clients
                if ev.event == "done" and "run_id" not in ev.data:
                    ev.data["run_id"] = run_id
                yield _event(ev.event, ev.data)
        except Exception as exc:  # noqa: BLE001
            yield _event("error", {"message": str(exc), "recoverable": False})
            yield _event("done", {"answer_confidence": 0.0, "run_id": run_id})

    return EventSourceResponse(gen())
