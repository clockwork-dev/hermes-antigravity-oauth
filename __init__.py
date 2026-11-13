"""Hermes Agent model-provider plugin for Google Antigravity OAuth subscriptions."""

from __future__ import annotations

import logging
from typing import Any

from hermes_cli.auth_constants import AuthError
from providers import register_provider
from providers.base import ProviderProfile

if __package__:
    from .client import AntigravityClient
    from .models import (
        _FALLBACK_FAMILIES,
        _FALLBACK_MODELS,
        DEFAULT_MODEL,
        EFFORT_LEVELS,
        MODEL_ALIASES,
        get_annotated_models,
        get_available_models,
        get_model_catalog,
        invalidate_model_catalog_cache,
        parse_model_and_effort,
        resolve_model_family_alias,
    )
    from .process import (
        build_clean_child_env,
        check_antigravity_auth,
        check_antigravity_token_file_evidence,
        describe_antigravity_auth_source,
        remove_isolated_antigravity_auth,
        resolve_antigravity_binary,
        resolve_hermes_antigravity_home,
        run_isolated_antigravity_login,
        setup_isolated_home,
        sync_updated_isolated_tokens,
    )
    from .usage import fetch_antigravity_usage, invalidate_usage_cache
else:
    import os
    import sys

    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)

    from client import AntigravityClient  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        _FALLBACK_FAMILIES,
        _FALLBACK_MODELS,
        DEFAULT_MODEL,
        EFFORT_LEVELS,
        MODEL_ALIASES,
        get_annotated_models,
        get_available_models,
        get_model_catalog,
        invalidate_model_catalog_cache,
        parse_model_and_effort,
        resolve_model_family_alias,
    )
    from process import (  # type: ignore[no-redef]
        build_clean_child_env,
        check_antigravity_auth,
        check_antigravity_token_file_evidence,
        describe_antigravity_auth_source,
        remove_isolated_antigravity_auth,
        resolve_antigravity_binary,
        resolve_hermes_antigravity_home,
        run_isolated_antigravity_login,
        setup_isolated_home,
        sync_updated_isolated_tokens,
    )
    from usage import fetch_antigravity_usage, invalidate_usage_cache  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

PROVIDER_NAME = "antigravity-oauth"
PROVIDER_ALIASES = (
    "antigravity",
    "agy",
    "agy-oauth",
    "antigravity-subscription",
    "antigravity-direct",
    "antigravity-subscription-directsdk",
)


class AntigravityOAuthProfile(ProviderProfile):
    """Hermes `ProviderProfile` bridging Google Antigravity OAuth subscriptions via `agy`."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # ProviderProfile declares `auth_handler`, `classify_api_error`, and
        # `setup_reasoning_efforts` as dataclass fields defaulting to None.
        if self.__dict__.get("auth_handler") is None:
            self.auth_handler = self._handle_auth
        if self.__dict__.get("classify_api_error") is None:
            self.classify_api_error = self._classify_error
        if self.__dict__.get("setup_reasoning_efforts") is None:
            self.setup_reasoning_efforts = self.supported_reasoning_efforts

    def create_client(
        self,
        credentials: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> AntigravityClient:
        binary = resolve_antigravity_binary()
        # Do not use user home as cwd; use None so AntigravityClient creates an isolated tempdir
        return AntigravityClient(binary=binary, cwd=None, timeout=timeout)

    def external_auth_evidence(self) -> tuple[bool, str]:
        """Fast subprocess-free check for an OAuth token file on disk."""
        return check_antigravity_token_file_evidence()

    def missing_credentials_hint(self) -> str:
        """Actionable instructions when Antigravity OAuth credentials are missing."""
        return (
            "Google Antigravity OAuth credentials not found.\n"
            "  1. Install Google Antigravity from https://antigravity.google/download (provides `agy`)\n"
            "  2. Authenticate your subscription via `hermes auth add antigravity-oauth` or `agy acp --login`"
        )

    def setup_status(self) -> dict[str, Any]:
        """Interactive `hermes model` / `hermes setup model` status with inline OAuth login command."""
        binary = resolve_antigravity_binary()
        if not binary:
            detail = (
                "Google Antigravity CLI (`agy`) is not installed or not on PATH. "
                "Install from https://antigravity.google/download or set ANTIGRAVITY_BINARY."
            )
            return {
                "available": False,
                "logged_in": False,
                "ready": False,
                "binary": "",
                "detail": detail,
                "reason": detail,
                "hint": detail,
            }

        if not check_antigravity_auth():
            detail = "Not signed in to Google Antigravity OAuth. Run `agy acp --login` or `hermes auth add antigravity-oauth`."
            login_cmd = [binary, "acp", "--login"]
            return {
                "available": True,
                "logged_in": False,
                "ready": False,
                "binary": binary,
                "detail": detail,
                "reason": detail,
                "hint": detail,
                "login_command": login_cmd,
                "login_cmd": login_cmd,
                "login_label": "Sign in to Google Antigravity via browser OAuth (agy acp --login)",
            }

        source = describe_antigravity_auth_source() or "Google Antigravity OAuth"
        return {
            "available": True,
            "logged_in": True,
            "ready": True,
            "binary": binary,
            "plan": source,
            "detail": f"Authenticated via {source}",
            "reason": f"Authenticated via {source}",
        }

    def _handle_auth(self, action: str, args: Any = None) -> bool:
        """Handle `hermes auth add|status|refresh|logout antigravity-oauth`."""
        act = (action or "status").strip().lower()
        binary = resolve_antigravity_binary()

        if act == "status":
            print(f"Provider: {self.name}")
            print(f"Binary:   {binary or 'NOT FOUND (install from https://antigravity.google/download)'}")
            source = describe_antigravity_auth_source()
            if source:
                print(f"Status:   Authenticated ({source})")
            else:
                print("Status:   Not authenticated")
                print(f"Hint:     {self.missing_credentials_hint()}")
            return True

        if act in ("add", "login"):
            if not binary:
                raise AuthError(
                    "Google Antigravity CLI (`agy`) was not found on PATH. "
                    "Install Antigravity from https://antigravity.google/download or set ANTIGRAVITY_BINARY.",
                    provider=self.name,
                    relogin_required=False,
                )
            hermes_home = resolve_hermes_antigravity_home()
            print(
                f"Launching isolated Google Antigravity OAuth login for Hermes "
                f"(profile: {hermes_home}) via `{binary} acp --login`..."
            )
            returncode = run_isolated_antigravity_login(binary)
            invalidate_model_catalog_cache()
            invalidate_usage_cache()
            if returncode != 0 and not check_antigravity_auth():
                raise AuthError(
                    f"`{binary} acp --login` exited with status {returncode} and no OAuth credentials were found.",
                    provider=self.name,
                    relogin_required=True,
                )
            source = describe_antigravity_auth_source()
            print(f"Authenticated successfully ({source or 'Google Antigravity OAuth'}).")
            return True

        if act == "refresh":
            invalidate_model_catalog_cache()
            invalidate_usage_cache()
            source = describe_antigravity_auth_source()
            if not source:
                raise AuthError(
                    "No Google Antigravity OAuth credentials found to refresh. "
                    "Run `hermes auth add antigravity-oauth` to sign in.",
                    provider=self.name,
                    relogin_required=True,
                )
            models = get_available_models(binary)
            print(f"Refreshed {self.name}: {source} ({len(models)} models available).")
            return True

        if act in ("logout", "remove"):
            removed = remove_isolated_antigravity_auth()
            invalidate_model_catalog_cache()
            invalidate_usage_cache()
            if removed:
                print(
                    f"Removed Hermes-isolated Antigravity OAuth token ({removed[0]}). "
                    "Your global `agy` CLI account was not modified."
                )
            else:
                print(
                    "No Hermes-isolated Antigravity token found. To sign out of your global `agy` CLI account, "
                    "run `agy` and select logout, or remove ~/.gemini/antigravity-cli/jetski-standalone-oauth-token "
                    "(or the `gemini:antigravity` OS keyring entry)."
                )
            return True

        raise ValueError(f"Unsupported auth action for {self.name}: {action!r}")

    def discover_models(
        self,
        credentials: dict[str, Any] | None = None,
        *,
        quiet: bool = False,
    ) -> list[dict[str, Any]]:
        """Return annotated model rows (`id`, `label`, `note`) for `hermes model`."""
        return get_annotated_models()

    def fetch_models(self, **kwargs: Any) -> list[str]:
        """Return flat list of available model IDs."""
        return get_available_models()

    def supported_reasoning_efforts(self, model: str) -> tuple[str, ...] | None:
        """Return valid reasoning effort levels for `model`."""
        _, families = get_model_catalog()
        base, _ = parse_model_and_effort(model, families=families)
        base = resolve_model_family_alias(base, families)
        if base in families:
            return families[base]
        return EFFORT_LEVELS

    def build_api_kwargs_extras(
        self,
        model: str = "",
        *,
        reasoning_config: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        reasoning_effort: str = "",
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Provider-specific kwargs split between `(extra_body_additions, top_level_kwargs)`."""
        raw = (reasoning_effort or "").strip().lower()
        if not raw and isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                raw = "none"
            else:
                raw = str(reasoning_config.get("effort") or reasoning_config.get("reasoning_effort") or "").strip().lower()
        if not raw and isinstance(config, dict):
            raw = str(config.get("reasoning_effort") or "").strip().lower()

        if not raw or raw in ("auto", "default"):
            return {}, {}

        if raw in ("none", "minimal", "low"):
            norm = "low"
        elif raw == "medium":
            norm = "medium"
        elif raw in ("high", "xhigh", "max"):
            norm = "high"
        else:
            return {}, {}

        return {"reasoning_effort": norm}, {"reasoning_effort": norm}

    def get_model_context_length(
        self,
        model: str,
        *,
        base_url: str = "",
        api_key: str = "",
    ) -> int | None:
        """Return known context window sizes for Antigravity model families."""
        _, families = get_model_catalog()
        base, _ = parse_model_and_effort(model, families=families)
        base = resolve_model_family_alias(base, families).lower()

        if base.startswith("gemini-"):
            return 1_000_000
        if base.startswith("claude-"):
            return 200_000
        if "gpt-oss" in base:
            return 128_000
        return None

    def _classify_error(
        self,
        error: Exception,
        *,
        status_code: int | None = None,
        error_code: str = "",
        message: str = "",
        body: Any = None,
        model: str = "",
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Classify Antigravity-specific errors for Hermes failover and recovery.

        Returns a verdict dict for Antigravity-specific patterns, or `None` to defer
        to Hermes's built-in error classifier pipeline.
        """
        msg = f"{error} {message}".lower()

        # 1. Output token cutoff — retryable as server_error WITHOUT compressing prompt context
        if (
            "exceeded the output token limit" in msg
            or "previous response was cut off" in msg
            or "output token limit" in msg
        ):
            return {
                "reason": "server_error",
                "retryable": True,
                "should_compress": False,
                "should_fallback": False,
            }

        # 2. Trajectory / context window exhaustion or stream stall under large context
        if any(
            pat in msg
            for pat in (
                "max_trajectory_tokens",
                "trajectory token",
                "subscriber fell behind",
                "prompt is too long",
                "context length exceeded",
                "input exceeds the maximum",
            )
        ):
            return {
                "reason": "context_overflow",
                "retryable": True,
                "should_compress": True,
                "should_fallback": False,
            }

        # 3. Subscription AI credit / billing exhaustion
        if any(
            pat in msg
            for pat in (
                "ai credits balance is too low",
                "credits balance",
                "insufficient credits",
                "quota has been exhausted",
                "upgrade your plan",
            )
        ):
            return {
                "reason": "billing",
                "retryable": False,
                "should_compress": False,
                "should_fallback": True,
            }

        # 4. OAuth / account verification errors
        if any(
            pat in msg
            for pat in (
                "verify your account",
                "appeal a terms of service",
                "terms of service block",
                "terms of service violation",
                "oauth token expired",
                "not authenticated",
                "no google antigravity oauth credentials",
                "unauthenticated",
                "invalid_grant",
            )
        ):
            return {
                "reason": "auth_permanent",
                "retryable": False,
                "should_compress": False,
                "should_fallback": True,
            }

        # 5. Model not found / unavailable
        if any(
            pat in msg
            for pat in (
                "model not found",
                "unknown model",
                "unsupported model",
                "model_not_found",
            )
        ):
            return {
                "reason": "model_not_found",
                "retryable": False,
                "should_compress": False,
                "should_fallback": True,
            }

        # 6. Rate limit / capacity exhaustion (429 / RESOURCE_EXHAUSTED)
        if any(
            pat in msg
            for pat in (
                "resource_exhausted",
                "exhausted your capacity",
                "429 too many requests",
                "ratelimitexceeded",
                "rate limit",
            )
        ):
            return {
                "reason": "rate_limit",
                "retryable": True,
                "should_compress": False,
                "should_fallback": True,
            }

        return None

    def fetch_account_usage(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Query Antigravity subscription quota via `agy -p '/usage' --output-format json`."""
        return fetch_antigravity_usage()


AntigravityProfile = AntigravityOAuthProfile

antigravity_oauth_profile = AntigravityOAuthProfile(
    name=PROVIDER_NAME,
    aliases=PROVIDER_ALIASES,
    display_name="Google Antigravity OAuth",
    description="Use your Google Antigravity subscription via official OAuth CLI (agy)",
    base_url="antigravity://local-oauth-cli",
    api_mode="chat_completions",
    auth_type="external_process",
    process_command="agy",
    process_args=("--output-format", "stream-json"),
    process_command_env_vars=("ANTIGRAVITY_BINARY", "AGY_CLI_PATH"),
    default_aux_model="gemini-3.8-flash",
    model_aliases=dict(MODEL_ALIASES),
    fallback_models=tuple(_FALLBACK_MODELS),
    static_models=tuple(_FALLBACK_MODELS),
    supports_streaming=True,
    supports_vision=True,
    supports_health_check=False,
    unsupported_response_formats=("json_schema",),
)

antigravity_profile = antigravity_oauth_profile

register_provider(antigravity_oauth_profile)

__all__ = [
    "AntigravityClient",
    "AntigravityOAuthProfile",
    "AntigravityProfile",
    "DEFAULT_MODEL",
    "EFFORT_LEVELS",
    "MODEL_ALIASES",
    "PROVIDER_ALIASES",
    "PROVIDER_NAME",
    "antigravity_oauth_profile",
    "antigravity_profile",
    "build_clean_child_env",
    "check_antigravity_auth",
    "check_antigravity_token_file_evidence",
    "describe_antigravity_auth_source",
    "fetch_antigravity_usage",
    "get_annotated_models",
    "get_available_models",
    "get_model_catalog",
    "invalidate_model_catalog_cache",
    "invalidate_usage_cache",
    "parse_model_and_effort",
    "remove_isolated_antigravity_auth",
    "resolve_antigravity_binary",
    "resolve_hermes_antigravity_home",
    "resolve_model_family_alias",
    "run_isolated_antigravity_login",
    "setup_isolated_home",
    "sync_updated_isolated_tokens",
]
