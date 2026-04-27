#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_ENV_FILE = ".env"
DEFAULT_API_PATH = "/groups/51288148188224"
NOTIFICATION_TITLE = "rss-stack"
NOTIFICATION_SUBTITLE = "知识星球 token 检查失败"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run check_zsxq_token.py and show a macOS notification if it fails."
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Path to env file. Default: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--api-path",
        default=DEFAULT_API_PATH,
        help=f"ZSXQ API path used for validation. Default: {DEFAULT_API_PATH}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        help="Request timeout in seconds. Default: 15",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries for transient ZSXQ errors. Default: 3",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_check(args: argparse.Namespace) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    command = [
        uv,
        "run",
        "scripts/check_zsxq_token.py",
        "--env-file",
        args.env_file,
        "--api-path",
        args.api_path,
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
    ]
    return subprocess.run(
        command,
        cwd=repo_root(),
        text=True,
        capture_output=True,
        check=False,
    )


def parse_message(stdout: str, stderr: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        output = (stderr or stdout).strip()
        return output or "check_zsxq_token.py exited with no diagnostic output."

    status = payload.get("status") or "unknown"
    message = payload.get("message") or payload.get("error") or "No message returned."
    api_path = payload.get("api_path")
    code = payload.get("code") or payload.get("http_status")

    parts = [f"status={status}"]
    if code is not None:
        parts.append(f"code={code}")
    if api_path:
        parts.append(f"path={api_path}")
    parts.append(str(message))
    return " | ".join(parts)


def escape_osascript_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def notify(message: str) -> None:
    script = (
        f'display notification "{escape_osascript_text(message)}" '
        f'with title "{escape_osascript_text(NOTIFICATION_TITLE)}" '
        f'subtitle "{escape_osascript_text(NOTIFICATION_SUBTITLE)}"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def main() -> int:
    args = parse_args()
    result = run_check(args)

    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if result.returncode == 0:
        return 0

    message = parse_message(result.stdout, result.stderr)
    notify(message)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
