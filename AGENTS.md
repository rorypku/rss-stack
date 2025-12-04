# Repository Guidelines

## Project Structure & Module Organization
- `docker-compose.yml`: defines the FreshRSS stack (FreshRSS, rss-sync worker, rss-bridge, wechat2rss).
- `apps/freshrss-search`: Python code for embedding sync (`sync_daemon.py`) and semantic search CLI (`search.py`).
- `config/rss-bridge`: configuration mounted into the `rss-bridge` service.
- `data/`: runtime data volumes for FreshRSS, LanceDB, and wechat2rss. Treat as generated data; avoid manual edits where possible.

## Build, Test, and Development Commands
- Copy environment file: `cp .env.example .env`, then fill in required secrets (e.g., `SILICONFLOW_API_KEY`, `LIC_EMAIL`, `LIC_CODE`).
- Start stack: `docker compose up -d` from the repo root (or `docker-compose up -d` depending on your Docker version).
- View service logs: `docker compose logs -f rss-sync` (or another service name).
- Local Python dev (optional): `pip install -r apps/freshrss-search/requirements.txt` and run `python apps/freshrss-search/search.py "your query"`.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints where practical, and small, focused functions.
- Use `snake_case` for functions and variables, `PascalCase` for classes, and descriptive names (avoid single letters except for short loops).
- Follow existing patterns in `apps/freshrss-search` for environment access (`config.py`) and LanceDB integration (`lancedb_utils.py`).

## Testing Guidelines
- There is currently no formal test suite in this repo.
- When adding tests for Python code, prefer `pytest`, place them under `apps/freshrss-search/tests/`, and name files `test_*.py`.
- Ensure new features have at least basic coverage of happy-path and key error cases.

## Commit & Pull Request Guidelines
- Current history uses short messages (e.g., `fix`); please favor concise, imperative summaries (e.g., `Add semantic search CLI`).
- Group related changes in a single commit; avoid mixing refactors and behavior changes without explanation.
- Pull requests should describe the change, note any migration or config steps (e.g., new env vars), and include screenshots or logs when UI or behavior changes are involved.

## Security & Configuration Tips
- Never commit real API keys, license codes, or personal data; rely on `.env` and Docker secrets instead.
- Keep `.env` in sync with `.env.example` when adding new configuration knobs.
