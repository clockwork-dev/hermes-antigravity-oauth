"""Subprocess lifecycle, zero-exfiltration OAuth verification, and environment sanitization."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Any, Callable

logger = logging.getLogger(__name__)

# Patterns indicating rate limiting, quota exhaustion, or capacity errors in Antigravity logs.
_ERROR_LOG_PATTERNS = re.compile(
    r"(RESOURCE_EXHAUSTED|exhausted your capacity|quota.*reset|429\s+Too\s+Many\s+Requests|model_Validation|RateLimitExceeded)",
    re.IGNORECASE,
)

# Strictly non-secret environment variables required for child execution.
_SAFE_ENV_KEYS = frozenset({
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TEMP",
    "TMP",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    # Windows process and credential subsystem essentials
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    # Linux keyring / D-Bus / XDG IPC discovery
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    # Enterprise / custom TLS CA trust bundles
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # Antigravity CLI config location override
    "ANTIGRAVITY_CONFIG_DIR",
})

# Environment variable names or substrings that commonly carry credentials/secrets.
_SECRET_KEY_PATTERNS = re.compile(
    r"(?:^|_)(?:"
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|AUTH|BEARER|"
    r"COOKIE|SESSION|PRIVATE|JWT|SIGNATURE|CERT|PEM|WEBHOOK|CONN|CONNECTION|"
    r"DSN|DATABASE_URL|REDIS_URL|MONGO_URL|AMQP_URL|SMTP_URL|"
    r"AWS_ACCESS|AWS_SECRET|AWS_SESSION|AZURE_CLIENT|GCP_|GOOGLE_APPLICATION|"
    r"OPENAI|ANTHROPIC|GEMINI|CLAUDE|COHERE|MISTRAL|GROQ|DEEPSEEK|OPENROUTER|"
    r"HUGGINGFACE|HF_TOKEN|REPLICATE|TOGETHER|FIREWORKS|PERPLEXITY|XAI|"
    r"GITHUB|GH_TOKEN|GITLAB|BITBUCKET|NPM_TOKEN|PYPI|DOCKER|KUBECONFIG|"
    r"SLACK|DISCORD|TELEGRAM|TWILIO|SENDGRID|STRIPE|PAYPAL|CLOUDFLARE|"
    r"SUPABASE|NEON|PLANETSCALE|TURSO|UPSTASH|ALGOLIA| DATADOG|SENTRY|"
    r" VAULT|CONSUL|NOMAD|TERRAFORM|PULUMI|ANSIBLE|HERMES_KEY|NOUS"
    r")(?:$|_)",
    re.IGNORECASE,
)

# Value patterns that look like API keys, tokens, JWTs, or private keys regardless of var name.
_SECRET_VALUE_PATTERNS = re.compile(
    r"^(?:"
    r"sk-[a-zA-Z0-9_\-]{16,}|"
    r"rk-[a-zA-Z0-9_\-]{16,}|"
    r"pk-[a-zA-Z0-9_\-]{16,}|"
    r"gh[pousr]_[a-zA-Z0-9_]{20,}|"
    r"github_pat_[a-zA-Z0-9_]{20,}|"
    r"glpat-[a-zA-Z0-9_\-]{16,}|"
    r"xox[baprs]-[a-zA-Z0-9\-]{16,}|"
    r"AIza[0-9A-Za-z_\-]{30,}|"
    r"ya29\.[0-9A-Za-z_\-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"ASIA[0-9A-Z]{16}|"
    r"eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}|"
    r"hf_[a-zA-Z0-9]{20,}|"
    r"r8_[a-zA-Z0-9]{20,}|"
    r"gsk_[a-zA-Z0-9]{20,}|"
    r"pplx-[a-zA-Z0-9]{20,}|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://[^/\s]+:[^@\s]+@"
    r")"
)

# Proxy variables stripped in heuristic mode unless explicitly passed via ANTIGRAVITY_PROXY.
_PROXY_ENV_KEYS = frozenset({
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
})

# Dynamic loader / runtime injection variables that must never cross into the child process.
_DANGEROUS_LOADER_ENV_KEYS = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PERL5OPT",
    "RUBYOPT",
    "JAVA_TOOL_OPTIONS",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
})

_SSH_AGENT_ENV_KEYS = frozenset({
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
})

# OAuth token filenames across Antigravity CLI versions:
# - agy >= 1.2: jetski-standalone-oauth-token
# - agy 1.1:    antigravity-oauth-token
_OAUTH_TOKEN_FILENAMES: tuple[str, ...] = (
    "jetski-standalone-oauth-token",
    "antigravity-oauth-token",
)

# OS Keyring identifiers used by `agy` (Go `zalando/go-keyring` library):
# service = "gemini", user/account = "antigravity" (Windows target: "gemini:antigravity").
_KEYRING_SERVICE = "gemini"
_KEYRING_ACCOUNT = "antigravity"
_WINDOWS_CREDENTIAL_TARGET = f"{_KEYRING_SERVICE}:{_KEYRING_ACCOUNT}"
_KEYRING_PROBE_TIMEOUT_SECONDS = 3.0
_KEYRING_BYPASS_SSH_MARKER = "127.0.0.1 0 127.0.0.1 22"


def _is_path_home_mocked() -> bool:
    """Return True when `pathlib.Path.home` has been patched by unit tests."""
    home_mod = getattr(Path.home, "__module__", "") or ""
    return not home_mod.startswith("pathlib")


def _posix_account_homes() -> list[Path]:
    """Return unique candidate home directories for the current user on POSIX/Windows."""
    homes: list[Path] = []
    try:
        homes.append(Path.home())
    except (OSError, RuntimeError):
        pass
    if os.name == "posix" and not _is_path_home_mocked():
        try:
            import pwd

            pw_dir = pwd.getpwuid(os.getuid()).pw_dir
            if pw_dir:
                p = Path(pw_dir)
                if p not in homes:
                    homes.append(p)
        except Exception:
            pass
    return homes


def resolve_hermes_antigravity_home(home_dir: Path | None = None) -> Path:
    """Return the persistent Hermes-owned isolated HOME directory for Antigravity OAuth.

    Storing Hermes's OAuth token here (instead of the global OS keyring or `~/.gemini`)
    allows Hermes Agent to use a dedicated Google account without switching or
    overwriting the user's primary `agy` CLI / Antigravity IDE account.

    Resolution order:
    1. `ANTIGRAVITY_HERMES_HOME` environment override (if set)
    2. `HERMES_HOME` environment variable (used by Linux `systemd` units and custom profiles),
       when `home_dir` is not explicitly passed and `Path.home` is not mocked by unit tests
    3. `<home_dir>/AppData/Local/hermes/antigravity-oauth` (Windows) or
       `<home_dir>/<HERMES_DATA_DIR_SUFFIX or .hermes>/antigravity-oauth` (Linux / macOS)
    """
    explicit = os.environ.get("ANTIGRAVITY_HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    if home_dir is None and not _is_path_home_mocked():
        hermes_home_env = os.environ.get("HERMES_HOME", "").strip()
        if hermes_home_env:
            candidate = Path(hermes_home_env).expanduser()
            # Ignore only the hermetic pytest bootstrap directory from conftest.py
            if not candidate.name.startswith("hermes_agy_oauth_test_home_"):
                return candidate / "antigravity-oauth"

    root = home_dir if home_dir is not None else Path.home()
    win_hermes = root / "AppData" / "Local" / "hermes"
    if win_hermes.is_dir():
        return win_hermes / "antigravity-oauth"

    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "").strip() or ".hermes"
    return root / suffix / "antigravity-oauth"


def _resolve_oauth_token_candidates(home_dir: Path | None = None) -> list[Path]:
    """Return candidate OAuth token paths in priority order:

    1. `ANTIGRAVITY_CONFIG_DIR` override (if set)
    2. Hermes-isolated profile (`<hermes_home>/antigravity-oauth/.gemini/antigravity-cli/`)
    3. User's global Antigravity CLI directory (`~/.gemini/antigravity-cli/`)
    """
    config_override = os.environ.get("ANTIGRAVITY_CONFIG_DIR", "").strip()
    if config_override:
        base = Path(config_override).expanduser()
        return [base / name for name in _OAUTH_TOKEN_FILENAMES]

    root = home_dir if home_dir is not None else Path.home()
    hermes_gemini_dir = resolve_hermes_antigravity_home(root) / ".gemini" / "antigravity-cli"
    user_gemini_dir = root / ".gemini" / "antigravity-cli"

    candidates: list[Path] = []
    for base_dir in (hermes_gemini_dir, user_gemini_dir):
        for name in _OAUTH_TOKEN_FILENAMES:
            candidates.append(base_dir / name)
    return candidates


def _find_existing_oauth_token(home_dir: Path | None = None) -> Path | None:
    """Return the first existing, non-empty OAuth token file, or None."""
    for candidate in _resolve_oauth_token_candidates(home_dir):
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def check_antigravity_token_file_evidence() -> tuple[bool, str]:
    """Fast subprocess-free check for an OAuth token file on disk.

    Used by ``ProviderProfile.external_auth_evidence()`` so ``hermes auth status``
    and Desktop pickers can verify file-backed OAuth immediately without spawning
    a child process.
    """
    token_path = _find_existing_oauth_token()
    if token_path is not None:
        return True, f"OAuth token file ({token_path})"
    return False, ""


def _check_macos_keychain_auth() -> bool:
    """Check macOS Login Keychain for `gemini`/`antigravity` without reading the secret.

    Calling `/usr/bin/security find-generic-password` without `-w` or `-g`
    checks item existence and metadata only — the secret payload is never printed.
    """
    security_bin = "/usr/bin/security"
    if not os.path.isfile(security_bin):
        security_bin = shutil.which("security") or ""
    if not security_bin:
        return False
    try:
        res = subprocess.run(
            [
                security_bin,
                "find-generic-password",
                "-s",
                _KEYRING_SERVICE,
                "-a",
                _KEYRING_ACCOUNT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("macOS keychain probe failed: %s", exc)
        return False


def _check_windows_credential_manager_auth() -> bool:
    """Check Windows Credential Manager for `gemini:antigravity` without reading the secret.

    `cmdkey /list:<target>` lists only metadata (Target, Type, User) and never
    outputs the credential blob.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    cmdkey_candidate = os.path.join(system_root, "System32", "cmdkey.exe")
    cmdkey_bin = cmdkey_candidate if os.path.isfile(cmdkey_candidate) else (shutil.which("cmdkey") or "")
    if not cmdkey_bin:
        return False

    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        res = subprocess.run(
            [cmdkey_bin, f"/list:{_WINDOWS_CREDENTIAL_TARGET}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
            check=False,
            **popen_kwargs,
        )
        if res.returncode != 0:
            return False
        stdout_lower = (res.stdout or "").lower()
        return _WINDOWS_CREDENTIAL_TARGET.lower() in stdout_lower
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Windows Credential Manager probe failed: %s", exc)
        return False


def _check_linux_secret_service_auth() -> bool:
    """Check Linux FreeDesktop Secret Service for `service=gemini`, `username=antigravity`.

    Zero-exfiltration guarantees:
    1. `secret-tool search` prints secret values to `stdout` and item metadata
       (`[/org/freedesktop/secrets/...]`) to `stderr`. We bind `stdout=subprocess.DEVNULL`
       so the kernel discards the secret before it ever reaches Python memory,
       and inspect only `stderr`.
    2. If `secret-tool` is not installed, we fall back to `secretstorage` over D-Bus
       and inspect only `item.get_attributes()`, never `item.get_secret()`.
    """
    secret_tool_bin = "/usr/bin/secret-tool"
    if not os.path.isfile(secret_tool_bin):
        secret_tool_bin = shutil.which("secret-tool") or ""

    if secret_tool_bin:
        try:
            res = subprocess.run(
                [
                    secret_tool_bin,
                    "search",
                    "--all",
                    "service",
                    _KEYRING_SERVICE,
                    "username",
                    _KEYRING_ACCOUNT,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                 stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            if res.returncode == 0 and "[/org/freedesktop/secrets/" in (res.stderr or ""):
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Linux secret-tool probe failed: %s", exc)

    try:
        import contextlib
        import secretstorage  # type: ignore[import-not-found]

        with contextlib.closing(secretstorage.dbus_init()) as connection:
            collection = secretstorage.get_default_collection(connection)
            items = collection.search_items({
                "service": _KEYRING_SERVICE,
                "username": _KEYRING_ACCOUNT,
            })
            for item in items:
                attrs = item.get_attributes()
                if (
                    attrs.get("service") == _KEYRING_SERVICE
                    and attrs.get("username") == _KEYRING_ACCOUNT
                ):
                    return True
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Linux secretstorage D-Bus probe failed: %s", exc)

    return False


def _check_os_keyring_auth() -> bool:
    """Platform-dispatched check for Antigravity OAuth credentials in the OS keyring."""
    if sys.platform == "darwin":
        return _check_macos_keychain_auth()
    if sys.platform == "win32":
        return _check_windows_credential_manager_auth()
    if sys.platform.startswith("linux"):
        return _check_linux_secret_service_auth()
    return False


def check_antigravity_auth() -> bool:
    """Verify whether Antigravity OAuth credentials exist in token files or OS keyring."""
    if _find_existing_oauth_token() is not None:
        return True
    return _check_os_keyring_auth()


def describe_antigravity_auth_source() -> str:
    """Return a human-readable description of where Antigravity OAuth credentials live, or ''."""
    token_path = _find_existing_oauth_token()
    if token_path is not None:
        hermes_home = resolve_hermes_antigravity_home()
        try:
            if token_path.is_relative_to(hermes_home):
                return f"Hermes isolated OAuth token file ({token_path})"
        except ValueError:
            pass
        return f"OAuth token file ({token_path})"
    if sys.platform == "darwin" and _check_macos_keychain_auth():
        return f"macOS Login Keychain (service={_KEYRING_SERVICE}, account={_KEYRING_ACCOUNT})"
    if sys.platform == "win32" and _check_windows_credential_manager_auth():
        return f"Windows Credential Manager (target={_WINDOWS_CREDENTIAL_TARGET})"
    if sys.platform.startswith("linux") and _check_linux_secret_service_auth():
        return f"Linux Secret Service keyring (service={_KEYRING_SERVICE}, username={_KEYRING_ACCOUNT})"
    return ""


def _is_usable_binary(path: str | Path) -> bool:
    """Return True only when ``path`` is a regular file (never a directory)."""
    try:
        p = Path(path)
        return p.is_file()
    except OSError:
        return False


def resolve_antigravity_binary() -> str:
    """Locate the `agy` CLI executable across environment overrides, PATH, and standard locations."""
    for env_key in ("ANTIGRAVITY_BINARY", "AGY_CLI_PATH"):
        env_override = os.environ.get(env_key, "").strip()
        if env_override and _is_usable_binary(env_override):
            return env_override

    which_agy = shutil.which("agy")
    if which_agy and _is_usable_binary(which_agy):
        return which_agy

    homes = _posix_account_homes()
    candidates: list[Path] = []
    for h in homes:
        candidates.extend([
            h / ".local" / "bin" / "agy",
            h / ".antigravity" / "bin" / "agy",
        ])

    candidates.extend([
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
        Path("/usr/share/antigravity/bin/agy"),
        Path("/opt/antigravity/bin/agy"),
        Path("/opt/Antigravity/bin/agy"),
        Path("/opt/homebrew/bin/agy"),
    ])

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            base = Path(local_app_data)
            candidates.extend([
                base / "agy" / "bin" / "agy.exe",
                base / "agy" / "bin" / "agy.cmd",
                base / "Programs" / "Antigravity" / "bin" / "agy.cmd",
                base / "Programs" / "Antigravity" / "bin" / "agy.exe",
                base / "Programs" / "Antigravity" / "agy.exe",
            ])
        for h in homes:
            candidates.extend([
                h / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
                h / "AppData" / "Local" / "agy" / "bin" / "agy.cmd",
                h / "AppData" / "Local" / "Programs" / "Antigravity" / "bin" / "agy.exe",
                h / ".antigravity" / "bin" / "agy.cmd",
                h / ".antigravity" / "bin" / "agy.exe",
            ])

    for candidate in candidates:
        if _is_usable_binary(candidate):
            return str(candidate)

    return ""


def _same_underlying_file(path_a: Path, path_b: Path) -> bool:
    """Return True if two paths resolve to the same physical file."""
    try:
        return os.path.samefile(path_a, path_b)
    except OSError:
        try:
            return path_a.resolve() == path_b.resolve()
        except OSError:
            return False


def _link_macos_keychains(real_home: Path, isolated_home: Path) -> None:
    """Ensure `<isolated_home>/Library/Keychains` links to `<real_home>/Library/Keychains` on macOS.

    macOS `SecItemCopyMatching` resolves login.keychain-db via `$HOME/Library/Keychains`.
    When `HOME` is redirected to `isolated_home`, linking `Library/Keychains` preserves
    Keychain access without exposing the rest of the user's real `$HOME`.
    """
    real_keychains = real_home / "Library" / "Keychains"
    try:
        if not real_keychains.is_dir():
            return
    except OSError:
        return

    isolated_library = isolated_home / "Library"
    isolated_keychains = isolated_library / "Keychains"

    try:
        isolated_library.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("Failed to create isolated Library dir %s: %s", isolated_library, exc)
        return

    try:
        if os.path.islink(isolated_keychains):
            if _same_underlying_file(isolated_keychains, real_keychains):
                return
            isolated_keychains.unlink()
        elif isolated_keychains.exists():
            # Real directory or file already present — never overwrite user data.
            return
    except OSError as exc:
        logger.debug("Failed inspecting isolated Keychains path %s: %s", isolated_keychains, exc)
        return

    try:
        os.symlink(real_keychains, isolated_keychains, target_is_directory=True)
    except OSError as exc:
        logger.debug("Could not symlink macOS Keychains %s -> %s: %s", real_keychains, isolated_keychains, exc)


def _link_or_copy_token(source: Path, dest: Path) -> None:
    """Link or copy `source` token file to `dest` with `0o600` fallback permissions."""
    try:
        if os.path.abspath(source) == os.path.abspath(dest):
            return
    except OSError:
        return

    if os.path.islink(dest):
        try:
            dest.unlink()
        except OSError:
            pass

    if dest.exists():
        return

    for link_fn in (os.symlink, os.link):
        try:
            link_fn(source, dest)
            return
        except OSError:
            continue

    try:
        shutil.copy2(source, dest)
        os.chmod(dest, 0o600)
    except OSError as exc:
        logger.warning("Failed to copy OAuth token into isolated HOME (%s): %s", dest, exc)


def setup_isolated_home(cwd: str | Path) -> tuple[Path, Path]:
    """Prepare an isolated `HOME` directory for `agy` inside `cwd`.

    1. Creates `<cwd>/home/.gemini/antigravity-cli` with `0o700` permissions.
    2. Links only the active OAuth token file (via symlink -> hardlink -> copy2 0o600),
       preventing `agy` from scanning the user's real `$HOME` while preserving auth.
       Both `jetski-standalone-oauth-token` and `antigravity-oauth-token` slots are
       populated so both `NewCLITokenStorage` and `NewFileTokenStorage` in `agy`
       locate the isolated credential immediately.
    3. On macOS, symlinks `~/Library/Keychains` so Keychain-backed OAuth works.
    4. Creates an empty `<cwd>/home/.gemini/SOUL.md` to prevent global persona injection.

    Returns `(isolated_home, gemini_config_dir)`.
    """
    real_home = Path.home()
    isolated_home = Path(cwd) / "home"
    gemini_root = isolated_home / ".gemini"
    gemini_dir = gemini_root / "antigravity-cli"
    gemini_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(isolated_home, 0o700)
        os.chmod(gemini_root, 0o700)
        os.chmod(gemini_dir, 0o700)
    except OSError:
        pass

    real_token = _find_existing_oauth_token(real_home)
    if real_token is not None:
        for basename in _OAUTH_TOKEN_FILENAMES:
            _link_or_copy_token(real_token, gemini_dir / basename)
        _link_or_copy_token(real_token, gemini_root / "jetski-standalone-oauth-token")

    if sys.platform == "darwin":
        _link_macos_keychains(real_home, isolated_home)

    soul_file = gemini_root / "SOUL.md"
    if not soul_file.exists():
        try:
            soul_file.write_text("", encoding="utf-8")
        except OSError:
            pass

    return isolated_home, gemini_dir


def sync_updated_isolated_tokens(gemini_dir: Path | None, home_dir: Path | None = None) -> None:
    """Persist refreshed OAuth tokens written inside an isolated workspace back to the source token file.

    When `agy` refreshes an expired OAuth `access_token`, Go's file token storage may replace
    a symlink with a regular file (via atomic rename) or update a copied token file (when
    hardlinks/symlinks across filesystems or Windows without Developer Mode fell back to `copy2`).
    Syncing newer non-empty token files back to `_find_existing_oauth_token` ensures refreshed
    tokens survive across worker restarts and temporary directory cleanups.
    """
    if gemini_dir is None:
        return
    source_token = _find_existing_oauth_token(home_dir)
    if source_token is None:
        return
    try:
        source_mtime = source_token.stat().st_mtime
    except OSError:
        return

    gemini_root = gemini_dir.parent
    isolated_candidates = (
        *(gemini_dir / name for name in _OAUTH_TOKEN_FILENAMES),
        gemini_root / "jetski-standalone-oauth-token",
    )
    newest_candidate: Path | None = None
    newest_mtime = source_mtime
    for candidate in isolated_candidates:
        try:
            if os.path.islink(candidate):
                continue
            if not candidate.is_file():
                continue
            if _same_underlying_file(candidate, source_token):
                continue
            st = candidate.stat()
            if st.st_size > 0 and st.st_mtime > newest_mtime:
                newest_mtime = st.st_mtime
                newest_candidate = candidate
        except OSError:
            continue

    if newest_candidate is not None:
        try:
            shutil.copy2(newest_candidate, source_token)
            os.chmod(source_token, 0o600)
        except OSError as exc:
            logger.debug("Could not sync refreshed OAuth token back to %s: %s", source_token, exc)


def _isolated_home_has_token_file(isolated_home: Path) -> bool:
    """Return True if `isolated_home` contains an OAuth token file in `.gemini`."""
    gemini_root = isolated_home / ".gemini"
    gemini_dir = gemini_root / "antigravity-cli"
    for candidate in (
        *(gemini_dir / name for name in _OAUTH_TOKEN_FILENAMES),
        gemini_root / "jetski-standalone-oauth-token",
    ):
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _parse_csv_env_names(raw: str) -> set[str]:
    """Parse comma-separated environment variable names."""
    if not raw or not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def build_clean_child_env(isolated_home: Path, *, force_file_auth: bool = False) -> dict[str, str]:
    """Construct a sanitized environment dictionary for the `agy` child process.

    Modes:
    - Strict allowlist mode (default, or `ANTIGRAVITY_ENV_STRICT=1`):
      Forwards only `_SAFE_ENV_KEYS` + `ANTIGRAVITY_ENV_ALLOWLIST` + `ANTIGRAVITY_ENV_PASSTHROUGH`.
    - Heuristic filter mode (`ANTIGRAVITY_ENV_STRICT=0` / `false` / `no` / `off`):
      Starts from `os.environ` and strips secret-named keys, secret-shaped values,
      loader injection variables, proxy variables, and (when keyring-backed without
      a token file) SSH agent socket variables.
    """
    strict_raw = os.environ.get("ANTIGRAVITY_ENV_STRICT", "1").strip().lower()
    strict_mode = strict_raw not in ("0", "false", "no", "off")

    extra_allowed = (
        _parse_csv_env_names(os.environ.get("ANTIGRAVITY_ENV_ALLOWLIST", ""))
        | _parse_csv_env_names(os.environ.get("ANTIGRAVITY_ENV_PASSTHROUGH", ""))
    )

    clean_env: dict[str, str] = {}

    if strict_mode:
        allowed_keys = _SAFE_ENV_KEYS | extra_allowed
        allowed_upper = {k.upper(): k for k in allowed_keys}
        for key, val in os.environ.items():
            if not isinstance(val, str):
                continue
            if key in allowed_keys or key.upper() in allowed_upper:
                if key.upper() in _DANGEROUS_LOADER_ENV_KEYS and key not in extra_allowed:
                    continue
                clean_env[key] = val
            elif key.startswith("LC_") and not _SECRET_VALUE_PATTERNS.search(val.strip()):
                clean_env[key] = val
    else:
        has_token_file = _find_existing_oauth_token() is not None
        for key, val in os.environ.items():
            if not isinstance(val, str):
                continue
            if key in extra_allowed:
                clean_env[key] = val
                continue
            key_upper = key.upper()
            if key_upper in _DANGEROUS_LOADER_ENV_KEYS:
                continue
            if key in _PROXY_ENV_KEYS or key_upper in _PROXY_ENV_KEYS:
                continue
            if not has_token_file and key_upper in _SSH_AGENT_ENV_KEYS:
                continue
            if key_upper.startswith("ANTIGRAVITY_") and key_upper != "ANTIGRAVITY_CONFIG_DIR":
                continue
            if key_upper not in _SAFE_ENV_KEYS:
                if _SECRET_KEY_PATTERNS.search(key):
                    continue
                if _SECRET_VALUE_PATTERNS.search(val.strip()):
                    continue
            clean_env[key] = val

    # Explicit proxy opt-in via ANTIGRAVITY_PROXY
    explicit_proxy = os.environ.get("ANTIGRAVITY_PROXY", "").strip()
    if explicit_proxy:
        clean_env["HTTPS_PROXY"] = explicit_proxy
        clean_env["HTTP_PROXY"] = explicit_proxy
        clean_env["ALL_PROXY"] = explicit_proxy
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        if no_proxy:
            clean_env["NO_PROXY"] = no_proxy

    # Always pin HOME, USERPROFILE (used by Go os.UserHomeDir() on Windows), and XDG_CONFIG_HOME
    clean_env["HOME"] = str(isolated_home)
    clean_env["USERPROFILE"] = str(isolated_home)
    clean_env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")

    # On Linux/POSIX (especially under systemd with a minimal PATH), ensure standard binary dirs exist in PATH
    if sys.platform != "win32":
        raw_path = clean_env.get("PATH", "")
        path_parts = [p for p in raw_path.split(os.pathsep) if p] if raw_path else []
        fallback_dirs = [str(h / ".local" / "bin") for h in _posix_account_homes()]
        fallback_dirs.extend(["/usr/local/bin", "/usr/bin", "/bin"])
        for fallback_dir in fallback_dirs:
            if fallback_dir not in path_parts:
                path_parts.append(fallback_dir)
        clean_env["PATH"] = os.pathsep.join(path_parts)

    # When a file-based OAuth token is present in `isolated_home` (or during `hermes auth add`),
    # set SSH_CONNECTION so `agy`'s CompositeTokenStorage uses file-based token storage inside
    # `isolated_home` instead of reading/overwriting the host's global OS keyring (`gemini:antigravity`).
    if force_file_auth or _isolated_home_has_token_file(isolated_home):
        clean_env["SSH_CONNECTION"] = _KEYRING_BYPASS_SSH_MARKER

    return clean_env


def run_isolated_antigravity_login(binary: str) -> int:
    """Run `agy acp --login` inside the Hermes-isolated Antigravity home directory.

    By setting `HOME`, `USERPROFILE`, and `SSH_CONNECTION` on the login child process,
    `agy` writes the OAuth token exclusively into `<hermes_home>/antigravity-oauth/.gemini/`
    without overwriting the user's primary `agy` CLI account in the global OS keyring.
    """
    hermes_home = resolve_hermes_antigravity_home()
    gemini_root = hermes_home / ".gemini"
    gemini_dir = gemini_root / "antigravity-cli"
    gemini_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(hermes_home, 0o700)
        os.chmod(gemini_root, 0o700)
        os.chmod(gemini_dir, 0o700)
    except OSError:
        pass

    soul_file = gemini_root / "SOUL.md"
    if not soul_file.exists():
        try:
            soul_file.write_text("", encoding="utf-8")
        except OSError:
            pass

    login_env = build_clean_child_env(hermes_home, force_file_auth=True)
    res = subprocess.run([binary, "acp", "--login"], cwd=str(hermes_home), env=login_env, check=False)

    # Synchronize token file variants inside hermes_home so both NewCLITokenStorage
    # (`antigravity-oauth-token`) and NewFileTokenStorage (`jetski-standalone-oauth-token`) find it.
    candidate_paths = [
        *(gemini_dir / name for name in _OAUTH_TOKEN_FILENAMES),
        gemini_root / "jetski-standalone-oauth-token",
    ]
    newest_token: Path | None = None
    newest_mtime: float = -1.0
    for path in candidate_paths:
        try:
            if path.is_file() and path.stat().st_size > 0:
                mtime = path.stat().st_mtime
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_token = path
        except OSError:
            continue

    if newest_token is not None:
        for target in candidate_paths:
            if target == newest_token:
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                continue
            try:
                if os.path.islink(target):
                    target.unlink()
                shutil.copy2(newest_token, target)
                os.chmod(target, 0o600)
            except OSError:
                pass

    return res.returncode


def remove_isolated_antigravity_auth() -> list[Path]:
    """Remove Hermes-isolated Antigravity OAuth token files without touching the global `agy` account."""
    hermes_home = resolve_hermes_antigravity_home()
    gemini_root = hermes_home / ".gemini"
    gemini_dir = gemini_root / "antigravity-cli"
    removed: list[Path] = []
    for target in (
        *(gemini_dir / name for name in _OAUTH_TOKEN_FILENAMES),
        gemini_root / "jetski-standalone-oauth-token",
    ):
        try:
            if target.exists() or os.path.islink(target):
                target.unlink()
                removed.append(target)
        except OSError:
            continue
    return removed


def popen_isolated_session(
    cmd: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    """Launch `cmd` in a new process group / session with UTF-8 line-buffered pipes."""
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **popen_kwargs)


def terminate_process_group(proc: subprocess.Popen[Any] | None, timeout: float = 3.0) -> None:
    """Gracefully terminate a child process and its process group, escalating to kill."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    pid = getattr(proc, "pid", None)
    if sys.platform == "win32":
        try:
            proc.terminate()
        except OSError:
            pass
    else:
        if isinstance(pid, int) and pid > 0:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass

    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return

    if sys.platform == "win32":
        try:
            proc.kill()
        except OSError:
            pass
    else:
        if isinstance(pid, int) and pid > 0:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            try:
                proc.kill()
            except OSError:
                pass

    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass


class StderrDrainer:
    """Background daemon thread that drains a child's stderr pipe into a bounded ring buffer."""

    def __init__(self, stream: IO[str] | None, *, max_lines: int = 100) -> None:
        self._stream = stream
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain_loop,
                name="agy-stderr-drainer",
                daemon=True,
            )
            self._thread.start()

    def _drain_loop(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\r\n")
                if stripped:
                    with self._lock:
                        self._lines.append(stripped)
        except (OSError, ValueError):
            pass

    def get_output(self) -> str:
        """Return all buffered stderr lines joined by newline."""
        with self._lock:
            return "\n".join(self._lines)


def watch_log_for_errors(
    gemini_dir: Path,
    stop_event: threading.Event,
    error_holder: list[str],
    on_error_kill: Callable[[], None] | None = None,
) -> None:
    """Watch `<gemini_dir>/{log,logs}/*.log` and `cli.log` for fatal 429 / quota errors."""
    log_dirs = (gemini_dir / "log", gemini_dir / "logs")
    top_cli_log = gemini_dir / "cli.log"
    start_time = time.time() - 2.0
    file_offsets: dict[Path, int] = {}

    while not stop_event.is_set():
        try:
            candidate_logs: list[Path] = []
            if top_cli_log.is_file():
                candidate_logs.append(top_cli_log)
            for ldir in log_dirs:
                if ldir.is_dir():
                    candidate_logs.extend(ldir.glob("*.log"))

            for log_file in candidate_logs:
                try:
                    stat_res = log_file.stat()
                    if stat_res.st_mtime < start_time:
                        continue
                    offset = file_offsets.get(log_file, 0)
                    if stat_res.st_size <= offset:
                        continue
                    with log_file.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                        file_offsets[log_file] = fh.tell()
                    for line in chunk.splitlines():
                        if _ERROR_LOG_PATTERNS.search(line):
                            error_holder.append(line.strip())
                            if on_error_kill is not None:
                                try:
                                    on_error_kill()
                                except Exception:
                                    pass
                            return
                except OSError:
                    continue
        except OSError:
            pass
        stop_event.wait(0.25)
