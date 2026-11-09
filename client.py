"""OpenAI-compatible client backed by Google Antigravity (`agy`) with persistent worker pooling."""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from hermes_cli.auth_constants import AuthError

try:
    from . import models as _models
    from .debug import dump_prompt_if_enabled
    from .models import (
        DEFAULT_MODEL,
        EFFORT_LEVELS,
        MODEL_ALIASES,
        get_model_catalog,
        parse_model_and_effort,
        resolve_model_family_alias,
    )
    from .process import (
        StderrDrainer,
        build_clean_child_env,
        check_antigravity_auth,
        popen_isolated_session,
        resolve_antigravity_binary,
        setup_isolated_home,
        terminate_process_group,
    )
    from .prompt import build_prompt_from_messages, format_delta_messages
    from .stream import AntigravityStream, _AwaitableCompletion, collect_stream_completion
except ImportError:
    import models as _models  # type: ignore[no-redef]
    from debug import dump_prompt_if_enabled  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        DEFAULT_MODEL,
        EFFORT_LEVELS,
        MODEL_ALIASES,
        get_model_catalog,
        parse_model_and_effort,
        resolve_model_family_alias,
    )
    from process import (  # type: ignore[no-redef]
        StderrDrainer,
        build_clean_child_env,
        check_antigravity_auth,
        popen_isolated_session,
        resolve_antigravity_binary,
        setup_isolated_home,
        terminate_process_group,
    )
    from prompt import build_prompt_from_messages, format_delta_messages  # type: ignore[no-redef]
    from stream import AntigravityStream, _AwaitableCompletion, collect_stream_completion  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
_RMTREE_RETRY_DELAYS = (0.05, 0.15, 0.35)


def _chmod_writable_if_owned_unlinked(target: str | Path) -> None:
    """Make `target` owner-writable only if it is NOT a symlink and NOT a multi-hardlink file."""
    try:
        st = os.lstat(target)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        return
    try:
        os.chmod(target, st.st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _force_rmtree(root_path: str | Path) -> None:
    """Remove temporary workspace directory safely without following symlinks or widening linked tokens."""
    path_str = str(root_path)
    if not os.path.exists(path_str) and not os.path.islink(path_str):
        return

    def _on_rm_error(func: Any, failed_path: str, exc_info: Any) -> None:
        _chmod_writable_if_owned_unlinked(failed_path)
        try:
            func(failed_path)
        except OSError:
            pass

    for attempt in range(len(_RMTREE_RETRY_DELAYS) + 1):
        try:
            shutil.rmtree(path_str, ignore_errors=False, onerror=_on_rm_error)
        except OSError:
            pass

        if not os.path.exists(path_str) and not os.path.islink(path_str):
            return

        if attempt < len(_RMTREE_RETRY_DELAYS):
            time.sleep(_RMTREE_RETRY_DELAYS[attempt])
            try:
                for dirpath, dirnames, filenames in os.walk(path_str, topdown=False, followlinks=False):
                    for fname in filenames:
                        _chmod_writable_if_owned_unlinked(os.path.join(dirpath, fname))
                    for dname in dirnames:
                        _chmod_writable_if_owned_unlinked(os.path.join(dirpath, dname))
                _chmod_writable_if_owned_unlinked(path_str)
            except OSError:
                pass

    shutil.rmtree(path_str, ignore_errors=True)


class _CompletionsAdapter:
    """OpenAI `client.chat.completions` surface."""

    def __init__(self, client: AntigravityClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)

    def cancel(self) -> None:
        self._client.cancel()


class _ChatAdapter:
    """OpenAI `client.chat` surface."""

    def __init__(self, client: AntigravityClient) -> None:
        self.completions = _CompletionsAdapter(client)


class _ResponsesAdapter:
    """OpenAI `client.responses` compatibility surface."""

    def __init__(self, client: AntigravityClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        mapped = dict(kwargs)
        if "input" in mapped and "messages" not in mapped:
            raw_input = mapped.pop("input")
            if isinstance(raw_input, str):
                mapped["messages"] = [{"role": "user", "content": raw_input}]
            elif isinstance(raw_input, list):
                mapped["messages"] = raw_input
        return self._client._create_chat_completion(**mapped)

    def cancel(self) -> None:
        self._client.cancel()


class AntigravityClient:
    """Persistent + oneshot subprocess client for Google Antigravity (`agy`)."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        binary: str = "",
        cwd: str | Path | None = None,
        timeout: float | None = None,
    ) -> None:
        self._binary = (binary or "").strip() or resolve_antigravity_binary()
        self._timeout = timeout
        self._owns_temp_cwd = cwd is None
        if cwd is None:
            self._cwd = Path(tempfile.mkdtemp(prefix="hermes_agy_ws_"))
        else:
            self._cwd = Path(cwd)
            self._cwd.mkdir(parents=True, exist_ok=True)

        self.chat = _ChatAdapter(self)
        self.responses = _ResponsesAdapter(self)
        self.base_url = "antigravity://local-oauth-cli"
        self.api_key = "antigravity-oauth"

        self._worker_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._worker_proc: subprocess.Popen[str] | None = None
        self._worker_stderr: StderrDrainer | None = None
        self._worker_model: str | None = None
        self._worker_effort: str | None = None
        self._worker_gemini_dir: Path | None = None
        self._worker_usage: dict[str, int] = {}
        self._idle_timer: threading.Timer | None = None

        self._conversation_id: str | None = None
        self._sent_message_count: int = 0
        self._last_messages_prefix: list[tuple[str, str]] = []

        self._streams_lock = threading.Lock()
        self._active_streams: set[AntigravityStream] = set()
        self._closed = False

        weak_self = weakref.ref(self)

        def _atexit_cleanup() -> None:
            inst = weak_self()
            if inst is not None:
                inst.close()

        self._atexit_hook = _atexit_cleanup
        atexit.register(self._atexit_hook)

    @staticmethod
    def _normalize_reasoning_effort(raw_effort: Any) -> str | None:
        """Map Hermes/OpenAI reasoning_effort strings onto `agy` effort levels (`low`, `medium`, `high`)."""
        if not isinstance(raw_effort, str):
            return None
        cleaned = raw_effort.strip().lower()
        if not cleaned or cleaned in ("auto", "default"):
            return None
        if cleaned in ("none", "minimal", "low"):
            return "low"
        if cleaned == "medium":
            return "medium"
        if cleaned in ("high", "xhigh", "max"):
            return "high"
        return None

    def _resolve_model_and_effort(
        self,
        raw_model: str | None,
        raw_effort: Any = None,
    ) -> tuple[str, str | None]:
        """Resolve `(effective_model_id, effective_cli_effort_flag)` for `agy --model`."""
        _, families = _models.get_model_catalog(self._binary)
        base_model, suffix_effort = _models.parse_model_and_effort(
            raw_model or DEFAULT_MODEL,
            self._binary,
            families=families,
        )
        base_model = _models.resolve_model_family_alias(base_model, families)

        explicit_effort = self._normalize_reasoning_effort(raw_effort)
        desired_effort = explicit_effort or suffix_effort

        pass_effort_flag = os.environ.get("ANTIGRAVITY_PASS_EFFORT_FLAG", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        supported = families.get(base_model)
        if supported is not None:
            if len(supported) == 0:
                # Effortless model family (e.g., claude-sonnet-4-6, claude-opus-4-6-thinking)
                return base_model, None
            if len(supported) == 1:
                only_eff = supported[0]
                # Single-effort model family (e.g., gpt-oss-120b -> gpt-oss-120b-medium)
                return f"{base_model}-{only_eff}", (only_eff if pass_effort_flag else None)

            chosen = desired_effort or "high"
            if chosen not in supported:
                if chosen == "medium" and "high" in supported:
                    chosen = "high"
                elif "high" in supported:
                    chosen = "high"
                else:
                    chosen = supported[-1]
            return f"{base_model}-{chosen}", (chosen if pass_effort_flag else None)

        # Unknown custom model outside the discovered catalog
        if desired_effort:
            if not base_model.endswith(f"-{desired_effort}"):
                return f"{base_model}-{desired_effort}", desired_effort
            return base_model, desired_effort
        return base_model, None

    @staticmethod
    def _extract_reasoning_effort(kwargs: dict[str, Any]) -> Any:
        """Extract `reasoning_effort` from top-level kwargs, `extra_body`, or `reasoning` dict."""
        if kwargs.get("reasoning_effort") is not None:
            return kwargs.get("reasoning_effort")
        extra_body = kwargs.get("extra_body")
        if isinstance(extra_body, dict):
            if extra_body.get("reasoning_effort") is not None:
                return extra_body.get("reasoning_effort")
            reasoning_obj = extra_body.get("reasoning")
            if isinstance(reasoning_obj, dict) and reasoning_obj.get("effort") is not None:
                return reasoning_obj.get("effort")
        reasoning_top = kwargs.get("reasoning")
        if isinstance(reasoning_top, dict) and reasoning_top.get("effort") is not None:
            return reasoning_top.get("effort")
        return None

    @staticmethod
    def _message_fingerprints(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """Create compact `(role, content_hash_or_text)` fingerprints to detect history rewrites."""
        out: list[tuple[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            out.append((role, content[:512]))
        return out

    def _compute_turn_prompt(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str, str | None, int, list[tuple[str, str]]]:
        """Determine whether we can resume `self._conversation_id` with a delta prompt or start fresh."""
        fingerprints = self._message_fingerprints(messages)
        with self._state_lock:
            conv_id = self._conversation_id
            sent_count = self._sent_message_count
            prev_prefix = list(self._last_messages_prefix)

        if (
            conv_id
            and sent_count > 0
            and len(messages) > sent_count
            and fingerprints[:sent_count] == prev_prefix
        ):
            delta_msgs = messages[sent_count:]
            delta_prompt = format_delta_messages(delta_msgs)
            if delta_prompt:
                return delta_prompt, conv_id, len(messages), fingerprints

        full_prompt = build_prompt_from_messages(messages, tools)
        return full_prompt, None, len(messages), fingerprints

    def _record_conversation_id(self, conv_id: str) -> None:
        with self._state_lock:
            self._conversation_id = conv_id

    def _cancel_idle_timer(self) -> None:
        with self._state_lock:
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def _reset_idle_timer(self) -> None:
        self._cancel_idle_timer()
        raw_timeout = os.environ.get("ANTIGRAVITY_WORKER_IDLE_TIMEOUT", "").strip()
        try:
            idle_seconds = float(raw_timeout) if raw_timeout else _DEFAULT_IDLE_TIMEOUT_SECONDS
        except ValueError:
            idle_seconds = _DEFAULT_IDLE_TIMEOUT_SECONDS
        if idle_seconds <= 0:
            return

        timer = threading.Timer(idle_seconds, self._on_idle_timeout)
        timer.daemon = True
        with self._state_lock:
            if not self._closed:
                self._idle_timer = timer
                timer.start()

    def _on_idle_timeout(self) -> None:
        if self._worker_lock.acquire(blocking=False):
            try:
                self._kill_worker()
            finally:
                self._worker_lock.release()

    def _kill_worker(self) -> None:
        self._cancel_idle_timer()
        with self._state_lock:
            proc = self._worker_proc
            self._worker_proc = None
            self._worker_stderr = None
            self._worker_model = None
            self._worker_effort = None
            self._worker_usage = {}
        if proc is not None:
            terminate_process_group(proc)

    def _ensure_worker(self, model: str, effort: str | None) -> tuple[subprocess.Popen[str], StderrDrainer, Path]:
        """Return an alive persistent worker for `(model, effort)`, spawning a fresh one if needed."""
        with self._state_lock:
            proc = self._worker_proc
            drainer = self._worker_stderr
            g_dir = self._worker_gemini_dir
            same_config = (self._worker_model == model and self._worker_effort == effort)

        if proc is not None and drainer is not None and g_dir is not None and same_config and proc.poll() is None:
            return proc, drainer, g_dir

        self._kill_worker()

        isolated_home, gemini_dir = setup_isolated_home(self._cwd)
        clean_env = build_clean_child_env(isolated_home)

        cmd = [
            self._binary,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        if effort:
            cmd.extend(["--effort", effort])

        new_proc = popen_isolated_session(cmd, cwd=self._cwd, env=clean_env)
        new_drainer = StderrDrainer(new_proc.stderr)

        with self._state_lock:
            self._worker_proc = new_proc
            self._worker_stderr = new_drainer
            self._worker_model = model
            self._worker_effort = effort
            self._worker_gemini_dir = gemini_dir
            self._worker_usage = {}

        return new_proc, new_drainer, gemini_dir

    def _spawn_oneshot(
        self,
        *,
        prompt: str,
        model: str,
        effort: str | None,
        conversation_id: str | None = None,
    ) -> tuple[subprocess.Popen[str], StderrDrainer, Path]:
        """Spawn a oneshot `agy` process when the persistent worker is busy or resuming `--conversation`."""
        isolated_home, gemini_dir = setup_isolated_home(self._cwd)
        clean_env = build_clean_child_env(isolated_home)

        cmd = [
            self._binary,
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        proc = popen_isolated_session(cmd, cwd=self._cwd, env=clean_env)
        drainer = StderrDrainer(proc.stderr)

        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
                if not prompt.endswith("\n"):
                    proc.stdin.write("\n")
                proc.stdin.flush()
            except OSError as exc:
                terminate_process_group(proc)
                raise RuntimeError(f"Failed writing prompt to Antigravity oneshot stdin: {exc}") from exc
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        return proc, drainer, gemini_dir

    def _register_stream(self, stream: AntigravityStream) -> None:
        with self._streams_lock:
            self._active_streams.add(stream)

    def _unregister_stream(self, stream: AntigravityStream) -> None:
        with self._streams_lock:
            self._active_streams.discard(stream)

    def _update_worker_usage(self, cumulative: dict[str, int]) -> None:
        with self._state_lock:
            self._worker_usage = dict(cumulative)
        self._reset_idle_timer()

    def _create_chat_completion(self, **kwargs: Any) -> Any:
        """Execute a streaming or non-streaming chat completion turn via `agy`."""
        if not self._binary or not os.path.isfile(self._binary):
            raise AuthError(
                "Google Antigravity CLI (`agy`) was not found. Install Antigravity from "
                "https://antigravity.google/download or set ANTIGRAVITY_BINARY=/path/to/agy.",
                provider="antigravity-oauth",
                relogin_required=False,
            )

        if not check_antigravity_auth():
            raise AuthError(
                "No Google Antigravity OAuth credentials found. Sign in via `hermes auth add antigravity-oauth` "
                "or run `agy acp --login` in your terminal.",
                provider="antigravity-oauth",
                relogin_required=True,
            )

        messages = list(kwargs.get("messages") or [])
        tools = kwargs.get("tools")
        stream_requested = bool(kwargs.get("stream", False))
        raw_model = kwargs.get("model") or DEFAULT_MODEL
        raw_effort = self._extract_reasoning_effort(kwargs)

        effective_model, effective_effort = self._resolve_model_and_effort(raw_model, raw_effort)
        prompt, resume_conv_id, msg_count, msg_fingerprints = self._compute_turn_prompt(messages, tools)

        # Acquire persistent worker lock if free and not resuming via `--conversation` CLI flag
        acquired_worker_lock = False
        if resume_conv_id is None:
            acquired_worker_lock = self._worker_lock.acquire(blocking=False)

        lock_transferred_to_stream = False
        try:
            if acquired_worker_lock:
                self._cancel_idle_timer()
                dump_prompt_if_enabled(
                    prompt=prompt,
                    model=effective_model,
                    mode="worker",
                    conversation_id=None,
                    tools=tools,
                )
                payload_line = json.dumps(
                    {"event": "user", "message": {"content": prompt}},
                    ensure_ascii=False,
                )

                # Try sending to existing worker; if broken pipe on a reused worker, respawn once
                for attempt in range(2):
                    proc, drainer, gemini_dir = self._ensure_worker(effective_model, effective_effort)
                    try:
                        if proc.stdin is None:
                            raise BrokenPipeError("Worker stdin is None")
                        proc.stdin.write(payload_line + "\n")
                        proc.stdin.flush()
                        break
                    except OSError:
                        self._kill_worker()
                        if attempt == 1:
                            raise

                with self._state_lock:
                    prev_usage = dict(self._worker_usage)

                stream_iter = AntigravityStream(
                    proc,
                    effective_model,
                    gemini_dir,
                    is_persistent_worker=True,
                    stderr_drainer=drainer,
                    worker_lock=self._worker_lock,
                    prev_worker_usage=prev_usage,
                    on_turn_complete=self._update_worker_usage,
                    on_worker_dead=self._kill_worker,
                    on_conversation_id=self._record_conversation_id,
                    on_stream_done=self._unregister_stream,
                )
                lock_transferred_to_stream = True
            else:
                mode_label = "oneshot_resume" if resume_conv_id else "oneshot_concurrent"
                dump_prompt_if_enabled(
                    prompt=prompt,
                    model=effective_model,
                    mode=mode_label,
                    conversation_id=resume_conv_id,
                    tools=tools,
                )
                proc, drainer, gemini_dir = self._spawn_oneshot(
                    prompt=prompt,
                    model=effective_model,
                    effort=effective_effort,
                    conversation_id=resume_conv_id,
                )
                stream_iter = AntigravityStream(
                    proc,
                    effective_model,
                    gemini_dir,
                    is_persistent_worker=False,
                    stderr_drainer=drainer,
                    on_conversation_id=self._record_conversation_id,
                    on_stream_done=self._unregister_stream,
                )

            self._register_stream(stream_iter)
            with self._state_lock:
                self._sent_message_count = msg_count
                self._last_messages_prefix = msg_fingerprints

            if stream_requested:
                return stream_iter

            completion = collect_stream_completion(stream_iter, effective_model)
            return _AwaitableCompletion(completion)

        finally:
            if acquired_worker_lock and not lock_transferred_to_stream:
                try:
                    self._worker_lock.release()
                except RuntimeError:
                    pass

    def cancel(self) -> None:
        """Cross-thread interrupt hook called by Hermes `interrupt_active_client()`.

        Cancels all active streams and terminates the persistent worker subprocess
        immediately so blocked turn threads wake up cleanly.
        """
        with self._streams_lock:
            streams = list(self._active_streams)
        for stream in streams:
            try:
                stream.cancel()
            except Exception:
                pass
        self._kill_worker()

    def close(self) -> None:
        """Shut down persistent worker, cancel active streams, and remove temporary workspace."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

        try:
            atexit.unregister(self._atexit_hook)
        except Exception:
            pass

        self.cancel()

        if self._owns_temp_cwd:
            _force_rmtree(self._cwd)
