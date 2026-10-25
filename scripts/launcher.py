"""Helpers for start.bat.

These live in a file rather than inline in the batch script because quoting
Python inside cmd.exe is a reliable way to produce "undefined" and no
explanation.

    python -m scripts.launcher mode   # say which agents will run
    python -m scripts.launcher wait   # block until the server answers
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

HEALTH = "http://localhost:8000/health"


def describe_mode() -> int:
    from app.config import get_settings

    settings = get_settings()
    offline = (
        settings.force_offline_reasoner
        or settings.llm_provider == "offline"
        or not (settings.anthropic_api_key or settings.openai_api_key)
    )
    if offline:
        print("      offline reasoner  (no API key in .env)")
        print("      everything works, but no language model is called")
    else:
        provider = "Anthropic" if settings.anthropic_api_key else settings.openai_base_url
        print(f"      {settings.model_name}")
        print(f"      via {provider}")
    return 0


def wait_for_server(timeout_seconds: float = 25.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=1.5) as response:
                if response.status == 200:
                    print("      ready")
                    return 0
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.4)
    print("      the server did not come up - look at the server window")
    return 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "mode":
        return describe_mode()
    if command == "wait":
        return wait_for_server()
    print(f"unknown command: {command!r}; expected 'mode' or 'wait'")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
