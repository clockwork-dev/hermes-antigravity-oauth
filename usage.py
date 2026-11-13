"""Thread-safe single-flight Antigravity subscription quota fetcher (`agy -p "/usage"`)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

if __package__:
    from .process import (
        build_clean_child_env,
        resolve_antigravity_binary,
        setup_isolated_home,
    )
else:
    import os
    import sys

    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)

    from process import (  # type: ignore[no-redef]
        build_clean_child_env,
        resolve_antigravity_binary,
        setup_isolated_home,
    )

logger = logging.getLogger(__name__)

_USAGE_CACHE_TTL_SECONDS = 30.0
_USAGE_NEGATIVE_TTL_SECONDS = 30.0
_USAGE_TIMEOUT_SECONDS = 15.0

_USAGE_LOCK = threading.Lock()
_CACHED_SNAPSHOT: AccountUsageSnapshot | None = None
_CACHE_EXPIRES_AT: float = 0.0
_HAS_CACHED_VALUE: bool = False
_FETCH_IN_PROGRESS: bool = False
_FETCH_COND = threading.Condition(_USAGE_LOCK)


def invalidate_usage_cache() -> None:
    """Invalidate the cached `/usage` quota snapshot."""
    global _CACHED_SNAPSHOT, _CACHE_EXPIRES_AT, _HAS_CACHED_VALUE
    with _USAGE_LOCK:
        _CACHED_SNAPSHOT = None
        _CACHE_EXPIRES_AT = 0.0
        _HAS_CACHED_VALUE = False


def _parse_iso_datetime(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC `datetime`."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _run_usage_subprocess() -> AccountUsageSnapshot | None:
    """Invoke `agy -p "/usage" --output-format json` inside an isolated temporary HOME."""
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    binary = resolve_antigravity_binary()
    if not binary:
        return None

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes_agy_usage_") as tmp_dir:
            isolated_home, _ = setup_isolated_home(tmp_dir)
            clean_env = build_clean_child_env(isolated_home)
            res = subprocess.run(
                [binary, "-p", "/usage", "--output-format", "json"],
                cwd=tmp_dir,
                env=clean_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_USAGE_TIMEOUT_SECONDS,
                check=False,
                **popen_kwargs,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Antigravity `/usage` subprocess failed: %s", exc)
        return None

    if res.returncode != 0 or not res.stdout:
        return None

    try:
        payload = json.loads(res.stdout.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    tier = str(
        payload.get("tier")
        or payload.get("plan")
        or (
            payload.get("command", {}).get("data", {}).get("plan")
            if isinstance(payload.get("command"), dict) and isinstance(payload.get("command", {}).get("data"), dict)
            else None
        )
        or "Subscription"
    ).strip()

    windows: list[AccountUsageWindow] = []
    seen_labels: set[str] = set()

    # 1. agy 1.5.8+ modern quota schema: command.data.groups & buckets
    cmd_data = payload.get("command")
    if isinstance(cmd_data, dict):
        data_obj = cmd_data.get("data")
        if isinstance(data_obj, dict):
            raw_groups = data_obj.get("groups")
            if isinstance(raw_groups, list):
                for grp in raw_groups:
                    if not isinstance(grp, dict):
                        continue
                    grp_name = str(grp.get("name") or "").strip()
                    buckets = grp.get("buckets")
                    if not isinstance(buckets, list):
                        continue
                    for bucket in buckets:
                        if not isinstance(bucket, dict):
                            continue
                        b_name = str(bucket.get("name") or bucket.get("id") or "").strip()
                        if grp_name and b_name:
                            if grp_name.lower() in b_name.lower():
                                label = b_name
                            else:
                                label = f"{grp_name} - {b_name}"
                        else:
                            label = grp_name or b_name or "Quota"

                        if label in seen_labels:
                            continue
                        seen_labels.add(label)

                        raw_fraction = bucket.get("remaining_fraction")
                        used_pct: float | None = None
                        if isinstance(raw_fraction, (int, float)) and not isinstance(raw_fraction, bool):
                            rem_clamped = max(0.0, min(1.0, float(raw_fraction)))
                            used_pct = round((1.0 - rem_clamped) * 100.0, 1)

                        reset_dt = _parse_iso_datetime(bucket.get("reset_time"))
                        windows.append(
                            AccountUsageWindow(
                                label=label,
                                used_percent=used_pct,
                                reset_at=reset_dt,
                                scope="model",
                            )
                        )

    # 2. Legacy schema: payload.get("models")
    if not windows:
        raw_models = payload.get("models")
        if isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("displayName") or item.get("model") or "Model").strip()
                if label in seen_labels:
                    continue
                seen_labels.add(label)

                raw_pct = item.get("remainingPercentage")
                used_pct = None
                if isinstance(raw_pct, (int, float)) and not isinstance(raw_pct, bool):
                    remaining_clamped = max(0.0, min(1.0, float(raw_pct)))
                    used_pct = round((1.0 - remaining_clamped) * 100.0, 1)

                reset_dt = _parse_iso_datetime(item.get("resetTime"))
                windows.append(
                    AccountUsageWindow(
                        label=label,
                        used_percent=used_pct,
                        reset_at=reset_dt,
                        scope="model",
                    )
                )

    # 3. TSV fallback: payload.get("response")
    if not windows:
        raw_resp = payload.get("response")
        if isinstance(raw_resp, str) and "\t" in raw_resp:
            for line in raw_resp.strip().splitlines():
                parts = [p.strip() for p in line.split("\t")]
                if len(parts) >= 3:
                    grp_name, b_name, pct_str = parts[0], parts[1], parts[2]
                    reset_str = parts[3] if len(parts) > 3 else None
                    if grp_name and b_name:
                        if grp_name.lower() in b_name.lower():
                            label = b_name
                        else:
                            label = f"{grp_name} - {b_name}"
                    else:
                        label = grp_name or b_name or "Quota"

                    if label in seen_labels:
                        continue
                    seen_labels.add(label)

                    used_pct = None
                    if pct_str.endswith("%"):
                        try:
                            rem_val = float(pct_str[:-1].strip())
                            rem_clamped = max(0.0, min(100.0, rem_val)) / 100.0
                            used_pct = round((1.0 - rem_clamped) * 100.0, 1)
                        except ValueError:
                            pass

                    reset_dt = _parse_iso_datetime(reset_str)
                    windows.append(
                        AccountUsageWindow(
                            label=label,
                            used_percent=used_pct,
                            reset_at=reset_dt,
                            scope="model",
                        )
                    )

    if not windows:
        return None

    return AccountUsageSnapshot(
        provider="antigravity-oauth",
        source="agy /usage",
        fetched_at=datetime.now(UTC),
        title="Google Antigravity Subscription",
        plan=tier,
        windows=tuple(windows),
    )


def fetch_antigravity_usage() -> AccountUsageSnapshot | None:
    """Return the cached or freshly queried `AccountUsageSnapshot` with single-flight coalescing."""
    global _CACHED_SNAPSHOT, _CACHE_EXPIRES_AT, _HAS_CACHED_VALUE, _FETCH_IN_PROGRESS

    now = time.monotonic()
    with _FETCH_COND:
        if _HAS_CACHED_VALUE and now < _CACHE_EXPIRES_AT:
            return _CACHED_SNAPSHOT

        if _FETCH_IN_PROGRESS:
            _FETCH_COND.wait(timeout=_USAGE_TIMEOUT_SECONDS + 2.0)
            if _HAS_CACHED_VALUE and time.monotonic() < _CACHE_EXPIRES_AT:
                return _CACHED_SNAPSHOT
            return _CACHED_SNAPSHOT

        _FETCH_IN_PROGRESS = True

    snapshot: AccountUsageSnapshot | None = None
    try:
        snapshot = _run_usage_subprocess()
    finally:
        with _FETCH_COND:
            _CACHED_SNAPSHOT = snapshot
            _HAS_CACHED_VALUE = True
            ttl = _USAGE_CACHE_TTL_SECONDS if snapshot is not None else _USAGE_NEGATIVE_TTL_SECONDS
            _CACHE_EXPIRES_AT = time.monotonic() + ttl
            _FETCH_IN_PROGRESS = False
            _FETCH_COND.notify_all()

    return snapshot
