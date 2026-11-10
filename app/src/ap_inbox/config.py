from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://ap:ap_dev_password@localhost:5432/ap_inbox"
    database_url_sync: str = "postgresql+psycopg2://ap:ap_dev_password@localhost:5432/ap_inbox"
    attachments_dir: Path = Path("./attachments")
    web_origin: str = "http://localhost:3000"

    # Phase lock: local Ollama + qwen3:14b (use derived tag with num_ctx baked in when available)
    llm_provider: str = "ollama"  # openai | ollama — this phase defaults to ollama
    openai_api_key: str = "ollama"
    openai_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "qwen3:14b"
    llm_model: str | None = None  # optional override; falls back to openai_model
    llm_num_ctx: int = 16384
    llm_soft_prompt_budget: int = 13000
    llm_reserved_completion: int = 2384
    llm_max_tool_iterations: int = 6
    llm_max_chunks_per_turn: int = 12
    llm_max_attachment_chars: int = 20000

    embed_model: str = "nomic-embed-text"
    chunk_tokens: int = 768
    chunk_overlap: int = 128

    # Internal tool gateway (compose network + shared secret)
    internal_api_token: str = "dev-internal-token-change-me"
    internal_api_base_url: str = "http://127.0.0.1:8000"

    @property
    def chat_model(self) -> str:
        return (self.llm_model or self.openai_model).strip()

    @property
    def ollama_host(self) -> str:
        """Native Ollama HTTP root (no /v1) for embeddings /tokenize / generate options."""
        base = self.effective_base_url
        if base.endswith("/v1"):
            return base[:-3].rstrip("/") or "http://localhost:11434"
        return base.rstrip("/")

    @property
    def effective_base_url(self) -> str:
        if self.llm_provider.lower() == "ollama":
            # Prefer explicit OPENAI_BASE_URL; fall back to compose service name.
            if self.openai_base_url and "openai.com" not in self.openai_base_url:
                return self.openai_base_url.rstrip("/")
            return "http://ollama:11434/v1"
        return self.openai_base_url.rstrip("/")

    @property
    def effective_api_key(self) -> str:
        if self.llm_provider.lower() == "ollama":
            return self.openai_api_key or "ollama"
        return self.openai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
