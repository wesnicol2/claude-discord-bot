# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Step 2: Discord bot source code and Claude Code integration
- Step 3: CLAUDE.md profiles and custom seccomp profile
- Step 4: Unraid Community Apps template

---

## [0.1.0] — 2025-04-09

### Added
- Multi-stage `Dockerfile` using `node:20-alpine`; hardened runtime stage runs as UID/GID 1000 (`node` user)
- `docker-compose.yml` with full security configuration:
  - Read-only root filesystem
  - `no-new-privileges: true`
  - All Linux capabilities dropped (`cap_drop: ALL`)
  - Resource limits: 1 vCPU, 512 MiB RAM, 100 PIDs
  - Isolated bridge network with ICC disabled
  - tmpfs mounts for `/tmp` and `/run`
- `tini` as PID 1 for correct signal handling and zombie reaping
- Claude Code CLI v2.1.98 pre-installed in the image
- Bind mounts: `/workspace` (rw sandbox), `/app/logs` (rw), `/config` (ro), `/home/node/.claude` (ro)
- `config/allowed-tools.json` — Claude Code tool allowlist skeleton
- `claude-home/settings.json` — Claude Code global settings placeholder
- `scripts/entrypoint.sh` — container init with mount validation and root-user guard
- `scripts/test-security.sh` — 19-test security suite covering all hardening controls
- `.env.example` — environment variable template for Step 2
- `CONTRIBUTING.md`, `LICENSE` (MIT), `CHANGELOG.md`
- 19/19 security tests passing on Unraid OS 7.2 / Docker 27.5.1
