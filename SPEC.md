# Search 结果去重方案

## 问题现象
在开启 Rerank 的情况下，搜索结果中可能出现两个完全相同的 Chunk（内容完全一致，分值完全一致）。

## 原因分析
1.  **数据层（根本原因）**：
    `sync_daemon.py` 采用追加写入（Append）模式且未做幂等检查。如果同步状态重置或脚本重复运行，LanceDB 中会产生同一篇文章的重复向量记录。即使 FreshRSS 中文章唯一，LanceDB 中也可能因重复同步而存在多份向量。
2.  **逻辑层（直接原因）**：
    `search.py` 在开启 Rerank 时，策略设定 `max_chunks_per_entry = 2`（每篇文章选 2 个最佳切片）。当数据库存在重复数据时，系统会选出两个一模一样的切片。

## 解决方案
在 `apps/freshrss-search/search.py` 的 `_pick_best_per_entry` 函数中增加去重逻辑，过滤同一篇文章下的重复内容。

### 代码修改
目标文件：`apps/freshrss-search/search.py`

```python
def _pick_best_per_entry(df, *, max_chunks: int = 1):
    if df.empty:
        return df
    max_chunks = max(1, int(max_chunks))

    # 按 entry_id 选取距离最小的前 N 条切片作为候选
    df_sorted = df.sort_values(["entry_id", "_distance"], ascending=[True, True], kind="mergesort")
    
    # [新增] 组内去重：过滤掉同一篇文章下内容完全重复的切片（应对数据库脏数据）
    df_sorted = df_sorted.drop_duplicates(subset=["entry_id", "content"])

    best_df = df_sorted.groupby("entry_id").head(max_chunks).copy()
    best_df.sort_values("_distance", inplace=True)
    return best_df
```

