"""Terminal debug lines for chat + tooling. Disable with AISB_DEBUG=0 in the environment."""
from __future__ import annotations

import os


def _enabled() -> bool:
    return os.getenv("AISB_DEBUG", "1").strip().lower() not in ("0", "false", "no", "off")


def debug(tag: str, msg: str) -> None:
    if _enabled():
        print(f"[{tag}] {msg}", flush=True)


def debug_trim(tag: str, msg: str, limit: int = 280) -> None:
    text = msg if len(msg) <= limit else msg[: limit - 3] + "..."
    debug(tag, text)
