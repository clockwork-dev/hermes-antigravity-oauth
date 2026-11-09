# Hermes Antigravity OAuth Provider (`antigravity-oauth`)

A zero-exfiltration, high-performance `model-provider` plugin for **Hermes Agent** that routes your **Google Antigravity** personal, Pro, or Ultra subscription via OAuth through the official `agy` CLI binary.

## Key Features

- **Zero-Exfiltration OAuth Verification**: Verifies Google Antigravity OAuth login across token files (`jetski-standalone-oauth-token`, `antigravity-oauth-token`), Windows Credential Manager (`cmdkey`), macOS Login Keychain (`security`), and Linux Secret Service (`secret-tool` / `secretstorage`) **without ever reading or printing the secret token in Python memory**.
- **Isolated Subprocess `HOME` & Strict Environment Sanitization**: Spawns `agy` inside a mode-`0700` isolated `HOME` linking only the active OAuth token (or macOS Keychains directory) and an empty `SOUL.md`, while stripping third-party API keys, dynamic loader variables, and untrusted proxy settings.
- **Persistent Bi-Directional `stream-json` Worker + Multi-Turn `--conversation` Delta Persistence**: Keeps a warm `agy --input-format stream-json --output-format stream-json` worker for instant turn latency, falls back to oneshot execution for concurrent subagents, and sends only delta messages on multi-turn sessions.
- **Native Hermes CLI & Desktop Integration**: Implements `auth_handler` (`hermes auth status|add|refresh|logout antigravity-oauth`), `setup_status` (inline `agy acp --login` in `hermes model`), `discover_models`, `external_auth_evidence`, `classify_api_error`, `fetch_account_usage` (`/usage`), and `cancel()` for clean cross-thread turn interruption.

## Installation

Copy or symlink this directory into your Hermes plugins folder:

```bash
mkdir -p ~/.hermes/plugins/model-providers
cp -r hermes-antigravity-oauth ~/.hermes/plugins/model-providers/antigravity-oauth
```

Authenticate and select a model:

```bash
hermes auth add antigravity-oauth
hermes model
```

Validate the plugin at any time:

```bash
hermes plugins validate ~/.hermes/plugins/model-providers/antigravity-oauth
```

## Status: What Works & Known Limitations

### Working Features
- **Full Hermes v0.23+ Provider Contract**: Passes all 15 `hermes plugins validate` checks (`0 warnings`, `0 failures`, `security scan: safe`) and all 19 unit/integration tests.
- **Official `agy` `stream-json` Wire Protocol (`{"event": ...}`)**: Sends `{"event": "user", "message": {"content": ...}}` over `stdin` and parses `init`, `step_update`, and `result` events on `stdout`.
- **Reasoning Effort & Dynamic Model Catalog**: Supports `build_api_kwargs_extras(reasoning_config=...)`, dynamic `agy models` discovery with TTL caching, and cross-version model aliases (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.1-pro`, `claude-sonnet-4-6`/`5-5`, `claude-opus-4-6-thinking`/`5-5`, `gpt-oss-120b`).
- **Progressive `<tool_call>` Streaming & Native Tool Interception**: Buffers and emits OpenAI-compatible `tool_calls` chunks while immediately terminating the child if `agy` attempts to invoke its own built-in tools.
- **Isolated Hermes OAuth Profile (`hermes auth add antigravity-oauth`)**: Stores Hermes-specific OAuth tokens in `<HERMES_HOME>/antigravity-oauth/.gemini/antigravity-cli/` using file-backed token mode (`USERPROFILE`/`HOME` + `SSH_CONNECTION` keyring bypass) so Hermes can use a separate Google account from the host's global `agy` CLI.

### Current Limitations / Known Issues (TODO)
- **Inline `hermes setup model` Login Gate**: When not logged in at all, Hermes's built-in `_external_process_login_gate` invokes `login_command` (`agy acp --login`) directly without custom environment variables, which writes to the global OS keyring (`gemini:antigravity` in Windows Credential Manager). To log into a separate Google account dedicated to Hermes without touching the global `agy` CLI account, run `hermes auth add antigravity-oauth` explicitly.
- **Windows `agy` Cold-Start Language Server Overhead**: In oneshot fallback mode (`_spawn_oneshot`), `agy.EXE` starts a local language server per invocation (~1.5–2s startup). The primary persistent worker (`_ensure_worker`) avoids this after the first turn, but concurrent subagents still incur cold-start overhead.
- **Native Multimodal Binary Input**: Vision attachments are bridged via temporary local file paths referenced in the prompt rather than raw base64 frames over `stream-json`.
