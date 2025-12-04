# FreshRSS Semantic Search – DB Schema

本文件描述 FreshRSS 本地 SQLite 与 LanceDB 之间的字段映射及表设计，用于实现语义搜索与增量同步。

## 1. FreshRSS SQLite 结构（节选）

仅列出本项目需要用到的核心表和字段。

### 1.1 `entry` 表（文章）

- `id` (`BIGINT`, PK): 文章主键。
- `guid` (`VARCHAR`): 全局唯一标识。
- `title` (`VARCHAR`): 标题。
- `author` (`VARCHAR`): 作者。
- `content` (`TEXT`): HTML 格式正文内容。
- `link` (`VARCHAR`): 原文链接。
- `date` (`BIGINT`): 发布时间（时间戳）。
- `lastSeen` (`BIGINT`): FreshRSS 最后一次看到该条目的时间。
- `is_read` (`BOOLEAN`): 是否已读。
- `is_favorite` (`BOOLEAN`): 是否收藏。
- `id_feed` (`INTEGER`): 关联的订阅源 ID，对应 `feed.id`。
- `tags` (`VARCHAR`): 标签（字符串形式）。
- `attributes` (`TEXT`): 附加属性（JSON）。

### 1.2 `feed` 表（订阅源）

- `id` (`INTEGER`, PK): 订阅源主键。
- `url` (`VARCHAR`): RSS / Atom 源地址。
- `category` (`INTEGER`): 关联分类 ID，对应 `category.id`。
- `name` (`VARCHAR`): 源名称。
- `website` (`VARCHAR`): 网站地址。
- `description` (`TEXT`): 描述。
- `lastUpdate` (`BIGINT`): 最后更新时间。
- `ttl` (`INT`): FreshRSS 自身使用的 TTL 配置。

### 1.3 `category` 表（分类）

- `id` (`INTEGER`, PK): 分类主键。
- `name` (`VARCHAR`): 分类名称（如 `macro`、`news`）。
- `kind` (`SMALLINT`): 类型。

### 1.4 典型查询关系

- `entry.id_feed = feed.id`
- `feed.category = category.id`

示例查询：按分类名称过滤文章：

```sql
SELECT
  e.*,
  f.category AS category_id,
  c.name     AS category_name
FROM entry e
JOIN feed f ON e.id_feed = f.id
LEFT JOIN category c ON f.category = c.id
WHERE e.id > :last_entry_id
  AND (:category_ids_is_empty OR f.category IN (:category_ids));
```

其中 `:category_ids` 由环境变量 `SYNC_CATEGORIES` 中的 `category.name` 映射得到。

## 2. LanceDB 结构

LanceDB 数据库存放在挂载卷 `/app/lancedb_data` 中，通过环境变量 `LANCEDB_URI` 指定，默认：

- `LANCEDB_URI=dir:/app/lancedb_data/freshrss`

本项目使用两张表：

- `rss_chunks`：存储语义切片（RssChunk）。
- `sync_state`：存储同步状态（增量同步游标）。

### 2.1 `rss_chunks` 表（RssChunk 模型）

RssChunk 使用 LanceModel 定义，核心字段如下（类型为 Python 语义）：

- `entry_id: int`  
  - 来源：`entry.id`  
  - 用途：懒删除时回查 SQLite，并统一删除该文章的所有切片。

- `published_at: int`  
  - 来源：`entry.date`  
  - 用途：基于 `RETENTION_DAYS` 实现全局 TTL 清理。

- `feed_id: int`  
  - 来源：`entry.id_feed`  
  - 用途：调试、按订阅源过滤。

- `category_id: int | None`  
  - 来源：`feed.category`  
  - 用途：调试、按分类过滤。

- `category_name: str | None`  
  - 来源：`category.name`  
  - 用途：在搜索结果中展示所属分类，或做 CLI 级过滤（例如 `--category macro`）。

- `title: str`  
  - 来源：`entry.title`  
  - 用途：搜索结果展示标题，无需再回查 SQLite。

- `link: str`  
  - 来源：`entry.link`  
  - 用途：搜索结果中直接跳转到原文。

- `chunk_index: int`  
  - 来源：本地切块逻辑（从 0 开始）。  
  - 用途：表示文章内的第几个切片，便于排查问题或扩展上下文展示。

- `content: str`  
  - 来源：由 `entry.content` 清洗得到的纯文本。  
  - 标记：`registry.SourceField()`，用于 LanceDB 自动生成向量。

- `vector: Vector(registry.ndims())`  
  - 来源：上游 Embedding 模型（SiliconFlow / Qwen3）。  
  - 标记：`registry.VectorField()`，存储 embedding 向量。

### 2.2 `sync_state` 表（同步状态）

用于记录增量同步的进度，避免重复读取 SQLite。

字段设计示例：

- `id: str`  
  - 主键，固定为 `"default"`（后续如有多用户/多实例，可扩展为不同 scope）。

- `last_entry_id: int`  
  - 含义：上次同步完成时处理到的最大 `entry.id`。  
  - 用途：下一轮同步只拉取 `entry.id > last_entry_id` 的新文章。

- `last_sync_at: int`  
  - 含义：上次完成同步的时间戳（Unix 时间）。  
  - 用途：调试、监控用，可选字段。

同步流程（概念）：

1. 从 `sync_state` 读取当前 `last_entry_id`（如果不存在，视为 0）。
2. 按 `entry.id > last_entry_id` 从 SQLite 拉取新文章（可选叠加分类过滤）。
3. 清洗、切块后写入 `rss_chunks`。
4. 用新的最大 `entry.id` 更新 `sync_state`。

## 3. 分类过滤与 SYNC_CATEGORIES

通过环境变量 `SYNC_CATEGORIES` 控制同步范围：

- `SYNC_CATEGORIES` 为空或未设置：同步所有分类。
- `SYNC_CATEGORIES=macro,news`：仅同步 `category.name` 为 `macro` 或 `news` 的文章。

实现要点：

1. 启动时从 SQLite 读取 `category` 表，构建 `name -> id` 映射。  
2. 将 `SYNC_CATEGORIES` 拆分为若干 `category.name`，映射为一组 `category_id`。  
3. 在拉取 `entry` 时，通过 join `feed` / `category` 并使用 `feed.category IN (:category_ids)` 做过滤。  
4. 在写入 RssChunk 时，将对应的 `category_id` / `category_name` 一并持久化。

