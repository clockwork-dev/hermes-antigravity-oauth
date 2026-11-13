# AGENTS.md — Development Guidelines for AI Agents

This document contains mission-critical architectural rules, contracts, and invariants for AI models/agents modifying or extending the `antigravity-oauth` plugin for Hermes Agent.

---

## 1. Project Purpose & Core Architecture

`antigravity-oauth` is an official-standard `model-provider` plugin for **Hermes Agent** (`>=0.21.4`, verified on `v0.23.0`). It bridges Google Antigravity OAuth subscriptions (Personal, Pro, Ultra) into Hermes Agent using the official local `agy` CLI binary.

### High-Level Architecture
```
Hermes Agent Core (run_agent / Gateway / CLI)
     │
     ▼
ProviderProfile (`AntigravityOAuthProfile` in __init__.py)
     │ creates
     ▼
SDK Client (`AntigravityClient` in client.py)
     │
     ├─► Persistent Worker Subprocess (`_ensure_worker`)
     │   `agy --input-format stream-json --output-format stream-json --model <id> [--effort <low|medium|high|xhigh|max>]`
     │   (Held via `_worker_lock`; turns piped via stdin as NDJSON: `{"event": "user", ...}`)
     │
     └─► Oneshot Subprocess (`_spawn_oneshot`)
         `agy --output-format stream-json --model <id> [--effort <low|medium|high|xhigh|max>] [--conversation <id>]`
         (Used for concurrent subagent turns or `--conversation` resumes)
```

---

## 2. Strict Invariants (DO NOT BREAK)

### A. Zero Secret Exfiltration
- **Never read or log raw secret tokens into Python memory from OS keyrings**:
  - **Windows**: Probe via `cmdkey /list:gemini:antigravity` (target metadata only).
  - **macOS**: Probe via `/usr/bin/security find-generic-password -s gemini -a antigravity` (no `-w`/`-g`; `stdout=DEVNULL`).
  - **Linux**: Probe via `secret-tool search --all service gemini username antigravity` with `stdout=subprocess.DEVNULL` (inspect `stderr` only).
- When token files exist (`jetski-standalone-oauth-token` or `antigravity-oauth-token`), check existence and size, never print the contents to stdout or logs.

### B. Isolated Hermes Account (`SSH_CONNECTION` Bypass)
- Hermes-specific auth must reside in `<hermes_home>/antigravity-oauth/.gemini/antigravity-cli/` so it **never** overwrites the user's primary `agy` account in the global OS keyring.
- `agy` checks `SSH_CONNECTION`. When `SSH_CONNECTION` is non-empty, `agy` switches to file-based token storage and headless copy-paste OAuth login.
- Always forward `SSH_CONNECTION="127.0.0.1 0 127.0.0.1 22"` during `hermes auth add antigravity-oauth` and when launching workers from an isolated profile.

### C. Refreshed Token Write-Back (`sync_updated_isolated_tokens`)
- Workers execute inside isolated temporary directories (`<temp_cwd>/home`).
- When `agy` refreshes an expired OAuth `access_token`, it writes to `<temp_cwd>/home/.gemini/antigravity-cli/`.
- On stream completion and worker exit, any updated, non-symlink token file must be synced back to the persistent Hermes directory (`0600` permissions) to prevent token loss across worker reboots.

### D. Provider Registration & Dataclass Contract
- Hermes does **not** call `register(ctx)` on `kind: model-provider` plugins.
- Registration occurs at import time in `__init__.py` via `providers.register_provider(profile)`.
- `ProviderProfile` in Hermes Core is a `@dataclass`. Its fields (`auth_handler`, `classify_api_error`, `setup_reasoning_efforts`) default to `None`. Always bind them in `AntigravityOAuthProfile.__init__()` after `super().__init__()`.
- Set both `HERMES_SKIP_TRANSPORT_WRAP = True` and `HERMES_SKIP_ASYNC_WRAP = True` on both the profile and client.

### E. Cross-Platform Compatibility (Debian Linux + Windows + macOS)
- **Linux (`systemd`)**: Honor `HERMES_HOME` (and `HERMES_DATA_DIR_SUFFIX`). In `build_clean_child_env`, ensure `~/.local/bin`, `/usr/local/bin`, `/usr/bin`, `/bin` are present in `PATH`.
- **Process Groups**: On Windows, use `creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`. On POSIX, use `start_new_session = True` and terminate via `os.killpg(pgid, signal.SIGTERM/SIGKILL)`.
- **Python Compatibility**: Python 3.11–3.14. In `_force_rmtree()`, use `onexc` on Python `>= 3.12` and `onerror` on Python `< 3.12`. Never `chmod` a symlink or multi-hardlinked file (`st_nlink > 1`).

---

## 3. Subprocess Wire Protocol (`stream-json` v1.5+)

### Input (`stdin` in Persistent Mode)
```json
{"event": "user", "message": {"content": "<prompt_text>"}}
```
*(Must be a single line terminated with `\n`)*

### Output (`stdout` NDJSON Events)
1. **`event: "init"`** (or legacy `type: "system"`): Extract `session_id` / `conversation_id`.
2. **`event: "step_update"`**:
   - `type: "thinking"` -> Emit as `ChatCompletionChunk(delta={"reasoning_content": ...})`.
   - `type: "text"` -> Emit text deltas or buffer for `<tool_call>` extraction.
   - `type: "tool"` | `"command"` | `"mcp"` -> **Native tool attempt by `agy`**. Immediately kill child process and end turn cleanly so Hermes executes the tool call instead.
3. **`event: "result"`**: Extract stop reason and token usage. Subtract `_prev_worker_usage` to calculate delta turn usage.

---

## 4. Error Classification Map (`_classify_error`)

When `agy` emits error messages or exits non-zero, classify them for Hermes failover:
- `"exceeded the output token limit"`, `"previous response was cut off"` -> `reason: "server_error"`, `retryable: true`, `should_compress: false`
- `"max_trajectory_tokens"`, `"subscriber fell behind"`, `"prompt is too long"` -> `reason: "context_overflow"`, `retryable: true`, `should_compress: true`
- `"ai credits balance is too low"`, `"insufficient credits"` -> `reason: "billing"`, `retryable: false`, `should_fallback: true`
- `"verify your account"`, `"oauth token expired"`, `"invalid_grant"` -> `reason: "auth_permanent"`, `retryable: false`, `should_fallback: true`
- `"resource_exhausted"`, `"429 too many requests"`, `"exhausted your capacity"` -> `reason: "rate_limit"`, `retryable: true`, `should_fallback: true`

---

## 5. Development & Verification Workflow

Always verify changes with both the unit test suite and the Hermes plugin validator before committing or deploying:

1. **Run Unit Tests (`pytest`)**:
   ```bash
   pytest tests -v
   ```
   *(Must achieve 100% pass rate across all 23 tests)*

2. **Run Hermes Plugin Admission Validator**:
   ```bash
   hermes plugins validate /path/to/hermes-antigravity-oauth
   ```
   *(Requires 0 warnings, 0 failures, security scan: safe)*

3. **Deploy to Local / Server Hermes**:
   - Windows: `%LOCALAPPDATA%\hermes\plugins\antigravity-oauth`
   - Linux: `${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth`
   - Restart gateway: `hermes gateway restart`
