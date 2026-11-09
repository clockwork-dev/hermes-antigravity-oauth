"""Hermetic pytest bootstrap for `antigravity-oauth` plugin tests."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "hermes_antigravity_oauth"
PROVIDER_NAME = "antigravity-oauth"

_ISOLATED_HERMES_HOME: Path | None = None


def _ensure_isolated_hermes_home() -> Path:
    global _ISOLATED_HERMES_HOME
    if _ISOLATED_HERMES_HOME is None:
        _ISOLATED_HERMES_HOME = Path(tempfile.mkdtemp(prefix="hermes_agy_oauth_test_home_"))
        os.environ["HERMES_HOME"] = str(_ISOLATED_HERMES_HOME)
    return _ISOLATED_HERMES_HOME


def load_plugin_package() -> object:
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin package from {PLUGIN_DIR}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_isolated_hermes_home()
PLUGIN = load_plugin_package()


@pytest.fixture(autouse=True)
def _ensure_registered_profile():
    import providers

    profile = getattr(PLUGIN, "antigravity_oauth_profile")
    if providers.get_provider_profile(PROVIDER_NAME) is not profile:
        providers.register_provider(profile)
    yield


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    global _ISOLATED_HERMES_HOME
    if _ISOLATED_HERMES_HOME is not None:
        shutil.rmtree(_ISOLATED_HERMES_HOME, ignore_errors=True)
        _ISOLATED_HERMES_HOME = None
