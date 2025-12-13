import time
from typing import List

from openai import OpenAI

from config import get_settings
from db_utils import EntryRow, clean_html_content, chunk_text, iter_new_entries, open_sqlite
from lancedb_utils import get_or_create_rss_chunks_table, load_sync_state, save_sync_state


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
                text_batch: list[str] = []
                row_batch: list[dict] = []
                batch_size = settings.embedding_batch_size

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

                    for idx, chunk in enumerate(chunks):
                        text_batch.append(chunk)
                        row_batch.append(
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

                        if len(text_batch) >= batch_size:
                            _flush_batch(client, chunks_table, text_batch, row_batch, settings)

                # Flush remaining batch
                if text_batch:
                    _flush_batch(client, chunks_table, text_batch, row_batch, settings)

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


def _flush_batch(
    client: OpenAI,
    chunks_table,
    text_batch: List[str],
    row_batch: List[dict],
    settings,
) -> None:
    """
    调用 SiliconFlow Embedding API，对当前批次的文本生成向量并写入 LanceDB。
    """
    try:
        response = client.embeddings.create(
            model=settings.embedding_model,
            input=text_batch,
            dimensions=settings.embedding_dim,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_daemon] Error during embedding batch call: {exc}")
        # 丢弃本批次，避免阻塞后续同步
        text_batch.clear()
        row_batch.clear()
        return

    vectors = [item.embedding for item in response.data]
    if len(vectors) != len(row_batch):
        print(
            f"[sync_daemon] Embedding batch size mismatch: texts={len(text_batch)}, vectors={len(vectors)}",
        )
        text_batch.clear()
        row_batch.clear()
        return

    records = []
    for row, vec in zip(row_batch, vectors):
        records.append({**row, "vector": vec})

    try:
        chunks_table.add(records)
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_daemon] Error adding batch to LanceDB: {exc}")

    text_batch.clear()
    row_batch.clear()


if __name__ == "__main__":
    main()
