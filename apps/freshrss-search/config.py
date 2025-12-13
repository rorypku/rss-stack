from __future__ import annotations

import os
from pathlib import Path


def get_env_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required but not set")
    if value is None:
        raise RuntimeError(f"Environment variable {name} is not set and has no default")
    return value


def get_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be a boolean, got {raw!r}")


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


def get_env_path(name: str, default: Path | None = None, required: bool = False) -> Path:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        if required and default is None:
            raise RuntimeError(f"Environment variable {name} is required but not set")
        if default is None:
            raise RuntimeError(f"Environment variable {name} is not set and has no default")
        return default
    return Path(raw).expanduser()


def _detect_freshrss_sqlite_path(data_dir: Path) -> Path:
    users_dir = data_dir / "users"
    candidates = sorted(users_dir.glob("*/db.sqlite")) if users_dir.exists() else []
    if len(candidates) == 1:
        return candidates[0]
    return users_dir / "admin" / "db.sqlite"


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
        self.search_candidate_multiplier = get_env_int("SEARCH_CANDIDATE_MULTIPLIER", default=20)
        self.search_candidate_cap = get_env_int("SEARCH_CANDIDATE_CAP", default=200)

        # Rerank (optional; app-layer rerank for stability across LanceDB versions)
        self.rerank_enabled = get_env_bool("RERANK_ENABLED", default=False)
        self.rerank_model = get_env_str("RERANK_MODEL", default="BAAI/bge-reranker-v2-m3")
        self.rerank_candidates = get_env_int("RERANK_CANDIDATES", default=200)
        self.rerank_timeout_seconds = get_env_int("RERANK_TIMEOUT_SECONDS", default=15)
        self.rerank_max_doc_chars = get_env_int("RERANK_MAX_DOC_CHARS", default=2000)

        # Sync loop
        self.check_interval = get_env_int("CHECK_INTERVAL", default=3600)
        self.retention_days = get_env_int("RETENTION_DAYS", default=90)

        # LanceDB
        self.lancedb_uri = get_env_str(
            "LANCEDB_URI",
            default="/app/lancedb_data/freshrss",
        )

        # FreshRSS sqlite path (for sync + lazy deletion)
        self.freshrss_data_dir = get_env_path(
            "FRESHRSS_DATA_DIR",
            default=Path("/app/data"),
        )
        self.freshrss_sqlite_path = get_env_path(
            "FRESHRSS_SQLITE_PATH",
            default=_detect_freshrss_sqlite_path(self.freshrss_data_dir),
        )

        # Category filter
        self.sync_categories = get_env_list("SYNC_CATEGORIES")


def get_settings() -> Settings:
    return Settings()
