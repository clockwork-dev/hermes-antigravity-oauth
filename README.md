# hermes-antigravity-oauth

A model-provider plugin for [Nous Research Hermes Agent](https://github.com/nousresearch/hermes-agent) that enables inference through an active **Google Antigravity** subscription using the official `agy` CLI binary.

Supports **Gemini 4 Argon**, **Gemini 3.8 Flash**, **Claude Sonnet 5.5**, **Claude Opus 5.5**, and **GPT-OSS 120B** on Linux, Windows, and macOS.

```
┌────────────────────────────────────────────────────────┐
│                   Hermes Agent Core                    │
│   (Session Worker, Tool Router, Approvals Gate)        │
└──────────────────────────┬─────────────────────────────┘
                           │ ProviderProfile IPC
                           ▼
┌────────────────────────────────────────────────────────┐
│        hermes-antigravity-oauth (Provider Plugin)      │
│   - Token Isolation (~/.hermes/antigravity-oauth/)     │
│   - Persistent stream-json Worker Lifecycle            │
│   - Environment Sanitizer (Zero-Exfiltration)          │
└──────────────────────────┬─────────────────────────────┘
                           │ stdin / stdout (JSON lines)
                           ▼
┌────────────────────────────────────────────────────────┐
│             Google Antigravity CLI (agy)               │
│   - Local Language Server & Model Transport            │
│   - OAuth Session Custody                              │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTPS / gRPC
                           ▼
┌────────────────────────────────────────────────────────┐
│               Google Antigravity Service               │
│   (Gemini 4 Argon · Gemini 3.8 Flash · Claude 5.5)     │
└────────────────────────────────────────────────────────┘
```

---

## Technical Characteristics

- **Warm Worker Pool**: Maintains a persistent background `stream-json` process over `stdin`/`stdout`. Subsequent turns avoid language-server initialization latency (~1.5–2.5s per turn in standard oneshot implementations).
- **Reasoning Effort Integration**: Maps Hermes's native `/model` reasoning effort (`low`, `medium`, `high`) to underlying model capabilities via `build_api_kwargs_extras` and `supported_reasoning_efforts`. No duplicated `-low`/`-medium` pseudo-models in the picker catalog.
- **Zero-Exfiltration Isolation**: Secrets are never loaded into Python runtime memory. The plugin points `agy` to an isolated profile root (`~/.hermes/antigravity-oauth/`) via symlinked or permission-restricted file descriptors, leaving host keyring entries intact.
- **Strict Environment Sanitization**: Strips dynamic loader variables (`LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`), external script paths (`PYTHONPATH`), API keys, and external proxies before spawning child CLI processes.
- **Native Tool Sandboxing**: Monitors execution for host tool invocations from `agy`. If the CLI attempts to run host-level operations directly, the call is intercepted, terminated, and translated into Hermes tool requests subject to user permissions.

---

## Comparison with `antigravity-subscription-directsdk`

| Feature | `antigravity-subscription-directsdk` | `hermes-antigravity-oauth` (This Plugin) |
| :--- | :--- | :--- |
| **Execution Architecture** | One-shot process per prompt (high latency) | Persistent warm `stream-json` worker with mutex locking |
| **Model Catalog** | Static legacy list (`gemini-3.1-pro`, `claude-4-6`) | Dynamic `agy models` discovery + modern defaults (`gemini-4-argon`, `claude-5-5`) |
| **Reasoning Effort UX** | Cluttered picker (`model-low`, `model-medium`) | Clean base names + native Hermes reasoning selector |
| **Profile Isolation** | Uses host global `~/.gemini` or OS keyring | Dedicated profile (`~/.hermes/antigravity-oauth/`), multi-account ready |
| **Token Refresh Sync** | Manual / session-bound | Automatic bidirectional sync across isolated workspaces |
| **Cross-Platform** | Primarily POSIX / macOS tested | Windows 11 (`CREATE_NO_WINDOW`, `%LOCALAPPDATA%`) and Debian/Ubuntu (`systemd`) tested |
| **Validation Suite** | Minimal test coverage | 23 unit/integration tests; passes `hermes plugins validate` with 0 warnings |

---

## Prerequisites

1. **Hermes Agent** v0.21.4 or later:
   ```bash
   hermes --version
   ```

2. **Google Antigravity CLI (`agy`)**:
   - **Linux / macOS**:
     ```bash
     curl -fsSL https://antigravity.google/cli/install.sh -o install.sh
     bash install.sh && rm install.sh
     ```
   - **Windows**: Download the binary installer from [antigravity.google/download](https://antigravity.google/download).

Verify that `agy` is present in your `PATH`:
```bash
agy --version
```

---

## Installation

### Linux / Debian / Ubuntu (Headless Server, Proxmox LXC, systemd)

1. Clone into the Hermes user plugins directory:
   ```bash
   mkdir -p ~/.hermes/plugins
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
   ```

2. Verify plugin manifest and security boundaries:
   ```bash
   hermes plugins validate ~/.hermes/plugins/antigravity-oauth
   ```

3. Authenticate with Google Antigravity OAuth:
   ```bash
   hermes auth add antigravity-oauth
   ```
   Follow the authorization URL in a browser and paste the returned verification token into the terminal.

4. If running background services (e.g., Hermes Web Dashboard or Gateway), reload them to register the new provider:
   ```bash
   systemctl --user restart hermes-dashboard.service
   # or:
   hermes gateway restart
   ```

---

### Windows (PowerShell)

1. Clone or copy to your user profile:
   ```powershell
   $dir = "$env:USERPROFILE\.hermes\plugins\antigravity-oauth"
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git $dir
   ```

2. Validate:
   ```powershell
   hermes plugins validate "$env:USERPROFILE\.hermes\plugins\antigravity-oauth"
   ```

3. Authenticate:
   ```powershell
   hermes auth add antigravity-oauth
   ```

4. If running a background gateway daemon:
   ```powershell
   hermes gateway restart
   ```

---

## Usage

### Interactive Selection

Run the model switcher:
```bash
hermes model
```
Select **Google Antigravity OAuth**, choose the base model, and configure reasoning effort.

### Non-Interactive / CLI

```bash
hermes chat -q "Summarize the commit history" --provider antigravity-oauth -m gemini-3.8-flash
```

Specify reasoning effort:
```bash
hermes chat -q "Prove this theorem" --provider antigravity-oauth -m gemini-4-argon --reasoning-effort high
```

### Inspect Provider Status

```bash
hermes auth status antigravity-oauth
```

---

## Model Catalog & Aliases

The plugin dynamically queries `agy models` at runtime with a thread-safe 1-hour cache. The fallback catalog reflects active Google Antigravity tiers:

| Model Identifier | Upstream Architecture | Supported Reasoning | Context Window |
| :--- | :--- | :--- | :--- |
| `gemini-4-argon` | Google Gemini 4 | `low`, `medium`, `high` | 1,000,000 |
| `gemini-3.8-flash` *(Default)* | Google Gemini 3.8 | `low`, `medium`, `high` | 1,000,000 |
| `claude-sonnet-5-5` | Anthropic Claude 5.5 | `low`, `medium`, `high` | 200,000 |
| `claude-opus-5-5` | Anthropic Claude 5.5 | `low`, `medium`, `high` | 200,000 |
| `gpt-oss-120b` | Open-Weight Mixture of Experts | `medium` | 128,000 |

### Backward-Compatibility Aliases

Requests referencing legacy or shorthand names are resolved transparently:
- `gemini-pro`, `gemini-3.1-pro` ➔ `gemini-4-argon`
- `gemini-flash` ➔ `gemini-3.8-flash`
- `claude-sonnet`, `claude-sonnet-4-6` ➔ `claude-sonnet-5-5`
- `claude-opus`, `claude-opus-4-6-thinking` ➔ `claude-opus-5-5`
- `gpt-oss` ➔ `gpt-oss-120b`

---

## Troubleshooting

### `AuthError: Unknown provider 'antigravity-oauth'`
Occurs when a persistent daemon (`hermes gateway` or `hermes-dashboard.service`) was started prior to plugin installation. The daemon caches provider registries at boot time.
- **Fix**: Restart the daemon:
  ```bash
  systemctl --user restart hermes-dashboard.service
  # or:
  hermes gateway restart
  ```

### `git@github.com: Permission denied (publickey)`
Private repository access requires SSH keys or a personal access token on headless servers.
- **Fix (Deploy Key)**: Run `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519`, print `cat ~/.ssh/id_ed25519.pub`, and register it in GitHub repository **Settings ➔ Deploy Keys**.
- **Fix (HTTPS Token)**: Clone using a scoped Personal Access Token:
  ```bash
  git clone https://<GITHUB_TOKEN>@github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
  ```

### `agy: command not found`
The executable is missing from standard `PATH` directories.
- **Fix**: Ensure `~/.local/bin` (Linux) or `%LOCALAPPDATA%\agy\bin` (Windows) is exported in shell initialization files (`~/.bashrc`, `~/.profile`), or define the binary explicitly:
  ```bash
  export ANTIGRAVITY_BINARY=/usr/local/bin/agy
  ```

---

## Verification & Testing

The test suite covers process lifecycle, stream framing, signal handling, token rotation, and error classification:

```bash
python -m pytest tests/ -v
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
