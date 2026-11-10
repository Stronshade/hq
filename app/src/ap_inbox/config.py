from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _stripped(value: str | None) -> str:
    return (value or "").strip()


def _is_loopback(url: str) -> bool:
    lowered = url.lower()
    return "://localhost" in lowered or "://127.0.0.1" in lowered or "://[::1]" in lowered


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

    # OpenAI-compatible chat. Default preset is local Ollama (`/v1`).
    # LLM_API_KEY / LLM_BASE_URL win; OPENAI_* remain as fallbacks.
    llm_provider: str = "ollama"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    openai_api_key: str = "ollama"
    openai_base_url: str = "http://localhost:11434/v1"
    openai_base_url_docker: str | None = None
    openai_model: str = "qwen3:14b"
    llm_model: str | None = None  # optional override; falls back to openai_model
    llm_num_ctx: int = 16384
    llm_soft_prompt_budget: int = 13000
    llm_reserved_completion: int = 2384
    llm_max_tool_iterations: int = 6
    llm_max_chunks_per_turn: int = 12
    llm_max_attachment_chars: int = 20000
    # Set by docker-compose on the api service. Absent for host-side processes.
    running_in_compose: bool = False

    embed_model: str = "nomic-embed-text"
    embed_base_url: str | None = None
    embed_api_key: str | None = None
    chunk_tokens: int = 768
    chunk_overlap: int = 128

    # Internal tool gateway (compose network + shared secret)
    internal_api_token: str = "dev-internal-token-change-me"
    internal_api_base_url: str = "http://127.0.0.1:8000"

    @property
    def is_ollama(self) -> bool:
        return self.llm_provider.strip().lower() == "ollama"

    @property
    def chat_model(self) -> str:
        return (_stripped(self.llm_model) or self.openai_model).strip()

    @property
    def effective_base_url(self) -> str:
        """OpenAI-compatible root, including `/v1`."""
        explicit = _stripped(self.llm_base_url)
        if explicit:
            return explicit.rstrip("/")
        host = _stripped(self.openai_base_url)
        if self.is_ollama and self.running_in_compose:
            docker = _stripped(self.openai_base_url_docker)
            # localhost inside the api container is not the host Ollama daemon.
            if docker and (not host or _is_loopback(host)):
                return docker.rstrip("/")
        if host:
            return host.rstrip("/")
        if self.is_ollama:
            return "http://localhost:11434/v1"
        return "https://api.openai.com/v1"

    @property
    def effective_api_key(self) -> str:
        key = _stripped(self.llm_api_key) or _stripped(self.openai_api_key)
        if self.is_ollama:
            return key or "ollama"
        return key

    @property
    def effective_embed_base_url(self) -> str:
        explicit = _stripped(self.embed_base_url)
        return (explicit or self.effective_base_url).rstrip("/")

    @property
    def effective_embed_api_key(self) -> str:
        explicit = _stripped(self.embed_api_key)
        return explicit or self.effective_api_key

    @property
    def ollama_host(self) -> str:
        """Native Ollama HTTP root (no `/v1`), derived from the chat base URL."""
        base = self.effective_base_url
        if base.endswith("/v1"):
            return base[:-3].rstrip("/") or "http://localhost:11434"
        return base.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
