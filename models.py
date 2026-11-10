"""Dynamic model discovery, reasoning effort resolution, and model catalog caching."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

try:
    from .process import (
        build_clean_child_env,
        resolve_antigravity_binary,
        setup_isolated_home,
    )
except ImportError:
    from process import (  # type: ignore[no-redef]
        build_clean_child_env,
        resolve_antigravity_binary,
        setup_isolated_home,
    )

logger = logging.getLogger(__name__)

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")

# Static fallback families used when `agy models` cannot be queried offline.
_FALLBACK_FAMILIES: dict[str, tuple[str, ...]] = {
    "gemini-4-argon": ("low", "medium", "high"),
    "gemini-3.8-flash": ("low", "medium", "high"),
    "claude-opus-5-5": ("low", "medium", "high"),
    "claude-sonnet-5-5": ("low", "medium", "high"),
    "gpt-oss-120b": ("medium",),
}

DEFAULT_MODEL = "gemini-3.8-flash"

MODEL_ALIASES: dict[str, str] = {
    # Convenience short names
    "gemini-argon": "gemini-4-argon",
    "gemini-flash": "gemini-3.8-flash",
    "gemini-pro": "gemini-4-argon",
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-opus": "claude-opus-5-5",
    "gpt-oss": "gpt-oss-120b",
    # Legacy model aliases (automatically redirect to active models)
    "gemini-3.1-pro": "gemini-4-argon",
    "claude-sonnet-4-6": "claude-sonnet-5-5",
    "claude-sonnet-4-5": "claude-sonnet-5-5",
    "claude-sonnet-4-5-thinking": "claude-sonnet-5-5",
    "claude-opus-4-6": "claude-opus-5-5",
    "claude-opus-4-6-thinking": "claude-opus-5-5",
    "claude-opus-4-5": "claude-opus-5-5",
    "claude-opus-4-5-thinking": "claude-opus-5-5",
    "claude-sonnet-5-5-thinking": "claude-sonnet-5-5",
    "claude-opus-5-5-thinking": "claude-opus-5-5",
}

# Bidirectional cross-version fallback when legacy model names are requested.
_CROSS_VERSION_FALLBACKS: dict[str, str] = {
    "gemini-3.1-pro": "gemini-4-argon",
    "claude-sonnet-4-6": "claude-sonnet-5-5",
    "claude-opus-4-6-thinking": "claude-opus-5-5",
    "claude-opus-4-6": "claude-opus-5-5",
}


def _build_models_from_families(families: dict[str, tuple[str, ...]]) -> list[str]:
    """Return clean base model IDs from families without duplicating reasoning effort variants."""
    return list(families.keys())


_FALLBACK_MODELS: list[str] = _build_models_from_families(_FALLBACK_FAMILIES)

_CACHE_LOCK = threading.Lock()
_CACHED_CATALOG: tuple[list[str], dict[str, tuple[str, ...]]] | None = None
_CACHE_EXPIRES_AT: float = 0.0
_POSITIVE_CACHE_TTL_SECONDS = 3600.0
_NEGATIVE_CACHE_TTL_SECONDS = 60.0


def invalidate_model_catalog_cache() -> None:
    """Clear the cached model catalog so the next call re-queries `agy models`."""
    global _CACHED_CATALOG, _CACHE_EXPIRES_AT
    with _CACHE_LOCK:
        _CACHED_CATALOG = None
        _CACHE_EXPIRES_AT = 0.0


def _fetch_dynamic_models(binary: str = "") -> tuple[list[str], dict[str, tuple[str, ...]]] | None:
    """Run `agy models` inside an isolated temporary HOME and parse available model families."""
    agy_bin = binary or resolve_antigravity_binary()
    if not agy_bin:
        return None

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes_agy_models_") as tmp_dir:
            isolated_home, _ = setup_isolated_home(tmp_dir)
            clean_env = build_clean_child_env(isolated_home)
            res = subprocess.run(
                [agy_bin, "models"],
                cwd=tmp_dir,
                env=clean_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
                **popen_kwargs,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Dynamic model discovery via `agy models` failed: %s", exc)
        return None

    if res.returncode != 0 or not res.stdout:
        return None

    raw_ids: list[str] = []
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "\t" not in stripped:
            continue
        model_id = stripped.split("\t", 1)[0].strip()
        if model_id and " " not in model_id:
            raw_ids.append(model_id)

    if not raw_ids:
        return None

    families_lists: dict[str, list[str]] = {}
    for mid in raw_ids:
        matched_effort = False
        for eff in EFFORT_LEVELS:
            suffix = f"-{eff}"
            if mid.endswith(suffix):
                base = mid[: -len(suffix)]
                if base:
                    bucket = families_lists.setdefault(base, [])
                    if eff not in bucket:
                        bucket.append(eff)
                    matched_effort = True
                break
        if not matched_effort:
            families_lists.setdefault(mid, [])

    if not families_lists:
        return None

    # Sort effort tuples consistently as ("low", "medium", "high")
    effort_order = {eff: idx for idx, eff in enumerate(EFFORT_LEVELS)}
    families: dict[str, tuple[str, ...]] = {
        base: tuple(sorted(effs, key=lambda e: effort_order.get(e, 99)))
        for base, effs in families_lists.items()
    }
    models = _build_models_from_families(families)
    return models, families


def get_model_catalog(binary: str = "") -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Return `(models_list, families_dict)` using a thread-safe TTL cache."""
    global _CACHED_CATALOG, _CACHE_EXPIRES_AT
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHED_CATALOG is not None and now < _CACHE_EXPIRES_AT:
            return _CACHED_CATALOG

    dynamic = _fetch_dynamic_models(binary)
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHED_CATALOG is not None and now < _CACHE_EXPIRES_AT:
            return _CACHED_CATALOG
        if dynamic is not None:
            _CACHED_CATALOG = dynamic
            _CACHE_EXPIRES_AT = now + _POSITIVE_CACHE_TTL_SECONDS
        else:
            _CACHED_CATALOG = (list(_FALLBACK_MODELS), dict(_FALLBACK_FAMILIES))
            _CACHE_EXPIRES_AT = now + _NEGATIVE_CACHE_TTL_SECONDS
        return _CACHED_CATALOG


def get_available_models(binary: str = "") -> list[str]:
    """Return the list of available model IDs."""
    models, _ = get_model_catalog(binary)
    return list(models)


def get_annotated_models(binary: str = "") -> list[dict[str, Any]]:
    """Return clean annotated model rows for `ProviderProfile.discover_models()`."""
    _, families = get_model_catalog(binary)
    rows: list[dict[str, Any]] = []
    for base, efforts in families.items():
        if efforts:
            eff_str = ", ".join(efforts)
            rows.append({
                "id": base,
                "label": base,
                "note": f"reasoning: {eff_str}",
            })
        else:
            rows.append({
                "id": base,
                "label": base,
                "note": "adaptive thinking",
            })
    return rows


def resolve_model_family_alias(
    base_model: str,
    families: dict[str, tuple[str, ...]],
) -> str:
    """Resolve `base_model` against `MODEL_ALIASES` and `families` with cross-version awareness.

    1. If `base_model` is in `MODEL_ALIASES` and the alias target exists in `families`,
       return the alias target.
    2. If `base_model` itself exists in `families` (e.g., `agy` 1.2 natively serving
       `claude-sonnet-5-5`), keep `base_model`.
    3. If `base_model` is in `_CROSS_VERSION_FALLBACKS` and that target exists in
       `families` (e.g., user asked for `claude-sonnet-4-6` on `agy` 1.2), return the
       cross-version target.
    4. Otherwise fall back to `MODEL_ALIASES.get(base_model, base_model)`.
    """
    if base_model in MODEL_ALIASES:
        target = MODEL_ALIASES[base_model]
        if target in families:
            return target
        if base_model in families:
            return base_model
        return target

    if base_model not in families and base_model in _CROSS_VERSION_FALLBACKS:
        alt = _CROSS_VERSION_FALLBACKS[base_model]
        if alt in families:
            return alt

    return base_model


def parse_model_and_effort(
    model_id: str,
    binary: str = "",
    *,
    families: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, str | None]:
    """Split a model identifier into `(base_family, explicit_effort_suffix_or_None)`."""
    cleaned = (model_id or "").strip()
    for prefix in ("antigravity-oauth/", "antigravity-subscription-directsdk/", "antigravity/", "agy/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break

    if not cleaned:
        cleaned = DEFAULT_MODEL

    if families is None:
        _, families = get_model_catalog(binary)

    if cleaned in families:
        return cleaned, None

    for eff in EFFORT_LEVELS:
        suffix = f"-{eff}"
        if cleaned.endswith(suffix):
            candidate = cleaned[: -len(suffix)]
            if candidate in families or candidate in MODEL_ALIASES or candidate in _FALLBACK_FAMILIES:
                return candidate, eff

    if cleaned in MODEL_ALIASES:
        return cleaned, None

    for eff in EFFORT_LEVELS:
        suffix = f"-{eff}"
        if cleaned.endswith(suffix):
            candidate = cleaned[: -len(suffix)]
            if candidate:
                return candidate, eff

    return cleaned, None
