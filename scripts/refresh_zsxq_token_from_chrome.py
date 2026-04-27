#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "browser-cookie3>=0.20.1",
# ]
# ///

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from http.cookiejar import Cookie
from pathlib import Path

import browser_cookie3


COOKIE_NAME = "zsxq_access_token"
DOMAIN_NAME = "zsxq.com"
ENV_KEY = "ZSXQ_ACCESS_TOKEN"
DEFAULT_ENV_FILE = ".env"
DEFAULT_PROFILE_DIR = Path.home() / "Library/Application Support/Google/Chrome/Profile 4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read zsxq_access_token from Chrome Profile 4 and update "
            f"{ENV_KEY} in the env file."
        )
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Chrome profile directory. Default: {DEFAULT_PROFILE_DIR}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(DEFAULT_ENV_FILE),
        help=f"Env file to update. Default: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report the token without changing the env file.",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the full token. By default only a masked token is printed.",
    )
    return parser.parse_args()


def find_cookie_db(profile_dir: Path) -> Path:
    candidates = [
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Chrome Cookies database not found. Searched: {searched}")


def copy_cookie_db(cookie_db: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="zsxq-chrome-cookies-"))
    temp_cookie_db = temp_dir / "Cookies"
    shutil.copy2(cookie_db, temp_cookie_db)

    wal_file = cookie_db.with_name(cookie_db.name + "-wal")
    shm_file = cookie_db.with_name(cookie_db.name + "-shm")
    if wal_file.exists():
        shutil.copy2(wal_file, temp_cookie_db.with_name(temp_cookie_db.name + "-wal"))
    if shm_file.exists():
        shutil.copy2(shm_file, temp_cookie_db.with_name(temp_cookie_db.name + "-shm"))

    return temp_cookie_db


def cookie_sort_key(cookie: Cookie) -> tuple[int, int, str]:
    expires = cookie.expires or 0
    return (
        int(cookie.domain.endswith(DOMAIN_NAME)),
        expires,
        cookie.domain,
    )


def read_zsxq_token(cookie_db: Path) -> str:
    temp_cookie_db = copy_cookie_db(cookie_db)
    try:
        cookie_jar = browser_cookie3.chrome(
            cookie_file=str(temp_cookie_db),
            domain_name=DOMAIN_NAME,
        )
        matches = [
            cookie
            for cookie in cookie_jar
            if cookie.name == COOKIE_NAME and cookie.value and cookie.domain.endswith(DOMAIN_NAME)
        ]
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Could not read Chrome cookie database: {exc}") from exc
    finally:
        shutil.rmtree(temp_cookie_db.parent, ignore_errors=True)

    if not matches:
        raise LookupError(
            f"{COOKIE_NAME} was not found for {DOMAIN_NAME}. "
            "Open https://wx.zsxq.com in Chrome Profile 4 and make sure you are logged in."
        )

    return sorted(matches, key=cookie_sort_key, reverse=True)[0].value


def mask_token(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}...{token[-6:]}"


def update_env_file(env_file: Path, key: str, value: str) -> bool:
    assignment = f"{key}={value}"
    if not env_file.exists():
        env_file.write_text(assignment + "\n", encoding="utf-8")
        return True

    raw_text = env_file.read_text(encoding="utf-8")
    lines = raw_text.splitlines(keepends=True)
    changed = False
    found = False
    updated_lines: list[str] = []

    for line in lines:
        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line
        stripped = body.lstrip()

        if not found and stripped.startswith(f"{key}="):
            indent = body[: len(body) - len(stripped)]
            new_line = f"{indent}{assignment}{newline}"
            updated_lines.append(new_line)
            found = True
            changed = changed or new_line != line
        else:
            updated_lines.append(line)

    if not found:
        if raw_text and not raw_text.endswith("\n"):
            updated_lines.append("\n")
        updated_lines.append(assignment + "\n")
        changed = True

    if changed:
        env_file.write_text("".join(updated_lines), encoding="utf-8")

    return changed


def main() -> int:
    args = parse_args()
    profile_dir = args.profile_dir.expanduser()
    env_file = args.env_file.expanduser()

    try:
        cookie_db = find_cookie_db(profile_dir)
        token = read_zsxq_token(cookie_db)
    except (FileNotFoundError, LookupError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    display_token = token if args.print_token else mask_token(token)
    print(f"Read {ENV_KEY} from {cookie_db}: {display_token}")

    if args.dry_run:
        print("Dry run: env file was not changed.")
        return 0

    changed = update_env_file(env_file, ENV_KEY, token)
    action = "Updated" if changed else "Already up to date"
    print(f"{action}: {env_file}")
    print("Restart rsshub for Docker Compose to pick up the refreshed environment value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
