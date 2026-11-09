"""Progressive OpenAI-compatible stream parser and async compatibility wrapper for `agy`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import subprocess
import threading
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

try:
    from .delivery import warn_if_short_delivery
    from .process import (
        StderrDrainer,
        terminate_process_group,
        watch_log_for_errors,
    )
except ImportError:
    from delivery import warn_if_short_delivery  # type: ignore[no-redef]
    from process import (  # type: ignore[no-redef]
        StderrDrainer,
        terminate_process_group,
        watch_log_for_errors,
    )

logger = logging.getLogger(__name__)

_TOOL_CALL_OPEN = "<tool_call>"
_TOOL_CALL_CLOSE = "</tool_call>"


def make_stream_chunk(
    model: str,
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    """Create an OpenAI-compatible streaming `ChatCompletionChunk` (`SimpleNamespace`)."""
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(
        index=0,
        delta=delta,
        finish_reason=finish_reason,
    )
    return SimpleNamespace(
        choices=[choice],
        model=model,
        usage=None,
    )


def collect_stream_completion(stream: Iterator[SimpleNamespace], model: str = "") -> SimpleNamespace:
    """Drain a streaming `AntigravityStream` into a single non-streaming OpenAI completion object."""
    resolved_model = model or getattr(stream, "model", "")
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[SimpleNamespace] = []
    finish_reason: str | None = None
    usage: Any = None

    for chunk in stream:
        if getattr(chunk, "model", None):
            resolved_model = chunk.model
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        for choice in getattr(chunk, "choices", None) or ():
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "content", None):
                content_parts.append(str(delta.content))
            rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rc:
                reasoning_parts.append(str(rc))
            for tc in getattr(delta, "tool_calls", None) or ():
                fn = getattr(tc, "function", None)
                tool_calls.append(
                    SimpleNamespace(
                        id=getattr(tc, "id", f"call_{len(tool_calls) + 1}"),
                        type="function",
                        function=SimpleNamespace(
                            name=getattr(fn, "name", "") if fn else "",
                            arguments=getattr(fn, "arguments", "{}") if fn else "{}",
                        ),
                    )
                )

    full_content = "".join(content_parts) or None
    full_reasoning = "".join(reasoning_parts) or None
    if usage is None:
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )

    message = SimpleNamespace(
        role="assistant",
        content=full_content,
        tool_calls=tool_calls if tool_calls else None,
        reasoning=full_reasoning,
        reasoning_content=full_reasoning,
        reasoning_details=None,
    )
    choice = SimpleNamespace(
        index=0,
        message=message,
        finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
    )
    return SimpleNamespace(
        choices=[choice],
        model=resolved_model,
        usage=usage,
    )


def build_stream_tool_call_delta(*, index: int, call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """Build an OpenAI streaming tool_call delta object without private Hermes imports."""
    return SimpleNamespace(
        index=index,
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _longest_tag_prefix_Tail(text: str, tag: str) -> int:
    """Return length of the longest suffix of `text` that is a proper prefix of `tag`."""
    max_check = min(len(text), len(tag) - 1)
    for length in range(max_check, 0, -1):
        if text.endswith(tag[:length]):
            return length
    return 0


class _AwaitableCompletion:
    """Wrapper around a synchronous completion `SimpleNamespace` that is also `await`-able."""

    def __init__(self, completion: Any) -> None:
        object.__setattr__(self, "_completion", completion)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_completion"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_completion":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_completion"), name, value)

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_completion"))

    def __await__(self):
        async def _coro() -> Any:
            return object.__getattribute__(self, "_completion")

        return _coro().__await__()


class AntigravityStream(Iterator[SimpleNamespace]):
    """Progressive synchronous + asynchronous stream adapter over `agy --output-format stream-json`."""

    def __init__(
        self,
        proc: subprocess.Popen[str],
        model: str,
        gemini_dir: Path | None = None,
        *,
        is_persistent_worker: bool = False,
        stderr_drainer: StderrDrainer | None = None,
        worker_lock: threading.Lock | None = None,
        prev_worker_usage: dict[str, int] | None = None,
        on_turn_complete: Callable[[dict[str, int]], None] | None = None,
        on_worker_dead: Callable[[], None] | None = None,
        on_conversation_id: Callable[[str], None] | None = None,
        on_stream_done: Callable[[AntigravityStream], None] | None = None,
    ) -> None:
        self.proc = proc
        self.model = model
        self.gemini_dir = gemini_dir
        self._is_persistent = is_persistent_worker
        self._worker_lock = worker_lock
        self._lock_released = False
        self._lock_guard = threading.Lock()
        self._prev_usage = dict(prev_worker_usage or {})
        self._on_turn_complete = on_turn_complete
        self._on_worker_dead = on_worker_dead
        self._on_conversation_id = on_conversation_id
        self._on_stream_done = on_stream_done

        self._stderr_drainer = stderr_drainer or StderrDrainer(proc.stderr)
        self._stop_watchdog = threading.Event()
        self._watchdog_errors: list[str] = []
        self._watchdog_thread: threading.Thread | None = None
        if gemini_dir is not None:
            self._watchdog_thread = threading.Thread(
                target=watch_log_for_errors,
                args=(gemini_dir, self._stop_watchdog, self._watchdog_errors, self._kill_proc),
                name="agy-log-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

        self._cancelled = threading.Event()
        self._closed = False
        self._async_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._generator = self._stream_events()

    def _release_worker_lock_once(self) -> None:
        """Idempotently release the persistent worker lock if held by this stream."""
        with self._lock_guard:
            if self._lock_released or self._worker_lock is None:
                return
            self._lock_released = True
            try:
                if self._worker_lock.locked():
                    self._worker_lock.release()
            except RuntimeError:
                pass

    def _kill_proc(self) -> None:
        """Terminate the underlying `agy` process and notify the client if persistent."""
        terminate_process_group(self.proc)
        if self._is_persistent and self._on_worker_dead is not None:
            try:
                self._on_worker_dead()
            except Exception:
                pass

    def cancel(self) -> None:
        """Cross-thread cancellation hook invoked by `AntigravityClient.cancel()`.

        Terminates the child process immediately so any blocking `stdout.readline()`
        unblocks with EOF without closing file descriptors from another thread.
        """
        self._cancelled.set()
        self._stop_watchdog.set()
        self._kill_proc()

    def __iter__(self) -> AntigravityStream:
        return self

    def __next__(self) -> SimpleNamespace:
        return next(self._generator)

    def __await__(self):
        async def _return_self() -> AntigravityStream:
            return self

        return _return_self().__await__()

    def __aiter__(self) -> AntigravityStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self._async_executor is None:
            self._async_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="agy-async-stream",
            )
        loop = asyncio.get_running_loop()
        sentinel = object()
        item = await loop.run_in_executor(self._async_executor, next, self._generator, sentinel)
        if item is sentinel:
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    def __enter__(self) -> AntigravityStream:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> AntigravityStream:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the stream and release resources without blocking a running asyncio loop."""
        if self._closed:
            return
        self._closed = True
        self._stop_watchdog.set()

        if not self._is_persistent:
            in_running_loop = False
            try:
                asyncio.get_running_loop()
                in_running_loop = True
            except RuntimeError:
                pass

            if in_running_loop:
                threading.Thread(
                    target=terminate_process_group,
                    args=(self.proc,),
                    name="agy-async-reaper",
                    daemon=True,
                ).start()
            else:
                terminate_process_group(self.proc)

        try:
            self._generator.close()
        except Exception:
            pass

        if self._async_executor is not None:
            self._async_executor.shutdown(wait=False)
            self._async_executor = None

        self._release_worker_lock_once()
        if self._on_stream_done is not None:
            try:
                self._on_stream_done(self)
            except Exception:
                pass

    @staticmethod
    def _parse_tool_call_json(raw_json: str, default_index: int) -> SimpleNamespace | None:
        """Parse a `<tool_call>{...}</tool_call>` JSON payload into a tool call delta."""
        cleaned = (raw_json or "").strip()
        if not cleaned:
            return None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None

        fn = data.get("function") if isinstance(data.get("function"), dict) else data
        if not isinstance(fn, dict):
            return None
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            args_str = raw_args
        else:
            try:
                args_str = json.dumps(raw_args, ensure_ascii=False)
            except TypeError:
                args_str = "{}"

        call_id = str(data.get("id") or f"call_{default_index + 1}").strip()
        return build_stream_tool_call_delta(
            index=default_index,
            call_id=call_id,
            name=name.strip(),
            arguments=args_str,
        )

    def _stream_events(self) -> Iterator[SimpleNamespace]:
        """Core state machine reading JSONL events from `proc.stdout` and yielding OpenAI chunks."""
        pending_chunks: deque[SimpleNamespace] = deque()
        text_buffer = ""
        in_tool_call = False
        tool_call_buffer = ""
        emitted_tool_calls = 0
        full_content_parts: list[str] = []
        full_reasoning_parts: list[str] = []
        usage_stats: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        saw_result_event = False
        result_stop_reason = "end_turn"
        native_tool_intercepted = False

        def flush_safe_text(*, final: bool = False) -> None:
            nonlocal text_buffer, in_tool_call, tool_call_buffer, emitted_tool_calls
            while True:
                if not in_tool_call:
                    if not text_buffer:
                        return
                    open_idx = text_buffer.find(_TOOL_CALL_OPEN)
                    if open_idx != -1:
                        before = text_buffer[:open_idx]
                        if before:
                            full_content_parts.append(before)
                            pending_chunks.append(
                                make_stream_chunk(self.model, content=before)
                            )
                        text_buffer = text_buffer[open_idx + len(_TOOL_CALL_OPEN) :]
                        in_tool_call = True
                        tool_call_buffer = ""
                        continue

                    if final:
                        full_content_parts.append(text_buffer)
                        pending_chunks.append(
                            make_stream_chunk(self.model, content=text_buffer)
                        )
                        text_buffer = ""
                        return

                    hold_len = _longest_tag_prefix_Tail(text_buffer, _TOOL_CALL_OPEN)
                    emit_len = len(text_buffer) - hold_len
                    if emit_len > 0:
                        to_emit = text_buffer[:emit_len]
                        full_content_parts.append(to_emit)
                        pending_chunks.append(
                            make_stream_chunk(self.model, content=to_emit)
                        )
                        text_buffer = text_buffer[emit_len:]
                    return
                else:
                    tool_call_buffer += text_buffer
                    text_buffer = ""
                    close_idx = tool_call_buffer.find(_TOOL_CALL_CLOSE)
                    if close_idx != -1:
                        raw_payload = tool_call_buffer[:close_idx]
                        remainder = tool_call_buffer[close_idx + len(_TOOL_CALL_CLOSE) :]
                        in_tool_call = False
                        tool_call_buffer = ""
                        text_buffer = remainder

                        delta_tc = self._parse_tool_call_json(raw_payload, emitted_tool_calls)
                        if delta_tc is not None:
                            emitted_tool_calls += 1
                            pending_chunks.append(
                                make_stream_chunk(self.model, tool_calls=[delta_tc])
                            )
                        else:
                            literal = f"{_TOOL_CALL_OPEN}{raw_payload}{_TOOL_CALL_CLOSE}"
                            full_content_parts.append(literal)
                            pending_chunks.append(
                                make_stream_chunk(self.model, content=literal)
                            )
                        continue

                    if final:
                        # Unclosed <tool_call> at end of stream: try parsing JSON anyway, else emit literal
                        delta_tc = self._parse_tool_call_json(tool_call_buffer, emitted_tool_calls)
                        if delta_tc is not None:
                            emitted_tool_calls += 1
                            pending_chunks.append(
                                make_stream_chunk(self.model, tool_calls=[delta_tc])
                            )
                        else:
                            literal = f"{_TOOL_CALL_OPEN}{tool_call_buffer}"
                            full_content_parts.append(literal)
                            pending_chunks.append(
                                make_stream_chunk(self.model, content=literal)
                            )
                        in_tool_call = False
                        tool_call_buffer = ""
                    return

        stdout = self.proc.stdout
        try:
            if stdout is not None:
                for raw_line in iter(stdout.readline, ""):
                    if not raw_line:
                        break
                    while pending_chunks:
                        yield pending_chunks.popleft()

                    if self._watchdog_errors:
                        raise RuntimeError(
                            f"Antigravity rate limit / quota error detected in logs: {self._watchdog_errors[0]}"
                        )

                    stripped = raw_line.strip()
                    if not stripped:
                        continue

                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(event, dict):
                        continue

                    conv_id = event.get("conversation_id") or event.get("session_id")
                    if isinstance(conv_id, str) and conv_id.strip() and self._on_conversation_id is not None:
                        try:
                            self._on_conversation_id(conv_id.strip())
                        except Exception:
                            pass

                    ev_type = str(event.get("event") or event.get("type") or "").strip().lower()

                    if ev_type == "init":
                        init_obj = event.get("init")
                        if isinstance(init_obj, dict):
                            init_conv = init_obj.get("conversation_id") or init_obj.get("session_id")
                            if isinstance(init_conv, str) and init_conv.strip() and self._on_conversation_id is not None:
                                try:
                                    self._on_conversation_id(init_conv.strip())
                                except Exception:
                                    pass
                        continue

                    if ev_type == "step_update":
                        step_obj = event.get("step_update")
                        if isinstance(step_obj, dict):
                            step_conv = step_obj.get("conversation_id")
                            if isinstance(step_conv, str) and step_conv.strip() and self._on_conversation_id is not None:
                                try:
                                    self._on_conversation_id(step_conv.strip())
                                except Exception:
                                    pass
                            step_type = str(step_obj.get("step_type") or "").strip().lower()
                            if step_type == "tool":
                                native_tool_intercepted = True
                                self._kill_proc()
                                break

                            raw_step_usage = step_obj.get("usage")
                            if isinstance(raw_step_usage, dict):
                                for k in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens"):
                                    if k in raw_step_usage:
                                        usage_stats[k] = int(raw_step_usage.get(k) or 0)

                            thinking_delta = step_obj.get("thinking_delta") or step_obj.get("thinking")
                            if isinstance(thinking_delta, str) and thinking_delta:
                                full_reasoning_parts.append(thinking_delta)
                                pending_chunks.append(
                                    make_stream_chunk(self.model, reasoning_content=thinking_delta)
                                )

                            text_delta = step_obj.get("text_delta")
                            if isinstance(text_delta, str) and text_delta:
                                text_buffer += text_delta
                                flush_safe_text(final=False)

                        while pending_chunks:
                            yield pending_chunks.popleft()
                        continue

                    if ev_type == "error":
                        err_msg = str(event.get("error") or event.get("message") or "Unknown Antigravity error")
                        raise RuntimeError(f"Antigravity model error: {err_msg}")

                    if ev_type == "assistant":
                        msg_obj = event.get("message")
                        if isinstance(msg_obj, dict):
                            step_type = str(msg_obj.get("step_type") or "").strip().lower()
                            if step_type == "tool":
                                # Security boundary: immediately kill `agy` if it attempts native tool execution
                                native_tool_intercepted = True
                                self._kill_proc()
                                break

                            blocks = msg_obj.get("content")
                            if isinstance(blocks, list):
                                for block in blocks:
                                    if not isinstance(block, dict):
                                        continue
                                    b_type = str(block.get("type") or "").strip().lower()
                                    if b_type == "thinking":
                                        thinking_text = str(block.get("thinking") or "")
                                        if thinking_text:
                                            full_reasoning_parts.append(thinking_text)
                                            pending_chunks.append(
                                                make_stream_chunk(self.model, reasoning_content=thinking_text)
                                            )
                                    elif b_type == "text":
                                        chunk_text = str(block.get("text") or "")
                                        if chunk_text:
                                            text_buffer += chunk_text
                                            flush_safe_text(final=False)
                        while pending_chunks:
                            yield pending_chunks.popleft()
                        continue

                    if ev_type == "result":
                        saw_result_event = True
                        res_obj = event.get("result")
                        if isinstance(res_obj, dict):
                            res_conv = res_obj.get("conversation_id")
                            if isinstance(res_conv, str) and res_conv.strip() and self._on_conversation_id is not None:
                                try:
                                    self._on_conversation_id(res_conv.strip())
                                except Exception:
                                    pass
                            res_status = str(res_obj.get("status") or "").strip().upper()
                            res_err = res_obj.get("error")
                            if res_err or res_status == "ERROR":
                                raise RuntimeError(f"Antigravity model error: {res_err or res_status}")
                            stop_r = res_obj.get("stop_reason") or res_obj.get("status")
                            if isinstance(stop_r, str) and stop_r.strip():
                                result_stop_reason = stop_r.strip()
                            result_text = res_obj.get("response") or res_obj.get("result")
                            raw_usage = res_obj.get("usage") or event.get("usage")
                        else:
                            if event.get("is_error"):
                                err_detail = str(
                                    event.get("error") or event.get("result") or "Unknown execution error"
                                )
                                raise RuntimeError(f"Antigravity execution failed: {err_detail}")
                            stop_r = event.get("stop_reason")
                            if isinstance(stop_r, str) and stop_r.strip():
                                result_stop_reason = stop_r.strip()
                            result_text = res_obj if isinstance(res_obj, str) else None
                            raw_usage = event.get("usage")

                        # If `result` carries final text that was never streamed via step_update/assistant events
                        if (
                            isinstance(result_text, str)
                            and result_text.strip()
                            and not full_content_parts
                            and not text_buffer
                            and not in_tool_call
                            and emitted_tool_calls == 0
                        ):
                            text_buffer += result_text
                            flush_safe_text(final=False)

                        if isinstance(raw_usage, dict):
                            cumulative: dict[str, int] = {
                                "input_tokens": int(raw_usage.get("input_tokens") or 0),
                                "output_tokens": int(raw_usage.get("output_tokens") or 0),
                                "reasoning_tokens": int(raw_usage.get("reasoning_tokens") or 0),
                                "cache_read_tokens": int(raw_usage.get("cache_read_tokens") or 0),
                                "cache_write_tokens": int(raw_usage.get("cache_write_tokens") or 0),
                            }
                            if self._is_persistent:
                                for k, val in cumulative.items():
                                    prev_val = int(self._prev_usage.get(k) or 0)
                                    usage_stats[k] = val - prev_val if val >= prev_val else val
                                if self._on_turn_complete is not None:
                                    try:
                                        self._on_turn_complete(cumulative)
                                    except Exception:
                                        pass
                            else:
                                usage_stats.update(cumulative)
                        break

            # Flush any remaining buffered text at end of turn
            flush_safe_text(final=True)
            while pending_chunks:
                yield pending_chunks.popleft()

            if self._watchdog_errors:
                raise RuntimeError(
                    f"Antigravity rate limit / quota error detected in logs: {self._watchdog_errors[0]}"
                )

            if self._cancelled.is_set():
                raise RuntimeError("Antigravity turn cancelled by caller")

            if not saw_result_event and not native_tool_intercepted:
                if self._is_persistent and self._on_worker_dead is not None:
                    try:
                        self._on_worker_dead()
                    except Exception:
                        pass
                stderr_out = self._stderr_drainer.get_output()
                rc = self.proc.poll()
                if not full_content_parts and not full_reasoning_parts and emitted_tool_calls == 0:
                    detail = f" (stderr: {stderr_out})" if stderr_out else ""
                    raise RuntimeError(
                        f"Antigravity worker exited unexpectedly without producing a result (code={rc}){detail}"
                    )

            finish_reason = "tool_calls" if emitted_tool_calls > 0 else "stop"
            if result_stop_reason in ("max_tokens", "length") and finish_reason == "stop":
                finish_reason = "length"

            content_joined = "".join(full_content_parts)
            reasoning_joined = "".join(full_reasoning_parts)
            warn_if_short_delivery(
                model=self.model,
                finish_reason=finish_reason,
                stop_reason=result_stop_reason,
                content_text=content_joined,
                reasoning_text=reasoning_joined,
                has_tool_calls=(emitted_tool_calls > 0),
                output_tokens=usage_stats["output_tokens"],
            )

            # Final finish_reason chunk + usage chunk
            yield make_stream_chunk(self.model, finish_reason=finish_reason)

            prompt_tokens = (
                usage_stats["input_tokens"]
                + usage_stats["cache_read_tokens"]
                + usage_stats["cache_write_tokens"]
            )
            completion_tokens = usage_stats["output_tokens"] + usage_stats["reasoning_tokens"]
            usage_ns = SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                input_tokens=usage_stats["input_tokens"],
                output_tokens=usage_stats["output_tokens"],
                reasoning_tokens=usage_stats["reasoning_tokens"],
                cache_read_tokens=usage_stats["cache_read_tokens"],
                cache_write_tokens=usage_stats["cache_write_tokens"],
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=usage_stats["reasoning_tokens"]
                ),
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=usage_stats["cache_read_tokens"],
                    cache_write_tokens=usage_stats["cache_write_tokens"],
                ),
            )
            yield SimpleNamespace(choices=[], model=self.model, usage=usage_ns)

        finally:
            self._stop_watchdog.set()
            if not self._is_persistent:
                terminate_process_group(self.proc)
            self._release_worker_lock_once()
            if self._on_stream_done is not None:
                try:
                    self._on_stream_done(self)
                except Exception:
                    pass
