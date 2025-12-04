这是一份针对 **SiliconFlow (硅基流动)** 服务的 System Spec。

SiliconFlow 提供了 **OpenAI 兼容 (OpenAI-Compatible)** 的 API 接口，因此我们不需要重写底层复杂的请求逻辑，只需要配置 LanceDB 的 OpenAI 注册表指向 SiliconFlow 的 `base_url` 即可。

请将此 Spec 发送给 AI Agent。

-----

# System Spec: FreshRSS Semantic Search (SiliconFlow Edition)

## 1\. 项目目标

构建一个基于 **LanceDB** 和 **SiliconFlow Embedding** 的语义搜索系统，作为 FreshRSS 的 Docker Sidecar 运行。

  * **特性**：支持长文本自动切块 (Chunking)，增量同步，阈值过滤。
  * **核心变更**：将嵌入服务提供商从 OpenAI 替换为 SiliconFlow (使用 `Qwen/Qwen3-Embedding-8B`)。
  * **核心参数**：
      * Chunk Size: **200**
      * Stride (Overlap): **100**
      * Threshold: **0.5**
      * *参数需通过环境变量配置。*

## 2\. 系统架构

  * **运行环境**: Docker (Python 3.9-slim)
  * **数据源**: FreshRSS SQLite (Read-only volume mount)
  * **向量库**: LanceDB (Persistent volume)
  * **嵌入服务**: SiliconFlow API (OpenAI-compatible protocol)
      * **Base URL**: `https://api.siliconflow.cn/v1`
      * **Model**: 推荐 `Qwen/Qwen3-Embedding-8B`

## 3\. 环境变量配置 (Configuration Specification)

脚本必须优先读取以下环境变量：

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `SILICONFLOW_API_KEY` | (Required) | SiliconFlow 的 API 密钥 (`sk-...`) |
| `SILICONFLOW_BASE_URL`| `https://api.siliconflow.cn/v1` | SiliconFlow API 地址 |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-8B` | 使用的模型名称 |
| `EMBEDDING_DIM` | `4096` | 向量维度 (dimensions)，需与所选模型一致 |
| `CHUNK_SIZE` | `200` | 切片长度 |
| `CHUNK_OVERLAP` | `100` | 切片重叠长度 |
| `SEARCH_THRESHOLD` | `0.5` | 距离阈值 (Cosine Distance, 越小越相似) |
| `CHECK_INTERVAL` | `3600` | 同步间隔 (秒) |
| `RETENTION_DAYS` | `90` | 向量索引的全局保留天数 (兜底上限，单位：天) |
| `LANCEDB_URI` | `/app/lancedb_data/freshrss` | LanceDB 数据库目录 / URI (例如 `dir:/app/lancedb_data/freshrss`) |
| `SYNC_CATEGORIES` | (可选) | 只同步特定 `category.name`，逗号分隔；空值表示同步所有分类 |
| `EMBEDDING_BATCH_SIZE` | `20` | 同步时调用 SiliconFlow Embedding 的批处理大小，积攒到一定数量后再统一请求 |

## 4\. 文件结构与实施细节

在根目录创建 `freshrss-search/` 文件夹。

### Step 1: `requirements.txt`

虽然不连接 OpenAI 官方服务器，但 LanceDB 使用 `openai` 库作为兼容客户端。

```text
lancedb
pandas
openai
```

### Step 2: `sync_daemon.py` (同步与切块)

**关键逻辑变更：初始化 Embedding 函数**

```python
# 伪代码参考
from lancedb.embeddings import get_registry
import os

# 配置 SiliconFlow
registry = get_registry().get("openai").create(
    name=os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
    dimensions=int(os.getenv("EMBEDDING_DIM", "4096")),
    base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
    api_key=os.getenv("SILICONFLOW_API_KEY")
)

# 定义 Schema
class RssChunk(LanceModel):
    # 主键与时间
    entry_id: int          # 对应 entry.id
    published_at: int      # 对应 entry.date (BIGINT)

    # 源与分类信息
    feed_id: int           # 对应 entry.id_feed
    category_id: int | None = None   # 对应 feed.category
    category_name: str | None = None # 对应 category.name

    # 展示信息
    title: str             # 对应 entry.title
    link: str              # 对应 entry.link

    # 切块信息与向量
    chunk_index: int       # 当前文章的第几个切片，从 0 开始
    content: str = registry.SourceField()
    vector: Vector(registry.ndims()) = registry.VectorField()


# LanceDB 表设计
#
# - 使用环境变量 LANCEDB_URI 指向 LanceDB 数据目录，例如：
#     LANCEDB_URI=dir:/app/lancedb_data/freshrss
# - 主业务表：rss_chunks
#     - 存储上述 RssChunk 记录
# - 状态表：sync_state
#     - 字段示例：
#         id: str               # 主键，固定为 "default"
#         last_entry_id: int    # 上次同步到的最大 entry.id
#         last_sync_at: int     # 上次完成同步的时间戳 (可选，用于调试)
```

**其他逻辑要求 (保持不变)：**

1.  **切块算法**：基于 `CHUNK_SIZE` 和 `CHUNK_OVERLAP` 实现文本切分。
2.  **文本清洗**：从 FreshRSS SQLite 读取出的原始内容，需在切块前进行基础清洗，例如去除 HTML 标签、合并多余空格、移除多余或重复换行符等，得到适合语义搜索的纯文本。
3.  **增量同步**：通过 LanceDB 中的 `sync_state` 表持久化 `last_entry_id`，每轮只读取 `entry.id > last_entry_id` 的新文章：读取 SQLite -\> 文本清洗 -\> 切块 -\> `rss_chunks.add()` -\> 更新 `sync_state`。
4.  **守护进程**：`while True` 循环 + `time.sleep(CHECK_INTERVAL)`。
5.  **保留字段**：`RssChunk` 中需至少持久化 FreshRSS 文章的主键 ID（`entry_id`）与发布时间（`published_at`），用于后续懒删除与 TTL 清理；为调试与过滤方便，建议同时持久化 `feed_id` / `category_id` / `category_name` / `title` / `link` / `chunk_index` 等字段。
6.  **分类过滤 (可选)**：若设置 `SYNC_CATEGORIES`（例如 `macro,news`），同步时需根据 `feed.category` 与 `category.name` 的映射，只同步这些分类下的文章；未设置则同步所有分类。`RssChunk` 中的 `category_id` / `category_name` 需来自 FreshRSS 的 `category` / `feed` 表，以便后续搜索时按分类过滤或调试。
7.  **Embedding 批处理 (Batching)**：在 `sync_daemon.py` 中，同一轮同步产生的切片不应逐条调用 SiliconFlow Embedding API，而是积攒到一定数量（由 `EMBEDDING_BATCH_SIZE` 控制，例如 20 条）后批量调用一次；若使用 LanceDB 的自动向量生成能力，则需在写入 `rss_chunks` 时按批次 `add()`，以便底层客户端以批量方式调用远端 API，减少网络开销并降低触发速率限制的风险。

### Step 3: `search.py` (CLI 搜索)

**逻辑要求：**

1.  **复用配置**：搜索脚本也必须像 `sync_daemon.py` 一样初始化 registry 对象（或者依赖 LanceDB 的表元数据自动加载，但显式定义更稳健）。
2.  **执行搜索**：`table.search(query)`。由于 SiliconFlow 兼容 OpenAI 协议，LanceDB 会自动调用该 API 将 `query` 转换为向量。
3.  **阈值过滤**：过滤掉 `_distance > SEARCH_THRESHOLD` 的结果。
4.  **去重展示**：同一篇文章只展示最相关的一个切片。
5.  **懒删除 (Lazy Deletion)**：在展示搜索结果前，不对每条结果单独回查 SQLite，而是先收集所有候选结果的 `entry_id` 列表（例如最多 20 个），通过一条形如 `SELECT id FROM entry WHERE id IN ( ... )` 的 SQL 一次性查询存在的 ID 集合；将 LanceDB 返回的 `entry_id` 集合与 SQLite 查询结果做差，缺失的 ID 视为已被 FreshRSS 删除：对这些 ID，从当前结果集中跳过对应文章，并调用 LanceDB 的 `table.delete(where="entry_id IN (...)")` 批量删除所有切片，以保证后续搜索不会再命中已删除文章，同时将 SQLite IO 次数降到最低。

### Step 4: `docker-compose.yml` (集成)

修改环境变量部分，适配 SiliconFlow。

```yaml
  rss-sync:
    image: python:3.9-slim
    container_name: rss_sync_worker
    restart: unless-stopped
    environment:
      # --- SiliconFlow Config ---
      - SILICONFLOW_API_KEY=${SILICONFLOW_API_KEY} # 在 .env 中设置
      - SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
      - EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B
      
      # --- Tuning Params ---
      - CHUNK_SIZE=200
      - CHUNK_OVERLAP=100
      - SEARCH_THRESHOLD=0.5
      - CHECK_INTERVAL=3600
      - RETENTION_DAYS=90
      - LANCEDB_URI=dir:/app/lancedb_data/freshrss
      # 只同步特定分类（可选），例如：
      # - SYNC_CATEGORIES=macro,news
      - PYTHONUNBUFFERED=1
    volumes:
      - ./data:/app/data:ro
      - ./lancedb_data:/app/lancedb_data
      - ./freshrss-search:/app/code
    working_dir: /app/code
    command: >
      sh -c "pip install -r requirements.txt && python sync_daemon.py"
```

-----

## 5\. 执行验证指南

请 Agent 执行以下检查：

1.  **兼容性检查**：确认 Python 代码中 `get_registry().get("openai")` 正确传入了 `base_url` 和 `api_key` 参数。
2.  **维度检查**：首次运行时，关注日志输出。默认维度通过 `EMBEDDING_DIM` 环境变量配置为 `4096`，确认 LanceDB 建表时自动检测到的 `ndims` 与该值一致。
3.  **网络连通性**：确认容器内可以访问 `api.siliconflow.cn` (如果宿主机网络有特殊限制)。
