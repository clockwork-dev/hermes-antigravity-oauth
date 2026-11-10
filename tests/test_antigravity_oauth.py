"""Comprehensive hermetic test suite for the `antigravity-oauth` Hermes provider plugin."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import providers
from agent.error_classifier import FailoverReason, classify_api_error
from hermes_cli.auth_constants import AuthError

pkg = sys.modules["hermes_antigravity_oauth"]
AntigravityClient = pkg.AntigravityClient
AntigravityOAuthProfile = pkg.AntigravityOAuthProfile
antigravity_oauth_profile = pkg.antigravity_oauth_profile
build_clean_child_env = pkg.build_clean_child_env
check_antigravity_auth = pkg.check_antigravity_auth
check_antigravity_token_file_evidence = pkg.check_antigravity_token_file_evidence
describe_antigravity_auth_source = pkg.describe_antigravity_auth_source
fetch_antigravity_usage = pkg.fetch_antigravity_usage
get_annotated_models = pkg.get_annotated_models
get_available_models = pkg.get_available_models
get_model_catalog = pkg.get_model_catalog
invalidate_model_catalog_cache = pkg.invalidate_model_catalog_cache
invalidate_usage_cache = pkg.invalidate_usage_cache
parse_model_and_effort = pkg.parse_model_and_effort
resolve_antigravity_binary = pkg.resolve_antigravity_binary
resolve_model_family_alias = pkg.resolve_model_family_alias
setup_isolated_home = pkg.setup_isolated_home

client_mod = sys.modules["hermes_antigravity_oauth.client"]
models_mod = sys.modules["hermes_antigravity_oauth.models"]
process_mod = sys.modules["hermes_antigravity_oauth.process"]
prompt_mod = sys.modules["hermes_antigravity_oauth.prompt"]
stream_mod = sys.modules["hermes_antigravity_oauth.stream"]
usage_mod = sys.modules["hermes_antigravity_oauth.usage"]


class _FakePopen:
    """In-memory fake `subprocess.Popen` for hermetic worker and oneshot tests."""

    def __init__(self, stdout_lines: list[str], stderr_lines: list[str] | None = None, returncode: int = 0) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line if line.endswith("\n") else line + "\n" for line in stdout_lines))
        self.stderr = io.StringIO("".join((stderr_lines or [])))
        self.pid = 99991
        self.returncode = returncode
        self._terminated = False

    def poll(self) -> int | None:
        return self.returncode if self._terminated else None

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self._terminated = True
        return self.returncode


class ProviderRegistrationAndHooksTests(unittest.TestCase):
    def test_provider_registered_with_aliases_and_flags(self) -> None:
        for name in (
            "antigravity-oauth",
            "antigravity",
            "agy",
            "agy-oauth",
            "antigravity-subscription",
            "antigravity-direct",
            "antigravity-subscription-directsdk",
        ):
            resolved = providers.get_provider_profile(name)
            self.assertIs(resolved, antigravity_oauth_profile)

        self.assertEqual(antigravity_oauth_profile.auth_type, "external_process")
        self.assertTrue(antigravity_oauth_profile.supports_streaming)
        self.assertTrue(antigravity_oauth_profile.supports_vision)
        self.assertTrue(antigravity_oauth_profile.HERMES_SKIP_TRANSPORT_WRAP)
        self.assertTrue(antigravity_oauth_profile.HERMES_SKIP_ASYNC_WRAP)
        self.assertTrue(callable(antigravity_oauth_profile.auth_handler))
        self.assertTrue(callable(antigravity_oauth_profile.classify_api_error))

    def test_setup_status_and_auth_handler_flows(self) -> None:
        # 1. Missing binary
        with patch.object(pkg, "resolve_antigravity_binary", return_value=""):
            st = antigravity_oauth_profile.setup_status()
            self.assertFalse(st["ready"])
            self.assertIn("not installed", st["reason"])

        # 2. Binary present, signed out -> provides inline login_cmd
        with (
            patch.object(pkg, "resolve_antigravity_binary", return_value="/usr/local/bin/agy"),
            patch.object(pkg, "check_antigravity_auth", return_value=False),
        ):
            st = antigravity_oauth_profile.setup_status()
            self.assertFalse(st["ready"])
            self.assertEqual(st["login_cmd"], ["/usr/local/bin/agy", "acp", "--login"])

        # 3. Binary present, signed in -> ready
        with (
            patch.object(pkg, "resolve_antigravity_binary", return_value="/usr/local/bin/agy"),
            patch.object(pkg, "check_antigravity_auth", return_value=True),
            patch.object(pkg, "describe_antigravity_auth_source", return_value="OAuth token file"),
        ):
            st = antigravity_oauth_profile.setup_status()
            self.assertTrue(st["ready"])
            self.assertIn("OAuth token file", st["reason"])

        # 4. auth_handler("add") invokes `agy acp --login` inside isolated Hermes profile
        with tempfile.TemporaryDirectory() as fake_hermes_home:
            with (
                patch.object(pkg, "resolve_antigravity_binary", return_value="/usr/local/bin/agy"),
                patch.object(process_mod, "resolve_hermes_antigravity_home", return_value=Path(fake_hermes_home)),
                patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as mock_run,
                patch.object(pkg, "check_antigravity_auth", return_value=True),
                patch.object(pkg, "describe_antigravity_auth_source", return_value="Hermes isolated OAuth token file"),
            ):
                handled = antigravity_oauth_profile.auth_handler("add")
                self.assertTrue(handled)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args[0][0], ["/usr/local/bin/agy", "acp", "--login"])
                call_env = mock_run.call_args[1]["env"]
                self.assertEqual(call_env["HOME"], fake_hermes_home)
                self.assertEqual(call_env["USERPROFILE"], fake_hermes_home)
                self.assertIn("SSH_CONNECTION", call_env)

    def test_error_classifier_integration_with_hermes_core(self) -> None:
        # Output token limit -> server_error, retryable=True, should_compress=False
        v_cutoff = classify_api_error(
            RuntimeError("your previous response was cut off because it exceeded the output token limit"),
            provider="antigravity-oauth",
        )
        self.assertEqual(v_cutoff.reason, FailoverReason.server_error)
        self.assertTrue(v_cutoff.retryable)
        self.assertFalse(v_cutoff.should_compress)

        # Trajectory overflow -> context_overflow, should_compress=True
        v_overflow = classify_api_error(
            RuntimeError("Antigravity error: max_trajectory_tokens exceeded"),
            provider="antigravity-oauth",
        )
        self.assertEqual(v_overflow.reason, FailoverReason.context_overflow)
        self.assertTrue(v_overflow.retryable)
        self.assertTrue(v_overflow.should_compress)

        # Billing exhaustion -> billing, retryable=False, should_fallback=True
        v_billing = classify_api_error(
            RuntimeError("Your AI credits balance is too low to continue."),
            provider="antigravity-oauth",
        )
        self.assertEqual(v_billing.reason, FailoverReason.billing)
        self.assertFalse(v_billing.retryable)
        self.assertTrue(v_billing.should_fallback)

        # Account verification -> auth_permanent
        v_auth = classify_api_error(
            RuntimeError("Please verify your account to continue"),
            provider="antigravity-oauth",
        )
        self.assertEqual(v_auth.reason, FailoverReason.auth_permanent)
        self.assertFalse(v_auth.retryable)


class ZeroExfiltrationAuthAndEnvironmentTests(unittest.TestCase):
    def test_token_file_detection_and_isolated_home_linking(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as ws:
            gemini_dir = Path(fake_home) / ".gemini" / "antigravity-cli"
            gemini_dir.mkdir(parents=True)
            token_file = gemini_dir / "jetski-standalone-oauth-token"
            token_file.write_text('{"access_token":"secret_oauth_token"}', encoding="utf-8")

            with patch("pathlib.Path.home", return_value=Path(fake_home)), patch.dict(
                os.environ, {"ANTIGRAVITY_CONFIG_DIR": ""}, clear=False
            ):
                self.assertTrue(check_antigravity_auth())
                ok, source = check_antigravity_token_file_evidence()
                self.assertTrue(ok)
                self.assertIn("jetski-standalone-oauth-token", source)

                isolated_home, iso_gemini = setup_isolated_home(ws)
                linked_token = iso_gemini / "jetski-standalone-oauth-token"
                self.assertTrue(linked_token.is_file())
                self.assertEqual(linked_token.read_text(encoding="utf-8"), '{"access_token":"secret_oauth_token"}')
                self.assertTrue((isolated_home / ".gemini" / "SOUL.md").is_file())

    def test_isolated_hermes_account_prioritized_over_global_account(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as ws:
            # Global account in ~/.gemini/antigravity-cli
            global_dir = Path(fake_home) / ".gemini" / "antigravity-cli"
            global_dir.mkdir(parents=True)
            (global_dir / "antigravity-oauth-token").write_text('{"account":"global_agy_user"}', encoding="utf-8")

            # Hermes-isolated account in ~/.hermes/antigravity-oauth/.gemini/antigravity-cli
            hermes_dir = Path(fake_home) / ".hermes" / "antigravity-oauth" / ".gemini" / "antigravity-cli"
            hermes_dir.mkdir(parents=True)
            (hermes_dir / "antigravity-oauth-token").write_text('{"account":"hermes_isolated_user"}', encoding="utf-8")

            with patch("pathlib.Path.home", return_value=Path(fake_home)), patch.dict(
                os.environ, {"ANTIGRAVITY_CONFIG_DIR": "", "ANTIGRAVITY_HERMES_HOME": ""}, clear=False
            ):
                source = describe_antigravity_auth_source()
                self.assertIn("Hermes isolated OAuth token file", source)

                isolated_home, iso_gemini = setup_isolated_home(ws)
                # Both antigravity-oauth-token and jetski-standalone-oauth-token in isolated_home receive the Hermes token
                self.assertEqual(
                    (iso_gemini / "antigravity-oauth-token").read_text(encoding="utf-8"),
                    '{"account":"hermes_isolated_user"}',
                )
                self.assertEqual(
                    (iso_gemini / "jetski-standalone-oauth-token").read_text(encoding="utf-8"),
                    '{"account":"hermes_isolated_user"}',
                )
                child_env = build_clean_child_env(isolated_home)
                self.assertEqual(child_env["HOME"], str(isolated_home))
                self.assertEqual(child_env["USERPROFILE"], str(isolated_home))
                self.assertIn("SSH_CONNECTION", child_env)

                # Removing Hermes-isolated auth leaves global account intact
                removed = process_mod.remove_isolated_antigravity_auth()
                self.assertTrue(len(removed) >= 1)
                self.assertTrue((global_dir / "antigravity-oauth-token").is_file())
                self.assertIn("OAuth token file", describe_antigravity_auth_source())
                self.assertNotIn("Hermes isolated", describe_antigravity_auth_source())

    def test_windows_macos_linux_keyring_probes_never_read_secrets(self) -> None:
        # Windows cmdkey probe
        with (
            patch.object(process_mod.sys, "platform", "win32"),
            patch.object(process_mod, "_find_existing_oauth_token", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout="Target: LegacyGeneric:target=gemini:antigravity\nType: Generic\nUser: antigravity\n",
            )
            self.assertTrue(check_antigravity_auth())
            cmd_args = mock_run.call_args[0][0]
            self.assertIn("/list:gemini:antigravity", cmd_args[1])

        # macOS security probe (must NOT pass -w or -g)
        with (
            patch.object(process_mod.sys, "platform", "darwin"),
            patch.object(process_mod, "_find_existing_oauth_token", return_value=None),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(returncode=0)
            self.assertTrue(check_antigravity_auth())
            cmd_args = mock_run.call_args[0][0]
            self.assertNotIn("-w", cmd_args)
            self.assertNotIn("-g", cmd_args)
            self.assertEqual(mock_run.call_args[1]["stdout"], subprocess.DEVNULL)

        # Linux secret-tool probe (must bind stdout=DEVNULL and inspect stderr only)
        with (
            patch.object(process_mod.sys, "platform", "linux"),
            patch.object(process_mod, "_find_existing_oauth_token", return_value=None),
            patch("os.path.isfile", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stderr="[/org/freedesktop/secrets/collection/login/42]\nlabel = gemini\n",
            )
            self.assertTrue(check_antigravity_auth())
            self.assertEqual(mock_run.call_args[1]["stdout"], subprocess.DEVNULL)
            self.assertEqual(mock_run.call_args[1]["stderr"], subprocess.PIPE)

    def test_child_env_strict_and_heuristic_sanitization(self) -> None:
        isolated = Path("/tmp/fake_iso_home")
        dirty_env = {
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "sk-proj-1234567890abcdefghijklmnop",
            "GITHUB_TOKEN": "ghp_1234567890abcdefghijklmnopqrstuv",
            "UNLABELED_VAR": "AIzaSyA123456789012345678901234567890",
            "LD_PRELOAD": "/tmp/evil.so",
            "HTTPS_PROXY": "http://attacker:8080",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
            "SAFE_CUSTOM_VAR": "hello_world",
        }
        # Strict mode (default)
        with patch.dict(os.environ, dirty_env, clear=True):
            env_strict = build_clean_child_env(isolated)
            self.assertIn("PATH", env_strict)
            self.assertNotIn("OPENAI_API_KEY", env_strict)
            self.assertNotIn("GITHUB_TOKEN", env_strict)
            self.assertNotIn("UNLABELED_VAR", env_strict)
            self.assertNotIn("LD_PRELOAD", env_strict)
            self.assertNotIn("HTTPS_PROXY", env_strict)
            self.assertNotIn("SAFE_CUSTOM_VAR", env_strict)
            self.assertEqual(env_strict["HOME"], str(isolated))

        # Heuristic mode (ANTIGRAVITY_ENV_STRICT=0) without token file (strips SSH_AUTH_SOCK too)
        with (
            patch.dict(os.environ, {**dirty_env, "ANTIGRAVITY_ENV_STRICT": "0", "ANTIGRAVITY_PROXY": "http://corp:3128"}, clear=True),
            patch.object(process_mod, "_find_existing_oauth_token", return_value=None),
        ):
            env_heur = build_clean_child_env(isolated)
            self.assertIn("SAFE_CUSTOM_VAR", env_heur)
            self.assertNotIn("OPENAI_API_KEY", env_heur)
            self.assertNotIn("UNLABELED_VAR", env_heur)
            self.assertNotIn("LD_PRELOAD", env_heur)
            self.assertNotIn("SSH_AUTH_SOCK", env_heur)
            self.assertEqual(env_heur["HTTPS_PROXY"], "http://corp:3128")


class ModelResolutionAndCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_model_catalog_cache()

    def test_dynamic_catalog_and_cross_version_alias_resolution(self) -> None:
        # Case A: agy 1.1 / fallback catalog where claude-sonnet-4-6 is effortless and 5-5 is aliased to 4-6
        fallback_fams = dict(models_mod._FALLBACK_FAMILIES)
        with patch.object(models_mod, "get_model_catalog", return_value=(models_mod._FALLBACK_MODELS, fallback_fams)):
            with tempfile.TemporaryDirectory() as tmp:
                client = AntigravityClient(binary="agy", cwd=tmp)
                self.assertEqual(
                    client._resolve_model_and_effort("gemini-3.8-flash", "low"),
                    ("gemini-3.8-flash-low", None),
                )
                self.assertEqual(
                    client._resolve_model_and_effort("gemini-3.1-pro", "medium"),
                    ("gemini-3.1-pro-high", None),
                )
                self.assertEqual(
                    client._resolve_model_and_effort("claude-sonnet-4-6", "low"),
                    ("claude-sonnet-4-6", None),
                )
                self.assertEqual(
                    client._resolve_model_and_effort("claude-sonnet-5-5", "low"),
                    ("claude-sonnet-4-6", None),
                )
                client.close()

        # Case B: agy 1.2+ live catalog where claude-sonnet-5-5 has (low, medium, high) and 4-6 is absent
        live_1_2_fams = {
            "gemini-4-argon": ("low", "medium", "high"),
            "gemini-3.8-flash": ("low", "medium", "high"),
            "claude-sonnet-5-5": ("low", "medium", "high"),
            "claude-opus-5-5": ("low", "medium", "high"),
            "gpt-oss-120b": ("medium",),
        }
        with patch.object(models_mod, "get_model_catalog", return_value=([], live_1_2_fams)):
            with tempfile.TemporaryDirectory() as tmp:
                client = AntigravityClient(binary="agy", cwd=tmp)
                self.assertEqual(
                    client._resolve_model_and_effort("claude-sonnet-5-5", "low"),
                    ("claude-sonnet-5-5-low", None),
                )
                # Requesting 4-6 on agy 1.2 transparently upgrades to 5-5!
                self.assertEqual(
                    client._resolve_model_and_effort("claude-sonnet-4-6", "medium"),
                    ("claude-sonnet-5-5-medium", None),
                )
                self.assertEqual(
                    client._resolve_model_and_effort("gemini-4-argon", "high"),
                    ("gemini-4-argon-high", None),
                )
                client.close()


class StreamParserAndClientExecutionTests(unittest.TestCase):
    def test_progressive_tool_call_streaming_and_usage_deltas(self) -> None:
        lines = [
            json.dumps({"type": "system", "conversation_id": "conv-abc-123"}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "step_type": "thinking",
                    "content": [{"type": "thinking", "thinking": "Analyzing request..."}],
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello! Let me run a tool: <tool_c"}],
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": 'all>{"id":"call_99","type":"function","function":{"name":"terminal","arguments":"{\\"cmd\\":\\"ls\\"}"}}</tool_call>',
                        }
                    ],
                },
            }),
            json.dumps({
                "type": "result",
                "is_error": False,
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 45,
                    "reasoning_tokens": 20,
                    "cache_read_tokens": 50,
                },
            }),
        ]
        proc = _FakePopen(lines)
        recorded_conv: list[str] = []
        updated_usage: list[dict[str, int]] = []
        stream = stream_mod.AntigravityStream(
            proc,
            "gemini-3.8-flash-high",
            is_persistent_worker=True,
            prev_worker_usage={"input_tokens": 100, "output_tokens": 15, "reasoning_tokens": 5, "cache_read_tokens": 20},
            on_turn_complete=updated_usage.append,
            on_conversation_id=recorded_conv.append,
        )
        completion = stream_mod.collect_stream_completion(stream, "gemini-3.8-flash-high")
        self.assertEqual(recorded_conv, ["conv-abc-123"])
        self.assertEqual(completion.choices[0].message.content, "Hello! Let me run a tool: ")
        self.assertEqual(completion.choices[0].message.reasoning_content, "Analyzing request...")
        self.assertEqual(completion.choices[0].finish_reason, "tool_calls")
        self.assertEqual(len(completion.choices[0].message.tool_calls), 1)
        tc = completion.choices[0].message.tool_calls[0]
        self.assertEqual(tc.id, "call_99")
        self.assertEqual(tc.function.name, "terminal")
        self.assertEqual(tc.function.arguments, '{"cmd":"ls"}')
        # Verify delta usage subtraction from prev_worker_usage
        self.assertEqual(completion.usage.input_tokens, 50)
        self.assertEqual(completion.usage.output_tokens, 30)
        self.assertEqual(completion.usage.reasoning_tokens, 15)
        self.assertEqual(completion.usage.cache_read_tokens, 30)
        self.assertEqual(updated_usage[0]["input_tokens"], 150)

    def test_native_tool_step_immediately_kills_process(self) -> None:
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "step_type": "tool",
                    "content": [{"type": "text", "text": "Attempting native tool..."}],
                },
            }),
        ]
        proc = _FakePopen(lines)
        stream = stream_mod.AntigravityStream(proc, "gemini-3.8-flash-high")
        chunks = list(stream)
        self.assertTrue(proc._terminated)
        self.assertTrue(any(getattr(c.choices[0], "finish_reason", None) == "stop" for c in chunks if c.choices))

    def test_client_cancel_terminates_active_streams_and_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = AntigravityClient(binary="agy", cwd=tmp)
            proc = _FakePopen([])
            stream = stream_mod.AntigravityStream(proc, "gemini-3.8-flash-high")
            client._register_stream(stream)
            client.cancel()
            self.assertTrue(proc._terminated)
            self.assertTrue(stream._cancelled.is_set())
            client.close()

    def test_async_await_and_aiter_compatibility(self) -> None:
        async def _run_async_checks() -> None:
            lines = [
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Async hello!"}]},
                }),
                json.dumps({
                    "type": "result",
                    "is_error": False,
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }),
            ]
            proc1 = _FakePopen(lines)
            stream1 = stream_mod.AntigravityStream(proc1, "gemini-3.8-flash-high")
            awaited_stream = await stream1
            collected_text: list[str] = []
            async for chunk in awaited_stream:
                for ch in getattr(chunk, "choices", None) or ():
                    if getattr(ch.delta, "content", None):
                        collected_text.append(ch.delta.content)
            self.assertEqual("".join(collected_text), "Async hello!")

            proc2 = _FakePopen(lines)
            stream2 = stream_mod.AntigravityStream(proc2, "gemini-3.8-flash-high")
            comp = stream_mod._AwaitableCompletion(
                stream_mod.collect_stream_completion(stream2, "gemini-3.8-flash-high")
            )
            awaited_comp = await comp
            self.assertEqual(awaited_comp.choices[0].message.content, "Async hello!")

        asyncio.run(_run_async_checks())


class UsageSingleFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_usage_cache()

    def test_usage_snapshot_parsing_and_caching(self) -> None:
        fake_json = json.dumps({
            "tier": "Google AI Ultra",
            "models": [
                {
                    "displayName": "Gemini 3.8 Flash",
                    "remainingPercentage": 0.75,
                    "resetTime": "2026-11-10T00:00:00Z",
                },
                {
                    "displayName": "Claude Sonnet 5.5",
                    "remainingPercentage": 0.4,
                    "resetTime": "2026-11-10T00:00:00Z",
                },
            ],
        })
        with (
            patch.object(usage_mod, "resolve_antigravity_binary", return_value="/usr/local/bin/agy"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=fake_json)) as mock_run,
        ):
            snap1 = fetch_antigravity_usage()
            snap2 = fetch_antigravity_usage()
            self.assertIsNotNone(snap1)
            self.assertIs(snap1, snap2)
            self.assertEqual(mock_run.call_count, 1)
            self.assertEqual(snap1.plan, "Google AI Ultra")
            self.assertEqual(len(snap1.windows), 2)
            self.assertEqual(snap1.windows[0].label, "Gemini 3.8 Flash")
            self.assertEqual(snap1.windows[0].used_percent, 25.0)
            self.assertEqual(snap1.windows[1].used_percent, 60.0)


class EndToEndWorkerAndConcurrencyTests(unittest.TestCase):
    def test_worker_lock_ownership_never_releases_another_threads_lock(self) -> None:
        """Regression test for the worker lock race condition in _create_chat_completion."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "agy"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            client = AntigravityClient(binary=str(fake_bin), cwd=tmp)

            lines = [
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Done."}]},
                }),
                json.dumps({
                    "type": "result",
                    "is_error": False,
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }),
            ]
            fake_proc = _FakePopen(lines)

            orig_collect = client_mod.collect_stream_completion
            second_thread_acquired = False

            def _collect_and_sim_second_thread(stream_iter, model):
                nonlocal second_thread_acquired
                res = orig_collect(stream_iter, model)
                # At this point stream_iter's finally block has released _worker_lock.
                # Simulate Thread B immediately acquiring _worker_lock before Thread A's
                # _create_chat_completion `finally` block runs!
                second_thread_acquired = client._worker_lock.acquire(blocking=False)
                return res

            with (
                patch.object(client_mod, "check_antigravity_auth", return_value=True),
                patch.object(client_mod, "popen_isolated_session", return_value=fake_proc),
                patch.object(client_mod, "collect_stream_completion", side_effect=_collect_and_sim_second_thread),
            ):
                resp = client.chat.completions.create(
                    model="gemini-3.8-flash-high",
                    messages=[{"role": "user", "content": "Hello"}],
                    stream=False,
                )
                self.assertEqual(resp.choices[0].message.content, "Done.")
                self.assertTrue(second_thread_acquired)
                # Thread B's lock MUST still be held after Thread A's _create_chat_completion returns!
                self.assertTrue(client._worker_lock.locked())
                client._worker_lock.release()

            client.close()

    def test_concurrent_turn_falls_back_to_oneshot_while_worker_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "agy"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            client = AntigravityClient(binary=str(fake_bin), cwd=tmp)

            lines = [
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Oneshot response"}]},
                }),
                json.dumps({
                    "type": "result",
                    "is_error": False,
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                }),
            ]
            fake_proc = _FakePopen(lines)

            # Simulate worker busy by holding _worker_lock
            client._worker_lock.acquire()
            try:
                with (
                    patch.object(client_mod, "check_antigravity_auth", return_value=True),
                    patch.object(client_mod, "popen_isolated_session", return_value=fake_proc) as mock_popen,
                ):
                    resp = client.chat.completions.create(
                        model="gemini-3.8-flash-high",
                        messages=[{"role": "user", "content": "Concurrent subagent call"}],
                        stream=False,
                    )
                    self.assertEqual(resp.choices[0].message.content, "Oneshot response")
                    spawn_cmd = mock_popen.call_args[0][0]
                    # Oneshot does NOT pass --input-format stream-json
                    self.assertNotIn("--input-format", spawn_cmd)
                    self.assertIn("--output-format", spawn_cmd)
            finally:
                client._worker_lock.release()
                client.close()

    def test_workspace_cleanup_never_widens_hardlinked_token_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as ws:
            real_token = Path(outside) / "jetski-standalone-oauth-token"
            real_token.write_text("oauth_secret", encoding="utf-8")
            os.chmod(real_token, 0o400)
            baseline_mode = stat.S_IMODE(os.stat(real_token).st_mode)

            linked = Path(ws) / "linked-token"
            try:
                os.link(real_token, linked)
            except OSError:
                self.skipTest("Filesystem does not support hardlinks")

            client_mod._chmod_writable_if_owned_unlinked(linked)
            after_mode = stat.S_IMODE(os.stat(real_token).st_mode)
            self.assertEqual(after_mode, baseline_mode)
            # Make real_token writable again so TemporaryDirectory cleanup succeeds on Windows
            os.chmod(real_token, 0o600)


class PromptPruningAndDiagnosticsTests(unittest.TestCase):
    def test_historical_tool_outputs_pruned_and_recent_preserved(self) -> None:
        messages = [{"role": "user", "content": "Start task"}]
        for idx in range(12):
            messages.append({
                "role": "tool",
                "content": f"Tool_Output_{idx}_" + ("X" * 500),
            })

        prompt = prompt_mod.build_prompt_from_messages(
            messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "Run shell command",
                        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                    },
                }
            ],
        )
        self.assertIn("CRITICAL SYSTEM OVERRIDE FOR SUBPROCESS BRIDGE", prompt)
        # First 4 tool outputs (12 - 8 = 4) should be pruned
        self.assertIn("Historical tool output pruned for context efficiency", prompt)
        # Most recent 8 tool outputs (indices 4..11) remain intact
        self.assertIn("Tool_Output_11_" + ("X" * 500), prompt)

    def test_delta_recovery_nudge_rewritten_cleanly(self) -> None:
        delta = prompt_mod.format_delta_messages([
            {"role": "user", "content": "Your previous response was cut off. Please continue where you left off."}
        ])
        self.assertIn("Resume immediately from the exact point where you stopped", delta)

    def test_plugin_admission_validation_and_security_guard_pass(self) -> None:
        from hermes_cli.plugin_validate import validate_plugin_dir

        plugin_root = Path(__file__).resolve().parent.parent
        report = validate_plugin_dir(plugin_root)
        self.assertTrue(report.ok, f"Validation failed: {report.failures}")
        self.assertEqual(len(report.failures), 0)
        self.assertEqual(len(report.warnings), 0)


class CrossPlatformLinuxAndWindowsTests(unittest.TestCase):
    def test_resolve_hermes_antigravity_home_across_linux_systemd_and_windows(self) -> None:
        # 1. Explicit HERMES_HOME on Linux (systemd unit environment)
        with (
            patch.object(process_mod, "_is_path_home_mocked", return_value=False),
            patch.dict(
                os.environ,
                {
                    "ANTIGRAVITY_HERMES_HOME": "",
                    "HERMES_HOME": "/home/debian/.hermes-prod",
                },
                clear=False,
            ),
        ):
            resolved = process_mod.resolve_hermes_antigravity_home()
            self.assertEqual(resolved, Path("/home/debian/.hermes-prod/antigravity-oauth"))

        # 2. Windows AppData\Local\hermes discovery when directory exists
        with tempfile.TemporaryDirectory() as fake_win_home:
            win_hermes = Path(fake_win_home) / "AppData" / "Local" / "hermes"
            win_hermes.mkdir(parents=True)
            with patch.dict(os.environ, {"ANTIGRAVITY_HERMES_HOME": ""}, clear=False):
                resolved_win = process_mod.resolve_hermes_antigravity_home(Path(fake_win_home))
                self.assertEqual(resolved_win, win_hermes / "antigravity-oauth")

    def test_resolve_antigravity_binary_on_debian_and_windows(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home:
            local_bin_agy = Path(fake_home) / ".local" / "bin" / "agy"
            local_bin_agy.parent.mkdir(parents=True)
            local_bin_agy.write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                patch.object(process_mod.sys, "platform", "linux"),
                patch("shutil.which", return_value=None),
                patch.object(process_mod, "_posix_account_homes", return_value=[Path(fake_home)]),
                patch.dict(os.environ, {"ANTIGRAVITY_BINARY": "", "AGY_CLI_PATH": ""}, clear=False),
            ):
                self.assertEqual(resolve_antigravity_binary(), str(local_bin_agy))

            # AGY_CLI_PATH override
            custom_agy = Path(fake_home) / "custom-agy"
            custom_agy.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(os.environ, {"ANTIGRAVITY_BINARY": "", "AGY_CLI_PATH": str(custom_agy)}, clear=False):
                self.assertEqual(resolve_antigravity_binary(), str(custom_agy))

    def test_linux_child_env_includes_fallback_posix_path(self) -> None:
        isolated = Path("/tmp/fake_iso_linux")
        with (
            patch.object(process_mod.sys, "platform", "linux"),
            patch.dict(os.environ, {"PATH": "/custom/bin"}, clear=True),
        ):
            env = build_clean_child_env(isolated)
            path_entries = env["PATH"].split(os.pathsep)
            self.assertIn("/custom/bin", path_entries)
            self.assertIn("/usr/local/bin", path_entries)
            self.assertIn("/usr/bin", path_entries)
            self.assertIn("/bin", path_entries)

    def test_sync_updated_isolated_tokens_persists_refreshed_oauth_token(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as ws:
            hermes_gemini = Path(fake_home) / ".hermes" / "antigravity-oauth" / ".gemini" / "antigravity-cli"
            hermes_gemini.mkdir(parents=True)
            source_token = hermes_gemini / "antigravity-oauth-token"
            source_token.write_text('{"access_token":"old_expired_token"}', encoding="utf-8")
            old_time = 1_700_000_000.0
            os.utime(source_token, (old_time, old_time))

            iso_gemini = Path(ws) / "home" / ".gemini" / "antigravity-cli"
            iso_gemini.mkdir(parents=True)
            refreshed_token = iso_gemini / "antigravity-oauth-token"
            refreshed_token.write_text('{"access_token":"new_refreshed_token"}', encoding="utf-8")
            new_time = old_time + 3600.0
            os.utime(refreshed_token, (new_time, new_time))

            with patch.dict(os.environ, {"ANTIGRAVITY_CONFIG_DIR": "", "ANTIGRAVITY_HERMES_HOME": ""}, clear=False):
                process_mod.sync_updated_isolated_tokens(iso_gemini, home_dir=Path(fake_home))
                self.assertEqual(
                    source_token.read_text(encoding="utf-8"),
                    '{"access_token":"new_refreshed_token"}',
                )


if __name__ == "__main__":
    unittest.main()
