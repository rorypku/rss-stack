from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from config import get_settings


settings = get_settings()


@dataclass
class EntryRow:
    entry_id: int
    guid: str
    title: str
    author: Optional[str]
    content: str
    link: str
    date: int
    last_seen: int
    is_read: bool
    is_favorite: bool
    feed_id: int
    category_id: Optional[int]
    category_name: Optional[str]


def open_sqlite(db_path: Path) -> sqlite3.Connection:
    # Use read-only URI mode to be safe.
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_category_name_to_id(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.execute("SELECT id, name FROM category")
    mapping: dict[str, int] = {}
    for row in cur.fetchall():
        cid, name = row
        if name:
            mapping[str(name)] = int(cid)
    return mapping


def resolve_allowed_category_ids(conn: sqlite3.Connection) -> list[int]:
    """
    Map SYNC_CATEGORIES (names) to category ids.
    """
    if not settings.sync_categories:
        return []
    name_to_id = load_category_name_to_id(conn)
    ids: list[int] = []
    for name in settings.sync_categories:
        cid = name_to_id.get(name)
        if cid is not None:
            ids.append(cid)
    return ids


def iter_new_entries(
    conn: sqlite3.Connection,
    last_entry_id: int,
) -> Iterable[EntryRow]:
    """
    Yield new entries (entry.id > last_entry_id), optionally filtered by categories.
    """
    allowed_category_ids = resolve_allowed_category_ids(conn)
    params: dict[str, object] = {"last_entry_id": last_entry_id}

    base_query = """
    SELECT
      e.id          AS entry_id,
      e.guid        AS guid,
      e.title       AS title,
      e.author      AS author,
      e.content     AS content,
      e.link        AS link,
      e.date        AS date,
      e.lastSeen    AS last_seen,
      e.is_read     AS is_read,
      e.is_favorite AS is_favorite,
      e.id_feed     AS feed_id,
      f.category    AS category_id,
      c.name        AS category_name
    FROM entry e
    JOIN feed f ON e.id_feed = f.id
    LEFT JOIN category c ON f.category = c.id
    WHERE e.id > :last_entry_id
    """

    if allowed_category_ids:
        # Use named parameters for category ids to avoid mixing styles.
        category_param_names = []
        for idx, cid in enumerate(allowed_category_ids):
            name = f"cat_{idx}"
            category_param_names.append(f":{name}")
            params[name] = cid
        in_clause = ",".join(category_param_names)
        base_query += f" AND f.category IN ({in_clause})"

    base_query += " ORDER BY e.id ASC"

    cur = conn.execute(base_query, params)

    for row in cur.fetchall():
        (
            entry_id,
            guid,
            title,
            author,
            content,
            link,
            date,
            last_seen,
            is_read,
            is_favorite,
            feed_id,
            category_id,
            category_name,
        ) = row
        yield EntryRow(
            entry_id=int(entry_id),
            guid=str(guid),
            title=str(title),
            author=str(author) if author is not None else None,
            content=str(content),
            link=str(link),
            date=int(date),
            last_seen=int(last_seen),
            is_read=bool(is_read),
            is_favorite=bool(is_favorite),
            feed_id=int(feed_id),
            category_id=int(category_id) if category_id is not None else None,
            category_name=str(category_name) if category_name is not None else None,
        )


def clean_html_content(html: str) -> str:
    """
    将 HTML 内容清洗为适合做语义搜索的纯文本：
    - 去除标签
    - 合并多余空格
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    # 归一化空白字符
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """
    基于词级窗口实现简单的滑动切块：
    - chunk_size: 每个切片最大词数
    - chunk_overlap: 前后切片之间的重叠词数
    """
    tokens = text.split()
    if not tokens:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    # 实际步长，不允许非正
    step = max(1, chunk_size - max(0, chunk_overlap))

    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_tokens = tokens[start:end]
        if not chunk_tokens:
            break
        chunks.append(" ".join(chunk_tokens))
        if end >= len(tokens):
            break
        start += step

    return chunks


def fetch_existing_entry_ids(
    conn: sqlite3.Connection,
    entry_ids: list[int],
) -> set[int]:
    """
    使用一条 IN 查询获取当前仍存在的 entry.id 集合。
    """
    if not entry_ids:
        return set()
    placeholders = ",".join("?" for _ in entry_ids)
    query = f"SELECT id FROM entry WHERE id IN ({placeholders})"
    cur = conn.execute(query, entry_ids)
    # sqlite3 默认返回 (id,) 形式的元组，但为兼容性起见，也处理直接返回 int 的情况
    ids: set[int] = set()
    for row in cur.fetchall():
        if isinstance(row, (tuple, list)) and row:
            ids.add(int(row[0]))
        else:
            ids.add(int(row))
    return ids
