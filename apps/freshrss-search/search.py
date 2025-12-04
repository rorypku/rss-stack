from __future__ import annotations

import argparse
from pathlib import Path

from openai import OpenAI

from config import get_settings
from db_utils import fetch_existing_entry_ids, open_sqlite
from lancedb_utils import get_or_create_rss_chunks_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search FreshRSS semantic index (LanceDB).")
    parser.add_argument("query", help="search query text")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum number of articles to return (default: 10)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    table = get_or_create_rss_chunks_table()

    query = args.query
    limit = args.limit if args.limit and args.limit > 0 else 10

    # 先将查询文本转换为向量
    client = OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )
    try:
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=[query],
            dimensions=settings.embedding_dim,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error calling embedding API: {exc}")
        return

    if not resp.data:
        print("[search] Empty embedding result for query.")
        return

    query_vector = resp.data[0].embedding

    # 取比最终展示更多的候选结果，再做阈值过滤与去重（纯向量搜索）
    candidate_limit = limit * 5

    df = table.search(query_vector).limit(candidate_limit).to_pandas()
    if df.empty:
        print("No results found.")
        return

    # 阈值过滤
    df = df[df["_distance"] <= settings.search_threshold]
    if df.empty:
        print("No results within threshold.")
        return

    # 按 entry_id 选取距离最小的一条作为该文章的代表切片
    idx = df.groupby("entry_id")["_distance"].idxmin()
    best_df = df.loc[idx].copy()
    best_df.sort_values("_distance", inplace=True)
    best_df = best_df.head(limit)

    # 懒删除：批量回查 SQLite，并删除已被 FreshRSS 删除的文章对应切片
    sqlite_path = Path("/app/data/users/kai/db.sqlite")
    entry_ids = [int(eid) for eid in best_df["entry_id"].unique().tolist()]

    with open_sqlite(sqlite_path) as conn:
        existing_ids = fetch_existing_entry_ids(conn, entry_ids)

    missing_ids = sorted(set(entry_ids) - existing_ids)
    if missing_ids:
        # 从展示结果中剔除已被删除的文章
        best_df = best_df[~best_df["entry_id"].isin(missing_ids)]

        # 从 LanceDB 中批量删除对应切片
        where = f"entry_id in [{', '.join(str(eid) for eid in missing_ids)}]"
        try:
            table.delete(where=where)
        except Exception as exc:  # noqa: BLE001
            print(f"[search] Error during lazy deletion cleanup: {exc}")

    if best_df.empty:
        print("No valid results after lazy deletion cleanup.")
        return

    # 最终结果输出
    for rank, row in enumerate(best_df.itertuples(index=False), start=1):
        title = getattr(row, "title", "")
        link = getattr(row, "link", "")
        category_name = getattr(row, "category_name", None)
        distance = getattr(row, "_distance", None)
        content = getattr(row, "content", "")
        snippet = (content[:200] + "...") if len(content) > 200 else content

        parts = [f"[{rank}] {title}"]
        if category_name:
            parts.append(f"(category: {category_name})")
        if distance is not None:
            parts.append(f"(distance: {distance:.4f})")
        if link:
            parts.append(f"\n  Link: {link}")
        if snippet:
            parts.append(f"\n  Snippet: {snippet}")

        print(" ".join(parts))
        print()


if __name__ == "__main__":
    main()

