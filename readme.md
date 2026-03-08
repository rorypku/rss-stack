```bash
cd /Users/kai/docker/rss-stack

# 查看命令帮助：
docker compose exec rss-sync python search.py --help

# 实际检索示例：
docker compose exec rss-sync python search.py "Oracle 财报怎么样" --limit 1

# 根据 search.py 输出中的 entry_id 拉取全文纯文本：
docker compose exec rss-sync python fetch_entry.py 12345
```
