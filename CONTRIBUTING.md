# Contributing to Claude Discord Bot

Thank you for your interest in contributing. This document covers how to get set up, what we look for in pull requests, and our development conventions.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Security Guidelines](#security-guidelines)
- [Pull Request Process](#pull-request-process)
- [Reporting Vulnerabilities](#reporting-vulnerabilities)

---

## Getting Started

1. Fork the repository and clone your fork.
2. Create a feature branch from `main`: `git checkout -b feat/your-feature`
3. Make your changes, commit, and open a pull request.

For substantial changes (new features, architectural decisions), open an issue first to discuss the approach before writing code.

---

## Development Setup

### Requirements

- Docker 27+ with Compose v2
- `bash` (test suite)
- A Discord application and Anthropic API key (for end-to-end testing)

### First-time setup

```bash
# Copy and fill in your credentials
cp .env.example secrets/.env

# Create runtime directories
mkdir -p workspace logs
chown 1000:1000 workspace logs

# Build the image
docker compose build

# Start the container
docker compose up -d

# Run the security test suite — all 19 tests must pass
bash scripts/test-security.sh
```

---

## Making Changes

### Commit style

We use the [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short summary>

[optional body]
```

Common types: `feat`, `fix`, `docs`, `chore`, `test`, `refactor`, `security`

Examples:
```
feat(bot): add /ask slash command
fix(docker): correct workspace permissions on fresh clone
security(compose): replace seccomp:unconfined with custom profile
docs(readme): add Unraid Community Apps setup section
```

### Test requirements

- The security test suite (`scripts/test-security.sh`) must pass 19/19 before any PR is merged.
- If you add a new security control, add a corresponding test to the suite.
- If you add bot functionality (Step 2+), include unit tests alongside the feature code.

### What we do NOT accept

- PRs that disable security controls without a documented, justified replacement
- Hardcoded secrets or API keys of any kind
- Changes that require the container to run as root
- Adding host-path mounts beyond the four defined in `docker-compose.yml`

---

## Security Guidelines

This project is security-first. When contributing:

- **Least privilege**: request only the permissions your feature actually needs.
- **No new writable mounts**: the sandbox (`/workspace`) is the only place code should land.
- **Validate inputs**: anything from Discord is untrusted user input.
- **Allowlist over denylist**: prefer explicit tool allowlists in `allowed-tools.json`.
- **No secrets in logs**: scrub API keys and tokens before any log statement.

---

## Pull Request Process

1. Ensure `scripts/test-security.sh` passes locally.
2. Update `README.md` if your change affects setup, configuration, or architecture.
3. Add or update `CHANGELOG.md` entries under `[Unreleased]`.
4. Request a review from a maintainer.
5. PRs require at least one approving review before merge.
6. Squash commits on merge is preferred for a clean history.

---

## Reporting Vulnerabilities

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues by opening a [GitHub Security Advisory](../../security/advisories/new) in this repository. We aim to respond within 48 hours and will coordinate a fix and disclosure timeline with you.
