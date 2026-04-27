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
DEFAULT_TIMES = ("00:00", "06:00", "12:00", "18:00")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install a macOS LaunchAgent that checks the ZSXQ token every day."
    )
    parser.add_argument(
        "--time",
        action="append",
        help=(
            "Run time in HH:MM format. Can be repeated. "
            f"Default: {', '.join(DEFAULT_TIMES)}"
        ),
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Unload and remove the LaunchAgent instead of installing it.",
    )
    return parser.parse_args()


def parse_time(value: str) -> dict[str, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}; expected HH:MM") from None

    if hour == 24 and minute == 0:
        hour = 0
    elif not 0 <= hour <= 23:
        raise argparse.ArgumentTypeError(f"invalid hour in {value!r}; expected 00-23 or 24:00")
    if not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError(f"invalid minute in {value!r}; expected 00-59")

    return {"Hour": hour, "Minute": minute}


def schedule_from_args(times: list[str] | None) -> list[dict[str, int]]:
    raw_times = times or list(DEFAULT_TIMES)
    parser = argparse.ArgumentParser(prog="--time")
    schedule: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()

    for raw_time in raw_times:
        try:
            item = parse_time(raw_time)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        key = (item["Hour"], item["Minute"])
        if key not in seen:
            schedule.append(item)
            seen.add(key)

    return sorted(schedule, key=lambda item: (item["Hour"], item["Minute"]))


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


def format_schedule(schedule: list[dict[str, int]]) -> str:
    return ", ".join(f"{item['Hour']:02d}:{item['Minute']:02d}" for item in schedule)


def install(schedule: list[dict[str, int]]) -> int:
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
        "StartCalendarInterval": schedule,
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
    print(f"Schedule: every day at {format_schedule(schedule)}")
    print(f"Logs: {logs / 'zsxq-token-check.log'}")
    return 0


def main() -> int:
    args = parse_args()
    if args.uninstall:
        return uninstall()
    return install(schedule_from_args(args.time))


if __name__ == "__main__":
    sys.exit(main())
