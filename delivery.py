"""Opt-in short-delivery heuristic diagnostics (`ANTIGRAVITY_DELIVERY_WATCH=1`)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_MIN_REASONING_CHARS = 800
_MIN_RATIO = 5.0
_MAX_CONTENT_CHARS = 240
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _delivery_watch_enabled() -> bool:
    return os.environ.get("ANTIGRAVITY_DELIVERY_WATCH", "").strip().lower() in _TRUTHY


def _looks_incomplete_tail(text: str) -> bool:
    stripped = (text or "").rstrip()
    if not stripped:
        return True
    if stripped.endswith((":", ",", ";", "(", "[", "{", "\\", "-", "—")):
        return True
    if stripped.count("```") % 2 == 1:
        return True
    return False


def warn_if_short_delivery(
    *,
    model: str,
    finish_reason: str,
    stop_reason: str,
    content_text: str,
    reasoning_text: str,
    has_tool_calls: bool,
    output_tokens: int,
) -> None:
    """Emit a warning when `ANTIGRAVITY_DELIVERY_WATCH=1` and a turn looks prematurely truncated."""
    if not _delivery_watch_enabled():
        return
    if has_tool_calls or finish_reason != "stop":
        return

    r_len = len(reasoning_text or "")
    c_len = len(content_text or "")
    if r_len < _MIN_REASONING_CHARS:
        return
    if c_len > _MAX_CONTENT_CHARS:
        return
    ratio = r_len / max(1, c_len)
    if ratio < _MIN_RATIO and not _looks_incomplete_tail(content_text):
        return

    logger.warning(
        "Antigravity short delivery detected (model=%s, stop_reason=%s, reasoning_chars=%d, "
        "content_chars=%d, ratio=%.1f, output_tokens=%d)",
        model,
        stop_reason,
        r_len,
        c_len,
        ratio,
        output_tokens,
    )
