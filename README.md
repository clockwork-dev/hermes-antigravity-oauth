# Hermes Antigravity OAuth Plugin (`antigravity-oauth`)

[![Hermes Compatibility](https://img.shields.io/badge/Hermes%20Agent-%3E%3D0.21.4-blue.svg)](https://github.com/nousresearch/hermes-agent)
[![Platform Support](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows%20%7C%20macOS-green.svg)](#installation)
[![Security](https://img.shields.io/badge/Security-Zero--Exfiltration-brightgreen.svg)](#security--architecture)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A high-performance, cross-platform model-provider plugin for **[Hermes Agent](https://github.com/nousresearch/hermes-agent)** that connects your **Google Antigravity** subscription (Gemini 4 Argon, Gemini 3.8 Flash, Claude 5.5, GPT-OSS) via official OAuth through the `agy` CLI.

---

## 🚀 Key Advantages Over Generic SDKs

- **⚡ Zero Cold-Start Latency**: Keeps a persistent background `stream-json` worker process alive across conversation turns, eliminating the ~2s cold-start overhead of repeated CLI invocations.
- **🎯 Full Reasoning Effort Support**: Directly integrated with Hermes's interactive reasoning effort picker (`low`, `medium`, `high`) without duplicate model variants or catalog clutter.
- **🔒 Zero-Exfiltration Security**: The plugin **never reads, copies, or uploads** your secret OAuth tokens into Python memory. Tokens remain managed by the official binary inside an isolated sandbox.
- **👤 Multi-Account Isolation**: Uses a dedicated profile directory (`~/.hermes/antigravity-oauth/`), allowing Hermes to run on a dedicated Google account without disturbing your personal `agy` CLI or Antigravity IDE configuration.
- **🛡️ Native Tool Interception**: Intercepts any attempted host-level tool execution from `agy` and routes it through Hermes's permission and sandboxing system.
- **🔄 Cross-Platform Ready**: Tested out of the box on **Windows 11** (PowerShell), **Debian / Ubuntu Linux** (headless SSH, Proxmox LXC, systemd), and **macOS**.

---

## 📋 Prerequisites

Before installing the plugin, ensure you have:

1. **Hermes Agent** installed and running:
   ```bash
   hermes --version
   ```
2. **Google Antigravity CLI (`agy`)** installed:
   - **Linux / macOS**:
     ```bash
     curl -fsSL https://antigravity.google/cli/install.sh | bash
     ```
   - **Windows**: Download the installer from [antigravity.google/download](https://antigravity.google/download).
   - Verify installation:
     ```bash
     agy --version
     ```

---

## 📦 Installation

### Option A: Linux / Debian / Ubuntu (Headless Server, SSH, Proxmox LXC)

1. **Clone the plugin repository** into your Hermes plugins directory:
   ```bash
   # Under the hermes user (e.g. su - hermes)
   mkdir -p ~/.hermes/plugins
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
   ```

2. **Validate the plugin**:
   ```bash
   hermes plugins validate ~/.hermes/plugins/antigravity-oauth
   ```
   *(Expected output: `Validation passed.`)*

3. **Authenticate your Google account**:
   ```bash
   hermes auth add antigravity-oauth
   ```
   *Follow the on-screen URL in your browser and approve the login.*

4. **Restart Hermes background services** (if running):
   ```bash
   # If running the web dashboard via systemd:
   systemctl --user restart hermes-dashboard.service

   # Or if running the gateway service:
   hermes gateway restart
   ```

---

### Option B: Windows (PowerShell)

1. **Clone or copy the plugin** to your local Hermes directory:
   ```powershell
   $pluginsDir = "$env:USERPROFILE\.hermes\plugins"
   New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null
   git clone https://github.com/clockwork-dev/hermes-antigravity-oauth.git "$pluginsDir\antigravity-oauth"
   ```

2. **Validate the plugin**:
   ```powershell
   hermes plugins validate "$env:USERPROFILE\.hermes\plugins\antigravity-oauth"
   ```

3. **Authenticate your Google account**:
   ```powershell
   hermes auth add antigravity-oauth
   ```

4. **Restart Hermes background gateway** (if active):
   ```powershell
   hermes gateway restart
   ```

---

## 🎮 Usage

### 1. Interactive Model Selection

Launch Hermes's interactive model picker:
```bash
hermes model
```
1. Select **`Google Antigravity OAuth`** from the provider list.
2. Select your desired model (e.g. `gemini-4-argon` or `gemini-3.8-flash`).
3. Select your desired reasoning effort (`low`, `medium`, `high`).

### 2. Single-Line Command Execution

Run a quick query directly via CLI:
```bash
hermes chat -q "Explain quantum computing in 3 sentences" --provider antigravity-oauth -m gemini-3.8-flash
```

### 3. Check Authentication Status

Check your current connection and binary detection:
```bash
hermes auth status antigravity-oauth
```

---

## 🤖 Supported Models Catalog

| Model ID | Provider Family | Reasoning Effort | Description |
| :--- | :--- | :--- | :--- |
| **`gemini-4-argon`** | Google Gemini 4 | `low`, `medium`, `high` | Flagship reasoning & deep thinking model |
| **`gemini-3.8-flash`** *(Default)* | Google Gemini 3.8 | `low`, `medium`, `high` | Fast, low-latency, balanced daily model |
| **`claude-sonnet-5-5`** | Anthropic Claude 5.5 | `low`, `medium`, `high` | Top-tier coding, architecture, and tool use |
| **`claude-opus-5-5`** | Anthropic Claude 5.5 | `low`, `medium`, `high` | Heavy reasoning, complex research & mathematics |
| **`gpt-oss-120b`** | GPT-OSS | `medium` | Open-weight foundation model |

### Automatic Aliases & Legacy Compatibility
The plugin automatically routes common aliases and older model IDs to active equivalents:
- `gemini-pro`, `gemini-3.1-pro` ➡️ `gemini-4-argon`
- `gemini-flash` ➡️ `gemini-3.8-flash`
- `claude-sonnet`, `claude-sonnet-4-6` ➡️ `claude-sonnet-5-5`
- `claude-opus`, `claude-opus-4-6-thinking` ➡️ `claude-opus-5-5`
- `gpt-oss` ➡️ `gpt-oss-120b`

---

## 🛠️ Troubleshooting & FAQ

<details>
<summary><b>Q: I get "Unknown provider 'antigravity-oauth'" when chatting or in Dashboard</b></summary>
<br>
Hermes caches provider registrations in RAM when its daemon boots. If Hermes Gateway or Dashboard was already running before you installed the plugin, restart it:

```bash
# On Linux:
systemctl --user restart hermes-dashboard.service
# or:
hermes gateway restart
```
</details>

<details>
<summary><b>Q: Git clone fails with "Permission denied (publickey)"</b></summary>
<br>
If the repository is private, your server needs read access. Either:
1. Add a server SSH Deploy Key (`ssh-keygen -t ed25519` ➡️ GitHub Repo Settings ➡️ Deploy Keys).
2. Or clone with a Personal Access Token:
   ```bash
   git clone https://<GITHUB_TOKEN>@github.com/clockwork-dev/hermes-antigravity-oauth.git ~/.hermes/plugins/antigravity-oauth
   ```
</details>

<details>
<summary><b>Q: `agy: command not found` during auth or chat</b></summary>
<br>
Ensure the Antigravity CLI binary is located in your system `PATH` (typically `~/.local/bin/agy` or `C:\Users\<user>\AppData\Local\agy\bin\agy.exe`).
You can also set an explicit environment variable:
```bash
export ANTIGRAVITY_BINARY=/path/to/agy
```
</details>

---

## 🧪 Running Unit & Integration Tests

The plugin includes a full test suite with 23 hermetic tests:

```bash
# Run tests with pytest
pytest tests/ -v
```

All tests run in isolated temporary environments and mock OS processes cleanly.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
Derived and enhanced from the original `antigravity-subscription-directsdk` community project.
