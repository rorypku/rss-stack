from __future__ import annotations

import argparse
from datetime import datetime

from config import get_settings
from db_utils import clean_html_content, open_sqlite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a FreshRSS entry by entry_id and print plain text content."
    )
    parser.add_argument("entry_id", type=int, help="FreshRSS entry.id")
    return parser.parse_args()


def _format_published_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def fetch_entry_content(entry_id: int) -> tuple[str | None, str | None]:
    settings = get_settings()
    sqlite_path = settings.freshrss_sqlite_path

    if not sqlite_path.exists():
        raise FileNotFoundError(
            f"FreshRSS sqlite not found at {sqlite_path}; set FRESHRSS_SQLITE_PATH to configure."
        )

    query = "SELECT date, content FROM entry WHERE id = ?"
    with open_sqlite(sqlite_path) as conn:
        cur = conn.execute(query, [entry_id])
        row = cur.fetchone()

    if not row:
        return None, None

    published_date = _format_published_date(row[0])
    raw_content = str(row[1] or "")
    return published_date, clean_html_content(raw_content)


def main() -> None:
    args = parse_args()
    published_date, content = fetch_entry_content(args.entry_id)
    if content is None:
        raise SystemExit(f"Entry not found: {args.entry_id}")
    print(f"published_date: {published_date or ''}\n")
    print(content)


if __name__ == "__main__":
    main()
