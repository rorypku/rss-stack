from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import time

import lancedb
from lancedb.pydantic import LanceModel, Vector

from config import get_settings


settings = get_settings()


class RssChunk(LanceModel):
    # Primary id and timestamps
    entry_id: int
    published_at: int

    # Source and category info
    feed_id: int
    category_id: Optional[int] = None
    category_name: Optional[str] = None

    # Display info
    title: str
    link: str

    # Chunking
    chunk_index: int
    content: str
    vector: Vector(4096)


@dataclass
class SyncState:
    id: str
    last_entry_id: int
    last_sync_at: int


def get_db():
    """
    Open the LanceDB database using the configured URI.
    """
    return lancedb.connect(settings.lancedb_uri)


def get_or_create_rss_chunks_table():
    db = get_db()
    table_name = "rss_chunks"
    if table_name in db.table_names():
        return db.open_table(table_name)
    return db.create_table(table_name, schema=RssChunk)


def get_or_create_sync_state_table():
    db = get_db()
    table_name = "sync_state"
    if table_name in db.table_names():
        return db.open_table(table_name)

    from lancedb.pydantic import LanceModel

    class SyncStateModel(LanceModel):
        id: str
        last_entry_id: int
        last_sync_at: int

    return db.create_table(table_name, schema=SyncStateModel)


def load_sync_state(default_id: str = "default") -> SyncState:
    table = get_or_create_sync_state_table()
    rows = list(table.to_pandas().query("id == @default_id").itertuples(index=False))
    if not rows:
        return SyncState(id=default_id, last_entry_id=0, last_sync_at=0)
    row = rows[0]
    return SyncState(id=row.id, last_entry_id=int(row.last_entry_id), last_sync_at=int(row.last_sync_at))


def save_sync_state(state: SyncState) -> None:
    table = get_or_create_sync_state_table()
    now_ts = int(time.time())
    data = [
        {
            "id": state.id,
            "last_entry_id": state.last_entry_id,
            "last_sync_at": now_ts,
        }
    ]
    # Upsert semantics: delete existing row with same id then append new one
    table.delete(where="id == $id", params={"id": state.id})
    table.add(data)
