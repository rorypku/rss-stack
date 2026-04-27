```bash
cd /Users/kai/docker/rss-stack

# 查看命令帮助：
docker compose exec rss-sync python search.py --help

# 实际检索示例：
docker compose exec rss-sync python search.py "Oracle 财报怎么样" --limit 1

# 根据 search.py 输出中的 entry_id 拉取全文纯文本：
docker compose exec rss-sync python fetch_entry.py 12345

# 手动检查知识星球 token 是否还有效（默认会读取 .env 里的 ZSXQ_ACCESS_TOKEN）：
uv run scripts/check_zsxq_token.py

# 从 Chrome Profile 4 读取知识星球 token 并写回 .env：
uv run scripts/refresh_zsxq_token_from_chrome.py

# 让 rsshub 重新读取更新后的 .env：
docker compose up -d rsshub
```
