#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_ENV_FILE = ".env"
DEFAULT_API_PATH = "/groups/51288148188224"
API_BASE_URL = "https://api.zsxq.com/v2"
TRANSIENT_ERROR_CODE = 1059
RESOURCE_ACCESS_DENIED_CODE = 1005
DEFAULT_RETRIES = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether ZSXQ_ACCESS_TOKEN is still accepted by the ZSXQ API."
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Path to env file. Default: {DEFAULT_ENV_FILE}",
    )
    parser.add_argument(
        "--token",
        help="Explicit token value. If omitted, read ZSXQ_ACCESS_TOKEN from the env file.",
    )
    parser.add_argument(
        "--api-path",
        default=DEFAULT_API_PATH,
        help=(
            "ZSXQ API path used for validation, for example /groups/<group_id> or /users/<user_id>. "
            f"Default: {DEFAULT_API_PATH}"
        ),
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
        default=DEFAULT_RETRIES,
        help=f"Retries for transient ZSXQ errors such as code {TRANSIENT_ERROR_CODE}. Default: {DEFAULT_RETRIES}",
    )
    return parser.parse_args()


def load_env_value(env_path: Path, key: str) -> str | None:
    if not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip().strip("'").strip('"')
        return value or None
    return None


def normalize_api_path(api_path: str) -> str:
    return api_path if api_path.startswith("/") else f"/{api_path}"


def build_request(token: str, api_path: str) -> urllib.request.Request:
    return urllib.request.Request(
        API_BASE_URL + normalize_api_path(api_path),
        headers={
            "cookie": f"zsxq_access_token={token};",
            "user-agent": "rss-stack-zsxq-check/1.0",
            "accept": "application/json",
        },
    )


def print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def fetch_payload(request: urllib.request.Request, timeout: float) -> dict[str, object]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body)


def main() -> int:
    args = parse_args()
    token = args.token or os.environ.get("ZSXQ_ACCESS_TOKEN")
    if not token:
        token = load_env_value(Path(args.env_file), "ZSXQ_ACCESS_TOKEN")

    if not token:
        print_json(
            {
                "status": "error",
                "message": "ZSXQ_ACCESS_TOKEN not found. Pass --token or define it in the env file.",
            }
        )
        return 2

    request = build_request(token, args.api_path)

    payload: dict[str, object] | None = None

    for attempt in range(args.retries + 1):
        try:
            payload = fetch_payload(request, args.timeout)
            code = payload.get("code")
            if payload.get("succeeded") or code != TRANSIENT_ERROR_CODE or attempt == args.retries:
                break
            time.sleep(min(1 + attempt, 3))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = body
            try:
                payload = json.loads(body)
                message = payload.get("info") or payload.get("error") or body
            except json.JSONDecodeError:
                payload = None
            print_json(
                {
                    "status": "invalid",
                    "http_status": exc.code,
                    "message": message,
                    "api_path": normalize_api_path(args.api_path),
                }
            )
            return 1
        except urllib.error.URLError as exc:
            print_json(
                {
                    "status": "error",
                    "message": str(exc.reason),
                    "api_path": normalize_api_path(args.api_path),
                }
            )
            return 3
        except json.JSONDecodeError as exc:
            print_json(
                {
                    "status": "error",
                    "message": f"Invalid JSON response: {exc}",
                    "api_path": normalize_api_path(args.api_path),
                }
            )
            return 3

    if payload is None:
        print_json(
            {
                "status": "error",
                "message": "No response payload received from ZSXQ API.",
                "api_path": normalize_api_path(args.api_path),
            }
        )
        return 3

    succeeded = bool(payload.get("succeeded"))
    code = payload.get("code")
    message = payload.get("info") or payload.get("error")

    if succeeded:
        print_json(
            {
                "status": "ok",
                "message": "ZSXQ_ACCESS_TOKEN is valid for the tested API path.",
                "api_path": normalize_api_path(args.api_path),
            }
        )
        return 0

    if code == TRANSIENT_ERROR_CODE:
        print_json(
            {
                "status": "unknown",
                "code": code,
                "message": f"{message or 'ZSXQ transient internal error.'} Retries exhausted; token validity remains inconclusive.",
                "api_path": normalize_api_path(args.api_path),
            }
        )
        return 4

    if code == RESOURCE_ACCESS_DENIED_CODE:
        print_json(
            {
                "status": "ok",
                "code": code,
                "message": f"{message or 'The tested resource is not accessible to the current account.'} Token itself appears valid.",
                "api_path": normalize_api_path(args.api_path),
            }
        )
        return 0

    print_json(
        {
            "status": "invalid",
            "code": code,
            "message": message or "ZSXQ API returned succeeded=false.",
            "api_path": normalize_api_path(args.api_path),
        }
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
