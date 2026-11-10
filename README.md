# hermes-antigravity-oauth

<p align="left">
  <a href="https://github.com/nousresearch/hermes-agent"><img src="https://img.shields.io/badge/Hermes_Agent-%3E%3D0.21.4-1f6feb?style=flat-square" alt="Hermes Agent"></a>
  <a href="#supported-models"><img src="https://img.shields.io/badge/Models-Gemini_4_Argon_%7C_Claude_5.5-8957e5?style=flat-square" alt="Models"></a>
  <a href="#prerequisites"><img src="https://img.shields.io/badge/CLI-agy_1.5%2B-2ea44f?style=flat-square" alt="Antigravity CLI"></a>
  <a href="#security--isolation"><img src="https://img.shields.io/badge/Security-Zero--Exfiltration-0969da?style=flat-square" alt="Security"></a>
  <a href="#installation"><img src="https://img.shields.io/badge/Platform-Linux_%7C_Windows_%7C_macOS-6e7781?style=flat-square" alt="Platform"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-gray?style=flat-square" alt="License"></a>
</p>

A high-performance `model-provider` plugin for **[Hermes Agent](https://github.com/nousresearch/hermes-agent)** that connects your **Google Antigravity** subscription (Gemini 4 Argon, Gemini 3.8 Flash, Claude Sonnet 5.5, Claude Opus 5.5, GPT-OSS 120B) via official OAuth through Google's `agy` CLI binary.

---

## 🏛️ Architecture

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

## 🤖 Supported Models

| Model | Provider Family | Reasoning Effort | Context Window | Best For |
| :--- | :--- | :--- | :--- | :--- |
| **`gemini-4-argon`** | Google Gemini 4 | `low`, `medium`, `high` | 1,000,000 | Deep thinking, complex reasoning, mathematics |
| **`gemini-3.8-flash`** *(Default)* | Google Gemini 3.8 | `low`, `medium`, `high` | 1,000,000 | Daily coding, rapid iteration, chat |
| **`claude-sonnet-5-5`** | Anthropic Claude 5.5 | `low`, `medium`, `high` | 200,000 | Architecture, heavy refactoring, tool calling |
| **`claude-opus-5-5`** | Anthropic Claude 5.5 | `low`, `medium`, `high` | 200,000 | Deep analysis, scientific research |
| **`gpt-oss-120b`** | Open-Weight MoE | `medium` | 128,000 | Open foundation model workflows |

> [!TIP]
> **Automatic Aliases & Fallbacks**: Shorthand and legacy names are routed automatically:
> - `gemini-pro`, `gemini-3.1-pro` ➔ `gemini-4-argon`
> - `gemini-flash` ➔ `gemini-3.8-flash`
> - `claude-sonnet`, `claude-sonnet-4-6` ➔ `claude-sonnet-5-5`
> - `claude-opus`, `claude-opus-4-6-thinking` ➔ `claude-opus-5-5`
> - `gpt-oss` ➔ `gpt-oss-120b`

---

## ⚡ Prerequisites

1. **Hermes Agent** v0.21.4 or newer installed:
   ```bash
   hermes --version
   ```

2. **Google Antigravity CLI (`agy`)**:
   - **Linux / macOS**:
     ```bash
     curl -fsSL https://antigravity.google/cli/install.sh -o install.sh
     bash install.sh && rm install.sh
     ```
   - **Windows**: Download the official installer from [antigravity.google/download](https://antigravity.google/download).
   - Verify `agy` is on your `PATH`:
     ```bash
     agy --version
     ```

---

## 📦 Installation & Setup

### For Linux / Debian / Ubuntu (Bare-metal, VPS, Proxmox LXC)

1. **Clone the plugin repository**:
   ```bash
   # Under your hermes user (e.g. su - hermes)
   mkdir -p ~/.hermes/plugins
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
   ```

2. **Validate the plugin integrity**:
   ```bash
   hermes plugins validate ~/.hermes/plugins/antigravity-oauth
   ```
   *(Must report: `Validation passed.`)*

3. **Link your Google Antigravity account**:
   ```bash
   hermes auth add antigravity-oauth
   ```
   *Open the generated Google OAuth URL in your browser, approve access, and paste the confirmation code back into the terminal.*

4. **Restart Hermes background services**:
   ```bash
   # If running the web dashboard:
   systemctl --user restart hermes-dashboard.service

   # Or if running the gateway daemon:
   hermes gateway restart
   ```

---

### For Windows (PowerShell)

1. **Clone the plugin**:
   ```powershell
   $dir = "$env:USERPROFILE\.hermes\plugins\antigravity-oauth"
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git $dir
   ```

2. **Validate the plugin**:
   ```powershell
   hermes plugins validate "$env:USERPROFILE\.hermes\plugins\antigravity-oauth"
   ```

3. **Authenticate via OAuth**:
   ```powershell
   hermes auth add antigravity-oauth
   ```

4. **Restart gateway service** (if running):
   ```powershell
   hermes gateway restart
   ```

---

## 🚀 How to Use

### 1. Interactive Model Picker
Run Hermes's built-in model selector:
```bash
hermes model
```
Select **Google Antigravity OAuth** from the list, choose your model, and pick the reasoning effort (`low`, `medium`, or `high`).

### 2. Single-Command Query
Run a quick query from your shell:
```bash
hermes chat -q "Explain quantum key distribution in 2 sentences" --provider antigravity-oauth -m gemini-3.8-flash
```

Or with customized reasoning:
```bash
hermes chat -q "Solve this algorithm problem" --provider antigravity-oauth -m gemini-4-argon --reasoning-effort high
```

### 3. Check Connection & Quota
Verify your authentication status and binary location:
```bash
hermes auth status antigravity-oauth
```

---

## 🔒 Security & Isolation

- **Zero-Exfiltration Architecture**: Python runtime memory never holds Google OAuth refresh tokens or access keys. The plugin delegates credential storage to `agy`'s isolated file storage.
- **Dedicated Hermes Account**: Credentials are kept in `~/.hermes/antigravity-oauth/`, leaving your personal desktop `agy` CLI and Antigravity IDE accounts completely untouched.
- **Strict Environment Sanitization**: Strips dangerous dynamic loader overrides (`LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`), injection flags (`PYTHONPATH`), external tokens, and proxy variables before spawning child processes.
- **Native Tool Guard**: If `agy` attempts to invoke built-in host tools outside the session, the plugin immediately halts the process and routes the tool request through Hermes's permission sandbox.

---

## ⚖️ Comparison with `antigravity-subscription-directsdk`

| Feature | `antigravity-subscription-directsdk` | `hermes-antigravity-oauth` |
| :--- | :--- | :--- |
| **Worker Architecture** | Oneshot process per turn (2s cold start) | Warm background `stream-json` worker pool |
| **Catalog Maintenance** | Deprecated models (`gemini-3.1-pro`, `claude-4-6`) | Active families (`gemini-4-argon`, `claude-5-5`) |
| **Reasoning Effort** | Cluttered duplicate rows (`-low`, `-medium`) | Native Hermes effort picker integration |
| **Credential Storage** | Shared host `~/.gemini` or OS keyring | Dedicated profile (`~/.hermes/antigravity-oauth/`) |
| **Cross-Platform** | Tested on macOS/Linux only | Tested on Windows 11 & Debian 13 (`systemd`) |
| **Test Coverage** | Minimal | 23 unit/integration tests with hermetic mocks |

---

## ❓ Frequently Asked Questions

> [!NOTE]
> **Q: Hermes says: `Unknown provider 'antigravity-oauth'`**  
> **A:** When Hermes starts in daemon mode (gateway or web dashboard), it caches known providers in RAM. Restart the background service to reload the provider registry:
> ```bash
> systemctl --user restart hermes-dashboard.service   # Linux
> hermes gateway restart                              # Windows / CLI
> ```

> [!NOTE]
> **Q: Git clone fails with `Permission denied (publickey)`**  
> **A:** For private repositories, generate an SSH key on your server (`ssh-keygen -t ed25519`) and add it to your GitHub **Settings ➔ Deploy Keys**, or clone via HTTPS with a personal access token:
> ```bash
> git clone https://<GITHUB_TOKEN>@github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
> ```

> [!NOTE]
> **Q: `agy: command not found`**  
> **A:** Ensure the `agy` binary directory (`~/.local/bin` on Linux or `%LOCALAPPDATA%\agy\bin` on Windows) is included in your shell's `PATH`. You can also set `export ANTIGRAVITY_BINARY=/path/to/agy`.

---

## 🧪 Testing

Run the automated test suite:
```bash
pytest tests/ -v
```

---

## 📄 License

Distributed under the [MIT License](LICENSE).
