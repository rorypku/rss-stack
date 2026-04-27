#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "com.kai.rss-stack.zsxq-token-check"
DEFAULT_HOUR = 6
DEFAULT_MINUTE = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a macOS LaunchAgent that checks the ZSXQ token every day."
    )
    parser.add_argument("--hour", type=int, default=DEFAULT_HOUR, help="Run hour, 0-23. Default: 6")
    parser.add_argument("--minute", type=int, default=DEFAULT_MINUTE, help="Run minute, 0-59. Default: 0")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Unload and remove the LaunchAgent instead of installing it.",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def log_dir() -> Path:
    return Path.home() / "Library/Logs/rss-stack"


def run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def bootout(plist: Path) -> None:
    gui_target = f"gui/{os.getuid()}/{LABEL}"
    result = run_launchctl(["bootout", gui_target])
    if result.returncode == 0:
        return

    if plist.exists():
        run_launchctl(["bootout", f"gui/{os.getuid()}", str(plist)])


def uninstall() -> int:
    plist = plist_path()
    bootout(plist)
    if plist.exists():
        plist.unlink()
        print(f"Removed {plist}")
    else:
        print(f"LaunchAgent was not installed: {plist}")
    return 0


def install(hour: int, minute: int) -> int:
    if not 0 <= hour <= 23:
        print("error: --hour must be between 0 and 23", file=sys.stderr)
        return 2
    if not 0 <= minute <= 59:
        print("error: --minute must be between 0 and 59", file=sys.stderr)
        return 2

    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    if not Path(uv).exists():
        print("error: uv was not found. Install uv or adjust PATH before installing.", file=sys.stderr)
        return 1

    root = repo_root()
    logs = log_dir()
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "scripts/check_zsxq_token_notify.py",
        ],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {
            "Hour": hour,
            "Minute": minute,
        },
        "StandardOutPath": str(logs / "zsxq-token-check.log"),
        "StandardErrorPath": str(logs / "zsxq-token-check.err.log"),
    }

    with plist.open("wb") as file:
        plistlib.dump(payload, file, sort_keys=False)

    bootout(plist)
    result = run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist)])
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode

    run_launchctl(["enable", f"gui/{os.getuid()}/{LABEL}"])
    print(f"Installed {plist}")
    print(f"Schedule: every day at {hour:02d}:{minute:02d}")
    print(f"Logs: {logs / 'zsxq-token-check.log'}")
    return 0


def main() -> int:
    args = parse_args()
    if args.uninstall:
        return uninstall()
    return install(args.hour, args.minute)


if __name__ == "__main__":
    sys.exit(main())
