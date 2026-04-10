# Claude Discord Bot

A self-hosted Discord bot that pipes messages to [Claude Code CLI](https://github.com/anthropics/claude-code), running inside a hardened Docker container on Unraid (or any Linux host). Claude Code handles the heavy lifting — code generation, file operations, multi-step reasoning — while the bot manages Discord I/O and enforces security boundaries.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-27%2B-2496ED?logo=docker)](https://www.docker.com/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-CLI-blueviolet)](https://github.com/anthropics/claude-code)

---

## Features

- **Claude Code as the brain** — full tool use, file editing, bash execution (allowlist-controlled), and multi-turn reasoning
- **Secure by default** — non-root container, read-only root filesystem, all Linux capabilities dropped, no new privileges
- **Isolated sandbox** — Claude Code can only read/write `/workspace`; host filesystem is never exposed
- **Resource-bounded** — CPU, memory, and PID limits enforced at the container level
- **Network-isolated** — inter-container communication disabled; only outbound internet (Discord + Anthropic APIs)
- **Unraid-native** — directory layout matches Unraid's `/mnt/user/appdata/` convention; compatible with Community Apps

---

## Architecture

```
Discord User
     │  (message)
     ▼
Discord Gateway (WebSocket)
     │
     ▼
┌─────────────────────────────────────────────┐
│  Docker Container  (non-root, read-only fs) │
│                                             │
│  Bot Process  ──►  Claude Code CLI          │
│                         │                  │
│                         ▼                  │
│                   /workspace  (sandbox)     │
└─────────────────────────────────────────────┘
     │  (response)
     ▼
Discord User
```

---

## Security Model

| Layer | Control |
|-------|---------|
| Process user | UID/GID 1000 (`node`) — never root |
| Root filesystem | `read_only: true` — immutable OS layer |
| Writable paths | `/workspace` (sandbox), `/app/logs`, `/tmp` (tmpfs) only |
| Config paths | `/config`, `/home/node/.claude` — read-only bind mounts |
| Linux capabilities | All dropped (`cap_drop: ALL`) |
| Privilege escalation | `no-new-privileges: true` |
| Resource limits | 1 vCPU · 512 MiB RAM · 100 PIDs |
| Network | Isolated bridge; ICC disabled; outbound-only |
| Init process | `tini` as PID 1 — clean signal handling |

---

## Prerequisites

- **Docker** 27+ with Compose v2
- **Unraid** 6.12+ (or any Linux host with Docker)
- An **Anthropic API key** (for Claude Code)
- A **Discord application** with a bot token

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/claude-discord-bot.git
cd claude-discord-bot
```

### 2. Create the required directories

```bash
mkdir -p workspace logs secrets
chown 1000:1000 workspace logs
```

### 3. Configure environment variables

```bash
cp .env.example secrets/.env
# Edit secrets/.env with your API keys
```

### 4. Build and start

```bash
docker compose up -d --build
```

### 5. Verify security posture

```bash
bash scripts/test-security.sh
```

All 19 security tests should pass before you go further.

---

## Directory Layout

```
claude-discord-bot/
├── Dockerfile                 # Multi-stage hardened image
├── docker-compose.yml         # Full security configuration
├── .env.example               # Environment variable template
├── config/
│   └── allowed-tools.json     # Claude Code tool allowlist
├── claude-home/
│   └── settings.json          # Claude Code global settings
├── bot/                       # Bot source code (added in Step 2)
├── scripts/
│   ├── entrypoint.sh          # Container init + mount validation
│   └── test-security.sh       # 19-test security suite
├── workspace/                 # Claude Code sandbox (gitignored)
├── logs/                      # Bot logs (gitignored)
└── secrets/                   # .env lives here (gitignored)
```

---

## Configuration Reference

All runtime configuration is via environment variables in `secrets/.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude Code |
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `DISCORD_CLIENT_ID` | Yes | Discord application/client ID |
| `DISCORD_ALLOWED_GUILDS` | Recommended | Comma-separated guild IDs to restrict bot access |
| `DISCORD_ALLOWED_USERS` | Recommended | Comma-separated user IDs allowed to query the bot |
| `MAX_TOKENS` | No | Max tokens per Claude Code response (default: 4096) |
| `CLAUDE_TIMEOUT` | No | Timeout in seconds per invocation (default: 120) |

---

## Claude Code Tool Allowlist

By default (`config/allowed-tools.json`) all tools are denied. Edit this file to enable specific Claude Code tools for your use case:

```json
{
  "allowedTools": ["Read", "Glob", "Grep"],
  "deniedTools": ["Bash", "computer"]
}
```

Full tool reference: [Claude Code documentation](https://docs.anthropic.com/en/docs/claude-code)

---

## Roadmap

- [x] **Step 1** — Docker infrastructure, security hardening, test suite
- [ ] **Step 2** — Discord bot code, Claude Code integration, slash commands
- [ ] **Step 3** — CLAUDE.md profiles, per-channel tool allowlists, custom seccomp profile
- [ ] **Step 4** — Unraid Community Apps template

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

---

## License

[MIT](LICENSE) — see the license file for details.

---

## Disclaimer

This project is not affiliated with Anthropic or Discord. Use of Claude Code is subject to [Anthropic's usage policies](https://www.anthropic.com/legal/usage-policy). You are responsible for ensuring your deployment complies with Discord's [Terms of Service](https://discord.com/terms) and [Developer Policy](https://discord.com/developers/docs/policies-and-agreements/developer-policy).
