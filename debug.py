"""Opt-in prompt debug dumping (`ANTIGRAVITY_DEBUG_PROMPT=1`) with restrictive 0o600 permissions."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DUMP_LOCK = threading.Lock()
_DUMP_COUNTER = 0


def _debug_prompt_enabled() -> bool:
    return os.environ.get("ANTIGRAVITY_DEBUG_PROMPT", "").strip().lower() in _TRUTHY


def _resolve_debug_dir() -> Path:
    custom = os.environ.get("ANTIGRAVITY_DEBUG_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path(tempfile.gettempdir()) / "hermes_agy_debug"


def dump_prompt_if_enabled(
    *,
    prompt: str,
    model: str,
    mode: str,
    conversation_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> Path | None:
    """Write the outbound prompt to a mode-0600 file when `ANTIGRAVITY_DEBUG_PROMPT=1`."""
    if not _debug_prompt_enabled():
        return None

    global _DUMP_COUNTER
    with _DUMP_LOCK:
        _DUMP_COUNTER += 1
        seq = _DUMP_COUNTER

    debug_dir = _resolve_debug_dir()
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(debug_dir, 0o700)
        except OSError:
            pass

        ts = int(time.time() * 1000)
        filename = f"prompt_{ts}_{seq:04d}_{mode}.txt"
        target = debug_dir / filename
        tool_count = len(tools) if tools else 0
        header = (
            f"# model: {model}\n"
            f"# mode: {mode}\n"
            f"# conversation_id: {conversation_id or 'none'}\n"
            f"# tools_count: {tool_count}\n"
            f"# prompt_chars: {len(prompt)}\n"
            "---\n"
        )
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(header)
            fh.write(prompt)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target
    except OSError as exc:
        logger.debug("Failed to write Antigravity debug prompt dump: %s", exc)
        return None
