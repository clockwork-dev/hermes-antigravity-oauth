# Hermes Antigravity OAuth Provider (`antigravity-oauth`)

A cross-platform, zero-exfiltration, high-performance `model-provider` plugin for **Hermes Agent** (Windows, Debian/Ubuntu Linux, and macOS) that routes your **Google Antigravity** personal, Pro, or Ultra subscription via OAuth through the official `agy` CLI binary.

## Key Features

- **Full Cross-Platform Support (Debian Linux + Windows + macOS)**:
  - **Linux (`systemd` & Headless SSH)**: Honors `HERMES_HOME` and `HERMES_DATA_DIR_SUFFIX`, resolves `agy` across `~/.local/bin/agy`, `~/.antigravity/bin/agy`, `/usr/local/bin/agy`, `/usr/bin/agy`, and `/opt/Antigravity/bin/agy` even under minimal `systemd` service `PATH`s, and uses `start_new_session=True` + `os.killpg` process-group lifecycle management.
  - **Windows**: Resolves `agy.exe` across `%LOCALAPPDATA%\agy\bin\agy.exe` and `%LOCALAPPDATA%\Programs\Antigravity\bin\agy.exe`, uses `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, and supports Windows Credential Manager (`cmdkey`) alongside isolated file-backed tokens.
- **Zero-Exfiltration OAuth Verification**: Verifies Google Antigravity OAuth login across token files (`jetski-standalone-oauth-token`, `antigravity-oauth-token`), Windows Credential Manager (`cmdkey`), macOS Login Keychain (`security`), and Linux Secret Service (`secret-tool` / `secretstorage`) **without ever reading or printing the secret token in Python memory**.
- **Isolated Hermes OAuth Profile (`hermes auth add antigravity-oauth`)**: Stores Hermes-specific OAuth tokens in `<HERMES_HOME>/antigravity-oauth/.gemini/antigravity-cli/` using file-backed token mode (`HOME` + `USERPROFILE` + `SSH_CONNECTION` keyring bypass) and automatically syncs refreshed tokens back to disk. This lets Hermes use a dedicated Google account without overwriting the host's global `agy` CLI account, and works out-of-the-box on headless Debian servers without requiring `gnome-keyring` or D-Bus.
- **Persistent Bi-Directional `stream-json` Worker + Multi-Turn `--conversation` Delta Persistence**: Keeps a warm `agy --input-format stream-json --output-format stream-json` worker for instant turn latency, falls back to oneshot execution for concurrent subagents, and sends only delta messages on multi-turn sessions.

## Installation

### 1. Debian / Ubuntu Linux (Server / SSH / `systemd`)

1. Ensure the official Antigravity CLI (`agy`) is installed on the server (from https://antigravity.google/download — typically placed in `~/.local/bin/agy` or `/usr/bin/agy`).
2. Copy or clone this plugin into `~/.hermes/plugins/antigravity-oauth` (or `$HERMES_HOME/plugins/antigravity-oauth`):

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth"
cp -r ./* "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth/"
```

3. Validate the plugin and authenticate over SSH (paste the browser OAuth callback URL/code when prompted):

```bash
hermes plugins validate "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth"
hermes auth add antigravity-oauth
hermes model
```

4. If running Hermes Gateway as a `systemd` service, restart it so the provider loads:

```bash
hermes gateway restart
```

### 2. Windows (PowerShell)

1. Copy the plugin into `%LOCALAPPDATA%\hermes\plugins\antigravity-oauth`:

```powershell
$ dest = "$env:LOCALAPPDATA\hermes\plugins\antigravity-oauth"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path .\__init__.py, .\client.py, .\process.py, .\stream.py, .\prompt.py, .\models.py, .\usage.py, .\delivery.py, .\debug.py, .\plugin.yaml, .\pyproject.toml, .\README.md -Destination $dest -Force
```

2. Validate, sign in with your Hermes-dedicated Google account, and restart the gateway:

```powershell
hermes plugins validate "$env:LOCALAPPDATA\hermes\plugins\antigravity-oauth"
hermes auth add antigravity-oauth
hermes gateway restart
```

## Status: What Works & Known Limitations

### Working Features
- **Full Hermes v0.23+ Provider Contract**: Passes all 15 `hermes plugins validate` checks (`0 warnings`, `0 failures`, `security scan: safe`) and all unit/integration tests on Python 3.11–3.14.
- **Official `agy` `stream-json` Wire Protocol (`{"event": ...}`)**: Sends `{"event": "user", "message": {"content": ...}}` over `stdin` and parses `init`, `step_update`, and `result` events on `stdout`.
- **Reasoning Effort & Dynamic Model Catalog**: Supports `build_api_kwargs_extras(reasoning_config=...)`, dynamic `agy models` discovery with TTL caching, and cross-version model aliases (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.1-pro`, `claude-sonnet-4-6`/`5-5`, `claude-opus-4-6-thinking`/`5-5`, `gpt-oss-120b`).
- **Progressive `<tool_call>` Streaming & Native Tool Interception**: Buffers and emits OpenAI-compatible `tool_calls` chunks while immediately terminating the child if `agy` attempts to invoke its own built-in tools.
- **Automatic OAuth Token Refresh Persistence**: Syncs refreshed token files from isolated worker workspaces back to `<HERMES_HOME>/antigravity-oauth` across filesystems (`tmpfs` `/tmp` -> `/home` on Debian, or copied tokens on Windows).

### Current Limitations / Known Issues (TODO)
- **Inline `hermes setup model` Login Gate**: When not logged in at all, Hermes's built-in `_external_process_login_gate` invokes `login_command` (`agy acp --login`) directly without custom environment variables, which uses the global OS keyring if available. Always run `hermes auth add antigravity-oauth` first to use an isolated Hermes account or on headless Linux servers.
- **`agy` Cold-Start Language Server Overhead in Oneshot Mode**: In oneshot fallback mode (`_spawn_oneshot`), `agy` starts a local language server per invocation (~1.5–2s startup). The primary persistent worker (`_ensure_worker`) avoids this after the first turn, but concurrent subagents still incur cold-start overhead.
- **Native Multimodal Binary Input**: Vision attachments are bridged via temporary local file paths referenced in the prompt rather than raw base64 frames over `stream-json`.
