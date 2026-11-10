"""FastAPI entrypoint for the AP Inbox Agent."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ap_inbox.config import get_settings
from ap_inbox.routers import chat, insights, internal

settings = get_settings()

app = FastAPI(title="AP Inbox Agent", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin, "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(insights.router)
app.include_router(chat.router)
app.include_router(internal.router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "ap-inbox-agent",
        "docs": "/docs",
        "chat_stream": "POST /chat/stream",
        "health": "GET /health",
        "internal": "/internal/* (X-Internal-Token)",
        "llm_provider": settings.llm_provider,
        "llm_model": settings.chat_model,
        "llm_num_ctx": str(settings.llm_num_ctx),
    }
