# FreshRSS Semantic Search – Task Breakdown

本文档基于 `spec.md` 和 `db_shema.md`，将实现工作拆分为若干阶段，便于逐步落地与验证。

## Phase 0：项目布局与基础配置

- 在 `/Users/kai/docker-data` 下创建 `freshrss-search/` 目录，作为 sidecar Python 项目根目录。
- 定义环境变量读取约定（可通过 `config.py` / `settings.py` 封装）：
  - `SILICONFLOW_API_KEY`, `SILICONFLOW_BASE_URL`
  - `EMBEDDING_MODEL`, `EMBEDDING_DIM`
  - `CHUNK_SIZE`, `CHUNK_OVERLAP`, `SEARCH_THRESHOLD`, `CHECK_INTERVAL`
  - `RETENTION_DAYS`, `LANCEDB_URI`, `SYNC_CATEGORIES`, `EMBEDDING_BATCH_SIZE`
- 确认 Docker 侧挂载：
  - FreshRSS SQLite：`./data:/app/data:ro`
  - LanceDB 持久化目录：`./lancedb_data:/app/lancedb_data`
  - Python 代码目录：`./freshrss-search:/app/code`

## Phase 1：项目脚手架与依赖

- 在 `freshrss-search/` 下创建基本文件：
  - `requirements.txt`
  - `sync_daemon.py`
  - `search.py`
  - `config.py`（环境变量封装）
  - `db_utils.py`（SQLite 访问封装）
  - `lancedb_utils.py`（LanceDB 连接与表初始化）
- 在 `requirements.txt` 中声明依赖：
  - `lancedb`
  - `pandas`
  - `openai`
  - HTML 清洗所需库（如 `beautifulsoup4` 或 `lxml`）。

## Phase 2：LanceDB 集成与 Schema 定义

- 在 `lancedb_utils.py` 中实现：
  - 从 `LANCEDB_URI` 读取 LanceDB 数据库 URI（默认 `dir:/app/lancedb_data/freshrss`）。
  - 打开/创建数据库与两张表：
    - `rss_chunks`（RssChunk）
    - `sync_state`（同步状态）
- 定义 RssChunk 模型（可使用 LanceModel）：
  - 字段：`entry_id`, `published_at`, `feed_id`, `category_id`, `category_name`,
    `title`, `link`, `chunk_index`, `content`, `vector`。
  - `content` 标注为 `SourceField`，`vector` 标注为 `VectorField`。
- 定义 `sync_state` 的访问辅助函数：
  - 读取当前 `last_entry_id`（无记录则返回 0）。
  - 更新 `last_entry_id` / `last_sync_at`。

## Phase 3：SQLite 访问、清洗与切块

- 在 `db_utils.py` 中实现：
  - 打开 FreshRSS SQLite（只读）连接。
  - 读取 `category` 表并构建 `name -> id` 映射。
  - 基于 `SYNC_CATEGORIES` 解析允许的 `category_id` 集合。
  - 查询增量文章：
    - 按 `entry.id > last_entry_id` 过滤。
    - join `feed` / `category`，并根据允许的 `category_id` 做可选过滤。
    - 返回包含 `entry`、`feed`、`category` 所需字段的行。
- 实现文本清洗函数：
  - 从 `entry.content` 去 HTML 标签。
  - 合并多余空白、规范换行。
- 实现切块函数：
  - 使用 `CHUNK_SIZE` / `CHUNK_OVERLAP` 将清洗后的文本切分为多个片段。
  - 为每个片段生成对应的 `chunk_index`。

## Phase 4：增量同步守护进程与 TTL 清理

- 在 `sync_daemon.py` 中实现主循环：
  - 初始化 SiliconFlow Embedding registry（OpenAI-compatible）。
  - 每轮执行：
    1. 从 `sync_state` 获取 `last_entry_id`。
    2. 调用 `db_utils` 获取新文章（支持 `SYNC_CATEGORIES` 过滤）。
    3. 对每篇文章执行：清洗 → 切块 → 构造 RssChunk 记录；将 RssChunk 记录按 `EMBEDDING_BATCH_SIZE`（例如 20）分批聚合，并以批次方式调用 LanceDB 的 `rss_chunks.add()`，使底层 Embedding 客户端以批量请求方式调用 SiliconFlow API，减少网络开销并降低触发速率限制的风险。
    4. 更新 `sync_state` 中的 `last_entry_id` / `last_sync_at`。
  - 循环末尾 `time.sleep(CHECK_INTERVAL)`。
- 在同步循环中或单独周期任务中实现 TTL 清理：
  - 基于 `RETENTION_DAYS`（默认 90）计算过期阈值。
  - 删除 `published_at < now - RETENTION_DAYS` 的切片记录。

## Phase 5：搜索 CLI 与懒删除

- 在 `search.py` 中实现 CLI：
  - 解析命令行参数（query，`--limit`，未来可选 `--category` 等）。
  - 复用 LanceDB 配置与 Embedding registry，打开 `rss_chunks`。
  - 使用 `table.search(query)` 获取候选结果。
  - 应用 `SEARCH_THRESHOLD` 过滤 `_distance`。
  - 按 `entry_id` 聚合，保留每篇文章的最佳切片。
- 实现懒删除逻辑：
  - 收集当前候选结果中所有去重后的 `entry_id`，通过一条 `SELECT id FROM entry WHERE id IN (...)` 的 SQL 一次性回查 SQLite，得到实际存在的 ID 集合：
    - 若 `entry_id` 在 SQLite 查询结果中存在：保留对应结果。
    - 若 `entry_id` 不存在：
      - 从当前结果集中跳过该条。
      - 对这些 `entry_id` 调用 `table.delete(where="entry_id IN (...)")` 批量删除对应文章的所有切片。

## Phase 6：Docker 集成与验证

- 在 `docker-compose.yml` 中配置 `rss-sync` 服务：
  - 使用 `python:3.9-slim` 镜像。
  - 挂载数据卷与代码目录。
  - 设置环境变量（包括 `LANCEDB_URI`、`SYNC_CATEGORIES`、`RETENTION_DAYS` 等）。
  - 启动命令：安装依赖并运行 `sync_daemon.py`。
- 验证流程：
  - 首次启动：确认能成功连接 SiliconFlow 并生成向量，`rss_chunks` / `sync_state` 建表成功。
  - 向 FreshRSS 中新增文章：确认增量同步生效，搜索能命中新文章。
  - 从 FreshRSS 删除文章：确认搜索不再展示该文章，并在下一次命中时触发懒删除。
  - 等待超过 `RETENTION_DAYS` 的文章（或调整时间模拟）：确认 TTL 清理生效。
