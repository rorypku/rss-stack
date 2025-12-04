from __future__ import annotations

import os


def get_env_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required but not set")
    if value is None:
        raise RuntimeError(f"Environment variable {name} is not set and has no default")
    return value


def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def get_env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    def __init__(self) -> None:
        # SiliconFlow / OpenAI-compatible config
        self.siliconflow_api_key = get_env_str("SILICONFLOW_API_KEY", required=True)
        self.siliconflow_base_url = get_env_str(
            "SILICONFLOW_BASE_URL",
            default="https://api.siliconflow.cn/v1",
        )

        # Embedding config
        self.embedding_model = get_env_str(
            "EMBEDDING_MODEL",
            default="Qwen/Qwen3-Embedding-8B",
        )
        self.embedding_dim = get_env_int("EMBEDDING_DIM", default=4096)
        self.embedding_batch_size = get_env_int("EMBEDDING_BATCH_SIZE", default=20)

        # Chunking / search
        self.chunk_size = get_env_int("CHUNK_SIZE", default=200)
        self.chunk_overlap = get_env_int("CHUNK_OVERLAP", default=100)
        self.search_threshold = float(os.getenv("SEARCH_THRESHOLD", "0.5"))

        # Sync loop
        self.check_interval = get_env_int("CHECK_INTERVAL", default=3600)
        self.retention_days = get_env_int("RETENTION_DAYS", default=90)

        # LanceDB
        self.lancedb_uri = get_env_str(
            "LANCEDB_URI",
            default="/app/lancedb_data/freshrss",
        )

        # Category filter
        self.sync_categories = get_env_list("SYNC_CATEGORIES")


def get_settings() -> Settings:
    return Settings()
