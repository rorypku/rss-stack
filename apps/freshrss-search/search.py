from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

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
        help="maximum number of chunks to return (default: 10)",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="optional FreshRSS category.name to filter results by",
    )
    parser.add_argument(
        "--feed",
        type=str,
        default=None,
        help="optional FreshRSS feed.id (or feed.name) to filter results by",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--rerank",
        action="store_true",
        help="enable rerank (overrides env RERANK_ENABLED)",
    )
    group.add_argument(
        "--no-rerank",
        action="store_true",
        help="disable rerank (overrides env RERANK_ENABLED)",
    )
    parser.add_argument(
        "--rerank-model",
        type=str,
        default=None,
        help="override env RERANK_MODEL",
    )
    parser.add_argument(
        "--rerank-candidates",
        type=int,
        default=None,
        help="override env RERANK_CANDIDATES",
    )
    return parser.parse_args()


def _clamp_positive(value: int, default: int) -> int:
    return value if value and value > 0 else default


def get_query_embedding(text: str) -> list[float] | None:
    settings = get_settings()
    client = OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )
    try:
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=[text],
            dimensions=settings.embedding_dim,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error calling embedding API: {exc}")
        return None

    if not resp.data:
        print("[search] Empty embedding result for query.")
        return None

    return resp.data[0].embedding


def search_vector_db(query_vector: Sequence[float], limit: int):
    settings = get_settings()
    table = get_or_create_rss_chunks_table()

    # 多召回（纯向量搜索）：尽量避免过早过滤导致 rerank 没候选
    candidate_limit = min(
        limit * max(1, settings.search_candidate_multiplier),
        max(limit, settings.search_candidate_cap),
    )

    df = table.search(query_vector).limit(candidate_limit).to_pandas()
    return table, df


def _apply_distance_threshold(df, *, threshold: float):
    if df.empty:
        return df
    return df[df["_distance"] <= threshold]


def _pick_best_per_entry(df, *, max_chunks: int = 1):
    if df.empty:
        return df
    max_chunks = max(1, int(max_chunks))

    # 按 entry_id 选取距离最小的前 N 条切片作为候选（N=1 时用于去重展示）
    df_sorted = df.sort_values(["entry_id", "_distance"], ascending=[True, True], kind="mergesort")
    # 组内去重：过滤掉同一篇文章下内容完全重复的切片（应对数据库脏数据）
    df_sorted = df_sorted.drop_duplicates(subset=["entry_id", "content"])
    best_df = df_sorted.groupby("entry_id").head(max_chunks).copy()
    best_df.sort_values("_distance", inplace=True)
    return best_df


def _filter_by_category(results_df, *, category: str):
    if results_df.empty:
        return results_df
    return results_df[results_df["category_name"] == category]


def _resolve_feed_ids(*, sqlite_path: Path, feed: str) -> list[int]:
    feed = (feed or "").strip()
    if not feed:
        return []

    try:
        return [int(feed)]
    except ValueError:
        pass

    if not sqlite_path.exists():
        return []

    query = "SELECT id FROM feed WHERE name = ?"
    try:
        with open_sqlite(sqlite_path) as conn:
            cur = conn.execute(query, [feed])
            return [int(row[0]) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error reading feed ids from FreshRSS sqlite at {sqlite_path}: {exc}")
        return []


def _filter_by_feed_ids(results_df, *, feed_ids: Sequence[int]):
    if results_df.empty:
        return results_df
    if "feed_id" not in results_df.columns:
        return results_df.iloc[0:0]
    normalized_feed_ids = [int(fid) for fid in feed_ids if fid is not None]
    if not normalized_feed_ids:
        return results_df.iloc[0:0]
    return results_df[results_df["feed_id"].isin(normalized_feed_ids)]


def filter_deleted_entries(results_df, *, table, sqlite_path: Path) -> tuple[Any, list[int]]:
    """
    Lazy-delete entries that no longer exist in FreshRSS sqlite.
    Returns (filtered_df, missing_ids).
    """
    if results_df.empty:
        return results_df, []

    if not sqlite_path.exists():
        print(
            f"[search] FreshRSS sqlite not found at {sqlite_path}; "
            "skip lazy deletion cleanup (set FRESHRSS_SQLITE_PATH to configure).",
        )
        return results_df, []

    entry_ids = [int(eid) for eid in results_df["entry_id"].unique().tolist()]
    if not entry_ids:
        return results_df, []

    try:
        with open_sqlite(sqlite_path) as conn:
            existing_ids = fetch_existing_entry_ids(conn, entry_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error opening FreshRSS sqlite at {sqlite_path}: {exc}")
        return results_df, []

    missing_ids = sorted(set(entry_ids) - existing_ids)
    if not missing_ids:
        return results_df, []

    filtered_df = results_df[~results_df["entry_id"].isin(missing_ids)]

    # 从 LanceDB 中批量删除对应切片
    where = f"entry_id IN ({', '.join(str(eid) for eid in missing_ids)})"
    try:
        table.delete(where=where)
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error during lazy deletion cleanup: {exc}")

    return filtered_df, missing_ids


def _siliconflow_rerank(
    *,
    base_url: str,
    api_key: str,
    model: str,
    query: str,
    documents: list[str],
    timeout_seconds: int,
) -> list[float] | None:
    """
    Call SiliconFlow rerank API and return per-document scores.
    Expected response: {"results": [{"index": 0, "relevance_score": 0.98}, ...]}
    """
    if not documents:
        return []
    url = base_url.rstrip("/") + "/rerank"
    payload = {
        "model": model,
        "query": query,
        "documents": documents,
        "return_documents": False,
        "top_n": len(documents),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        results = data.get("results", [])
        scores = [0.0] * len(documents)
        for item in results:
            idx = int(item.get("index"))
            score = float(item.get("relevance_score"))
            if 0 <= idx < len(scores):
                scores[idx] = score
        return scores
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Rerank API error: {exc}")
        return None


def _iter_rerank_documents(rows: Iterable[Any], *, max_chars: int) -> list[str]:
    documents: list[str] = []
    max_chars = max(1, max_chars)
    for row in rows:
        title = getattr(row, "title", "") or ""
        content = getattr(row, "content", "") or ""
        doc = (title + "\n\n" + content).strip()
        documents.append(doc[:max_chars])
    return documents


def rerank_results(
    results_df,
    *,
    query: str,
    limit: int,
    rerank_enabled: bool,
    rerank_model: str,
    rerank_candidates: int,
):
    settings = get_settings()
    if not rerank_enabled or results_df.empty:
        return results_df

    rerank_candidates = _clamp_positive(rerank_candidates, default=200)
    rerank_candidates = max(limit, rerank_candidates)

    rerank_df = results_df.head(rerank_candidates).copy()
    documents = _iter_rerank_documents(
        rerank_df.itertuples(index=False),
        max_chars=settings.rerank_max_doc_chars,
    )

    scores = _siliconflow_rerank(
        base_url=settings.siliconflow_base_url,
        api_key=settings.siliconflow_api_key,
        model=rerank_model,
        query=query,
        documents=documents,
        timeout_seconds=max(1, settings.rerank_timeout_seconds),
    )
    if scores is None:
        return results_df

    rerank_df["rerank_score"] = scores
    rerank_df.sort_values("rerank_score", ascending=False, inplace=True)
    return rerank_df


def _fetch_feed_id_to_name(*, sqlite_path: Path, feed_ids: Sequence[int]) -> dict[int, str]:
    if not feed_ids:
        return {}
    if not sqlite_path.exists():
        return {}
    unique_ids = sorted({int(fid) for fid in feed_ids if fid is not None})
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    query = f"SELECT id, name FROM feed WHERE id IN ({placeholders})"
    try:
        with open_sqlite(sqlite_path) as conn:
            cur = conn.execute(query, unique_ids)
            mapping: dict[int, str] = {}
            for row in cur.fetchall():
                fid, name = row
                if name:
                    mapping[int(fid)] = str(name)
            return mapping
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error reading feed names from FreshRSS sqlite at {sqlite_path}: {exc}")
        return {}


def _format_results_jsonl(
    rows: Iterable[Any],
    *,
    rerank_enabled: bool,
    feed_id_to_name: dict[int, str],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for row in rows:
        title = getattr(row, "title", "")
        feed_id = getattr(row, "feed_id", None)
        chunk = getattr(row, "content", "")

        feed_name: str | None = None
        if feed_id is not None:
            try:
                feed_name = feed_id_to_name.get(int(feed_id))
            except Exception:  # noqa: BLE001
                feed_name = None

        item: dict[str, object] = {
            "feed.name": feed_name,
            "title": title,
            "chunk": chunk,
        }
        if rerank_enabled:
            score = getattr(row, "rerank_score", None)
            if score is not None:
                item["rerank_score"] = float(score)
        results.append(item)
    return results


def main() -> None:
    args = parse_args()
    settings = get_settings()

    query = args.query
    limit = _clamp_positive(args.limit, default=10)

    rerank_enabled = settings.rerank_enabled
    if args.rerank:
        rerank_enabled = True
    if args.no_rerank:
        rerank_enabled = False

    query_vector = get_query_embedding(query)
    if query_vector is None:
        return

    table, df = search_vector_db(query_vector, limit)
    if df.empty:
        print("No results found.")
        return

    df = _apply_distance_threshold(df, threshold=settings.search_threshold)
    if df.empty:
        print("No results within threshold.")
        return

    # Rerank 前每篇文章最多选取 2 个候选切片，避免只靠单个 chunk 表达不足。
    max_chunks_per_entry = 2 if rerank_enabled else 1
    best_df = _pick_best_per_entry(df, max_chunks=max_chunks_per_entry)
    if args.category:
        best_df = _filter_by_category(best_df, category=args.category)
        if best_df.empty:
            print(f"No results found for category: {args.category}")
            return
    if args.feed:
        feed_ids = _resolve_feed_ids(sqlite_path=settings.freshrss_sqlite_path, feed=args.feed)
        if not feed_ids:
            print(f"No such feed (id or name): {args.feed}")
            return
        best_df = _filter_by_feed_ids(best_df, feed_ids=feed_ids)
        if best_df.empty:
            print(f"No results found for feed: {args.feed}")
            return

    # 懒删除：批量回查 SQLite，并删除已被 FreshRSS 删除的文章对应切片
    best_df, _missing_ids = filter_deleted_entries(
        best_df,
        table=table,
        sqlite_path=settings.freshrss_sqlite_path,
    )

    if best_df.empty:
        print("No valid results after lazy deletion cleanup.")
        return

    rerank_model = args.rerank_model or settings.rerank_model
    rerank_candidates = args.rerank_candidates or settings.rerank_candidates
    if rerank_enabled:
        rerank_candidates = max(rerank_candidates, limit * max_chunks_per_entry)
    best_df = rerank_results(
        best_df,
        query=query,
        limit=limit,
        rerank_enabled=rerank_enabled,
        rerank_model=rerank_model,
        rerank_candidates=rerank_candidates,
    )
    best_df = best_df.head(limit)

    feed_ids: list[int] = []
    if "feed_id" in best_df.columns:
        try:
            feed_ids = [int(v) for v in best_df["feed_id"].dropna().unique().tolist()]
        except Exception:  # noqa: BLE001
            feed_ids = []
    feed_id_to_name = _fetch_feed_id_to_name(
        sqlite_path=settings.freshrss_sqlite_path,
        feed_ids=feed_ids,
    )

    # 最终结果输出为 JSONL（一行一个 JSON 对象）
    results = _format_results_jsonl(
        best_df.itertuples(index=False),
        rerank_enabled=rerank_enabled,
        feed_id_to_name=feed_id_to_name,
    )
    for item in results:
        print(json.dumps(item, ensure_ascii=False))


if __name__ == "__main__":
    main()
