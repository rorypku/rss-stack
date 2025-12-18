import time
from typing import Sequence

from openai import OpenAI

from config import get_settings
from db_utils import EntryRow, clean_html_content, chunk_text, iter_new_entries, open_sqlite
from lancedb_utils import get_or_create_rss_chunks_table, load_sync_state, save_sync_state


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
            response = client.embeddings.create(
                model=model,
                input=batch,
                dimensions=dimensions,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[sync_daemon] Error during embedding batch call: {exc}")
            return None

        batch_vectors = [item.embedding for item in response.data]
        if len(batch_vectors) != len(batch):
            print(
                "[sync_daemon] Embedding batch size mismatch: "
                f"texts={len(batch)}, vectors={len(batch_vectors)}",
            )
            return None
        vectors.extend(batch_vectors)

    return vectors


def _build_entry_rows(entry: EntryRow, chunks: Sequence[str]) -> list[dict]:
    rows: list[dict] = []
    for idx, chunk in enumerate(chunks):
        rows.append(
            {
                "entry_id": entry.entry_id,
                "published_at": entry.date,
                "feed_id": entry.feed_id,
                "category_id": entry.category_id,
                "category_name": entry.category_name,
                "title": entry.title,
                "link": entry.link,
                "chunk_index": idx,
                "content": chunk,
            }
        )
    return rows


def _flush_entry_batch(chunks_table, *, entry_ids: Sequence[int], records: Sequence[dict]) -> None:
    if not entry_ids:
        return
    where = f"entry_id IN ({', '.join(str(int(eid)) for eid in entry_ids)})"
    try:
        chunks_table.delete(where=where)
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_daemon] Error deleting existing chunks for batch: {exc}")

    if not records:
        return
    try:
        chunks_table.add(list(records))
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_daemon] Error adding batch to LanceDB: {exc}")


def main() -> None:
    settings = get_settings()

    client = OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
    )

    sqlite_path = settings.freshrss_sqlite_path

    chunks_table = get_or_create_rss_chunks_table()
    state = load_sync_state()

    print("Sync daemon initialized")
    print(f"LanceDB table: {chunks_table.name}")
    print(f"Last entry id: {state.last_entry_id}")
    print(f"Check interval: {settings.check_interval} seconds")
    print(f"FreshRSS sqlite path: {sqlite_path}")

    while True:
        try:
            if not sqlite_path.exists():
                print(
                    f"[sync_daemon] FreshRSS sqlite not found at {sqlite_path}; "
                    "set FRESHRSS_SQLITE_PATH to configure.",
                )
                time.sleep(settings.check_interval)
                continue

            with open_sqlite(sqlite_path) as conn:
                max_entry_id = state.last_entry_id
                pending_entry_ids: list[int] = []
                pending_records: list[dict] = []
                entry_batch_size = max(1, int(settings.sync_entry_batch_size))

                for entry in iter_new_entries(conn, last_entry_id=state.last_entry_id):
                    max_entry_id = max(max_entry_id, entry.entry_id)

                    plain_text = clean_html_content(entry.content)
                    if not plain_text:
                        continue

                    chunks = chunk_text(
                        plain_text,
                        chunk_size=settings.chunk_size,
                        chunk_overlap=settings.chunk_overlap,
                    )
                    if not chunks:
                        continue

                    vectors = _embed_texts(
                        client,
                        chunks,
                        model=settings.embedding_model,
                        dimensions=settings.embedding_dim,
                        batch_size=settings.embedding_batch_size,
                    )
                    if vectors is None:
                        continue

                    entry_rows = _build_entry_rows(entry, chunks)
                    if len(entry_rows) != len(vectors):
                        print(
                            "[sync_daemon] Embedding size mismatch for entry: "
                            f"entry_id={entry.entry_id}, chunks={len(entry_rows)}, vectors={len(vectors)}",
                        )
                        continue

                    for row, vec in zip(entry_rows, vectors):
                        pending_records.append({**row, "vector": vec})
                    pending_entry_ids.append(entry.entry_id)

                    # 按 Entry 数触发 flush：减少 LanceDB delete 次数（delete 通常更昂贵）
                    if len(pending_entry_ids) >= entry_batch_size:
                        _flush_entry_batch(
                            chunks_table,
                            entry_ids=pending_entry_ids,
                            records=pending_records,
                        )
                        pending_entry_ids.clear()
                        pending_records.clear()

                # Flush remaining entries
                if pending_entry_ids:
                    _flush_entry_batch(
                        chunks_table,
                        entry_ids=pending_entry_ids,
                        records=pending_records,
                    )
                    pending_entry_ids.clear()
                    pending_records.clear()

                # Update sync state if we processed new entries
                if max_entry_id > state.last_entry_id:
                    state.last_entry_id = max_entry_id
                    save_sync_state(state)

                # TTL cleanup
                run_ttl_cleanup(chunks_table, retention_days=settings.retention_days)

        except Exception as exc:  # noqa: BLE001
            # In a daemon context, do not crash on single failure; log and continue.
            print(f"[sync_daemon] Error during sync loop: {exc}")

        time.sleep(settings.check_interval)


def run_ttl_cleanup(chunks_table, retention_days: int) -> None:
    """
    删除超出保留窗口的旧切片。
    """
    if retention_days <= 0:
        return
    now_ts = int(time.time())
    threshold = now_ts - retention_days * 24 * 60 * 60
    where = "published_at < $threshold"
    params = {"threshold": threshold}
    try:
        chunks_table.delete(where=where, params=params)
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_daemon] Error during TTL cleanup: {exc}")


if __name__ == "__main__":
    main()
