from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import requests

from openai import OpenAI

from config import get_settings
from db_utils import fetch_existing_entry_ids, open_sqlite
from lancedb_utils import get_or_create_rss_chunks_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search FreshRSS semantic index (LanceDB).")
    parser.add_argument("query", nargs="+", help="one or more search queries")
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
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="max parallel workers for search/rerank (default: auto)",
    )
    return parser.parse_args()


def _clamp_positive(value: int, default: int) -> int:
    return value if value and value > 0 else default


def _embed_texts(
    client: OpenAI,
    texts: Sequence[str],
    *,
    model: str,
    dimensions: int,
    batch_size: int,
) -> list[list[float]] | None:
    if not texts:
        return []

    batch_size = max(1, int(batch_size))
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        try:
            resp = client.embeddings.create(
                model=model,
                input=batch,
                dimensions=dimensions,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[search] Error calling embedding API: {exc}")
            return None

        if not resp.data:
            print("[search] Empty embedding result for query batch.")
            return None

        batch_vectors: list[list[float] | None] = [None] * len(batch)
        has_index = True
        for item in resp.data:
            idx = getattr(item, "index", None)
            if idx is None:
                has_index = False
                break
            try:
                idx = int(idx)
            except Exception:  # noqa: BLE001
                has_index = False
                break
            if 0 <= idx < len(batch_vectors):
                batch_vectors[idx] = item.embedding

        if not has_index or any(vec is None for vec in batch_vectors):
            fallback_vectors = [item.embedding for item in resp.data]
            if len(fallback_vectors) != len(batch):
                print(
                    "[search] Embedding batch size mismatch: "
                    f"texts={len(batch)}, vectors={len(fallback_vectors)}",
                )
                return None
            vectors.extend(fallback_vectors)
            continue

        vectors.extend([vec for vec in batch_vectors if vec is not None])

    return vectors


def get_query_embeddings(texts: Sequence[str]) -> list[list[float]] | None:
    settings = get_settings()
    client = OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )

    return _embed_texts(
        client,
        texts,
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        batch_size=settings.embedding_batch_size,
    )


def get_query_embedding(text: str) -> list[float] | None:
    vectors = get_query_embeddings([text])
    if vectors is None or not vectors:
        return None
    return vectors[0]


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


def _fetch_existing_entry_ids_batched(
    conn,
    entry_ids: Sequence[int],
    *,
    batch_size: int = 900,
) -> set[int]:
    unique_ids = [int(eid) for eid in entry_ids if eid is not None]
    if not unique_ids:
        return set()

    batch_size = max(1, int(batch_size))
    existing_ids: set[int] = set()
    for start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[start : start + batch_size]
        existing_ids |= fetch_existing_entry_ids(conn, list(batch))
    return existing_ids


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
            existing_ids = _fetch_existing_entry_ids_batched(conn, entry_ids)
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


def _normalize_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:  # noqa: BLE001
        return None

    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return None


def _format_epoch_seconds_to_date(value: object) -> str | None:
    ts = _normalize_int(value)
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _fetch_entry_id_to_published_date(*, sqlite_path: Path, entry_ids: Sequence[int]) -> dict[int, str]:
    if not entry_ids:
        return {}
    if not sqlite_path.exists():
        return {}
    unique_ids = sorted({int(eid) for eid in entry_ids if eid is not None})
    if not unique_ids:
        return {}

    placeholders = ",".join("?" for _ in unique_ids)
    query = f"SELECT id, date FROM entry WHERE id IN ({placeholders})"
    try:
        with open_sqlite(sqlite_path) as conn:
            cur = conn.execute(query, unique_ids)
            mapping: dict[int, str] = {}
            for row in cur.fetchall():
                entry_id, raw_date = row
                published_date = _format_epoch_seconds_to_date(raw_date)
                if published_date is not None:
                    mapping[int(entry_id)] = published_date
            return mapping
    except Exception as exc:  # noqa: BLE001
        print(f"[search] Error reading entry dates from FreshRSS sqlite at {sqlite_path}: {exc}")
        return {}


def _format_results_jsonl(
    rows: Iterable[Any],
    *,
    rerank_enabled: bool,
    feed_id_to_name: dict[int, str],
    entry_id_to_published_date: dict[int, str],
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

        published_date: str | None = None
        entry_id = getattr(row, "entry_id", None)
        if entry_id is not None:
            try:
                published_date = entry_id_to_published_date.get(int(entry_id))
            except Exception:  # noqa: BLE001
                published_date = None

        if published_date is None:
            published_date = _format_epoch_seconds_to_date(getattr(row, "published_at", None))

        item: dict[str, object] = {
            "feed.name": feed_name,
            "title": title,
            "chunk": chunk,
            "published_date": published_date,
        }
        if rerank_enabled:
            score = getattr(row, "rerank_score", None)
            if score is not None:
                item["rerank_score"] = float(score)
        results.append(item)
    return results


def _normalize_queries(raw_queries: Sequence[str]) -> list[str]:
    queries: list[str] = []
    for raw_query in raw_queries:
        query = (raw_query or "").strip()
        if query:
            queries.append(query)
    return queries


def _resolve_max_workers(value: int | None, *, task_count: int, default_cap: int = 8) -> int:
    if task_count <= 1:
        return 1
    if value is not None and int(value) > 0:
        return min(int(value), task_count)
    return min(default_cap, task_count)


def main() -> None:
    args = parse_args()
    settings = get_settings()

    queries = _normalize_queries(args.query)
    if not queries:
        print("No query provided.")
        return

    limit = _clamp_positive(args.limit, default=10)

    rerank_enabled = settings.rerank_enabled
    if args.rerank:
        rerank_enabled = True
    if args.no_rerank:
        rerank_enabled = False

    # Resolve optional feed filter once (shared across all queries).
    feed_ids_filter: list[int] = []
    if args.feed:
        feed_ids_filter = _resolve_feed_ids(sqlite_path=settings.freshrss_sqlite_path, feed=args.feed)
        if not feed_ids_filter:
            print(f"No such feed (id or name): {args.feed}")
            return

    # Single-query path: keep existing behavior and messages.
    if len(queries) == 1:
        query = queries[0]
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
        if feed_ids_filter:
            best_df = _filter_by_feed_ids(best_df, feed_ids=feed_ids_filter)
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

        entry_ids: list[int] = []
        if "entry_id" in best_df.columns:
            try:
                entry_ids = [int(v) for v in best_df["entry_id"].dropna().unique().tolist()]
            except Exception:  # noqa: BLE001
                entry_ids = []
        entry_id_to_published_date = _fetch_entry_id_to_published_date(
            sqlite_path=settings.freshrss_sqlite_path,
            entry_ids=entry_ids,
        )

        # 最终结果输出为 JSONL（一行一个 JSON 对象）
        results = _format_results_jsonl(
            best_df.itertuples(index=False),
            rerank_enabled=rerank_enabled,
            feed_id_to_name=feed_id_to_name,
            entry_id_to_published_date=entry_id_to_published_date,
        )
        for item in results:
            print(json.dumps(item, ensure_ascii=False))
        return

    # Multi-query path: staged map-reduce to maximize parallelism and avoid write conflicts.
    query_vectors = get_query_embeddings(queries)
    if query_vectors is None:
        return
    if len(query_vectors) != len(queries):
        print(
            "[search] Embedding size mismatch for queries: "
            f"queries={len(queries)}, vectors={len(query_vectors)}",
        )
        return

    max_workers = _resolve_max_workers(args.workers, task_count=len(queries))

    def _search_one(query_vector: Sequence[float]) -> pd.DataFrame:
        _table, df = search_vector_db(query_vector, limit)
        return df

    raw_dfs: list[pd.DataFrame] = [pd.DataFrame() for _ in queries]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_search_one, vec): idx for idx, vec in enumerate(query_vectors)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                raw_dfs[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[search] Error searching vector DB for query #{idx + 1}: {exc}")
                raw_dfs[idx] = pd.DataFrame()

    # Stage 2b: in-memory filtering only (no lazy deletion).
    max_chunks_per_entry = 2 if rerank_enabled else 1
    candidate_dfs: list[pd.DataFrame] = []
    for df in raw_dfs:
        if df.empty:
            candidate_dfs.append(df)
            continue

        filtered_df = _apply_distance_threshold(df, threshold=settings.search_threshold)
        if filtered_df.empty:
            candidate_dfs.append(filtered_df)
            continue

        best_df = _pick_best_per_entry(filtered_df, max_chunks=max_chunks_per_entry)
        if args.category:
            best_df = _filter_by_category(best_df, category=args.category)
        if feed_ids_filter:
            best_df = _filter_by_feed_ids(best_df, feed_ids=feed_ids_filter)
        candidate_dfs.append(best_df)

    # Stage 3: consolidated lazy deletion (single sqlite lookup + single LanceDB delete).
    non_empty_candidate_dfs = [df for df in candidate_dfs if not df.empty]
    if non_empty_candidate_dfs:
        combined_df = pd.concat(non_empty_candidate_dfs, ignore_index=True)
        table = get_or_create_rss_chunks_table()
        _filtered_combined_df, missing_ids = filter_deleted_entries(
            combined_df,
            table=table,
            sqlite_path=settings.freshrss_sqlite_path,
        )
        if missing_ids:
            candidate_dfs = [
                df[~df["entry_id"].isin(missing_ids)] if (not df.empty and "entry_id" in df.columns) else df
                for df in candidate_dfs
            ]

    # Stage 4: parallel rerank per query (after lazy deletion filtering).
    rerank_model = args.rerank_model or settings.rerank_model
    rerank_candidates = args.rerank_candidates or settings.rerank_candidates
    if rerank_enabled:
        rerank_candidates = max(rerank_candidates, limit * max_chunks_per_entry)

    final_dfs: list[pd.DataFrame] = list(candidate_dfs)
    if rerank_enabled:
        rerank_task_indices = [idx for idx, df in enumerate(candidate_dfs) if not df.empty]
        rerank_workers = _resolve_max_workers(args.workers, task_count=len(rerank_task_indices))
        if rerank_task_indices:
            with ThreadPoolExecutor(max_workers=rerank_workers) as executor:
                futures = {
                    executor.submit(
                        rerank_results,
                        candidate_dfs[idx],
                        query=queries[idx],
                        limit=limit,
                        rerank_enabled=True,
                        rerank_model=rerank_model,
                        rerank_candidates=rerank_candidates,
                    ): idx
                    for idx in rerank_task_indices
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        final_dfs[idx] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[search] Error reranking for query #{idx + 1}: {exc}")
                        final_dfs[idx] = candidate_dfs[idx]

    for idx, df in enumerate(final_dfs):
        if not df.empty:
            final_dfs[idx] = df.head(limit)

    all_feed_ids: set[int] = set()
    all_entry_ids: set[int] = set()
    for df in final_dfs:
        if df.empty:
            continue
        if "feed_id" in df.columns:
            try:
                all_feed_ids |= {int(v) for v in df["feed_id"].dropna().unique().tolist()}
            except Exception:  # noqa: BLE001
                pass
        if "entry_id" in df.columns:
            try:
                all_entry_ids |= {int(v) for v in df["entry_id"].dropna().unique().tolist()}
            except Exception:  # noqa: BLE001
                pass

    feed_id_to_name = _fetch_feed_id_to_name(
        sqlite_path=settings.freshrss_sqlite_path,
        feed_ids=sorted(all_feed_ids),
    )
    entry_id_to_published_date = _fetch_entry_id_to_published_date(
        sqlite_path=settings.freshrss_sqlite_path,
        entry_ids=sorted(all_entry_ids),
    )

    any_output = False
    for idx, df in enumerate(final_dfs):
        if df.empty:
            continue
        results = _format_results_jsonl(
            df.itertuples(index=False),
            rerank_enabled=rerank_enabled,
            feed_id_to_name=feed_id_to_name,
            entry_id_to_published_date=entry_id_to_published_date,
        )
        for item in results:
            item["query"] = queries[idx]
            print(json.dumps(item, ensure_ascii=False))
            any_output = True

    if not any_output:
        print("No results found.")


if __name__ == "__main__":
    main()
