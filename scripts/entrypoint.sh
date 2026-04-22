#!/bin/sh
# entrypoint.sh – Step 2: launch the Claude Discord Bot
set -eu

echo "[entrypoint] Container started as uid=$(id -u) gid=$(id -g) user=$(id -un)"

# ── Validate required mounts ───────────────────────────────────────────────────
for dir in /mnt/user /app/logs /config /home/node/.claude; do
  if [ -d "$dir" ]; then
    echo "[entrypoint] Mount OK: $dir"
  else
    echo "[entrypoint] ERROR: expected mount missing: $dir" >&2
    exit 1
  fi
done

# ── Validate claude CLI is present ────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
  echo "[entrypoint] ERROR: claude CLI not found in PATH" >&2
  exit 1
fi
echo "[entrypoint] Claude CLI: $(claude --version 2>&1 | head -1)"

# ── Validate Python is present ────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
  echo "[entrypoint] ERROR: python3 not found in PATH" >&2
  exit 1
fi
echo "[entrypoint] Python: $(python3 --version)"

# ── Validate required env vars ────────────────────────────────────────────────
for var in DISCORD_TOKEN DISCORD_ALLOWED_USERS DISCORD_CHANNEL_ID; do
  eval "val=\${${var}:-}"
  if [ -z "$val" ]; then
    echo "[entrypoint] ERROR: required environment variable $var is not set" >&2
    exit 1
  fi
  echo "[entrypoint] Env OK: $var (set)"
done

# ── Ensure .claude.json exists for node user (doesn't persist across restarts) ─
if [ ! -f "/home/node/.claude.json" ]; then
  latest_backup=$(ls -t /home/node/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1)
  if [ -n "$latest_backup" ]; then
    cp "$latest_backup" "/home/node/.claude.json"
    echo "[entrypoint] Restored .claude.json from backup: $latest_backup"
  else
    echo '{}' > /home/node/.claude.json
    echo "[entrypoint] Created empty .claude.json (no backup found)"
  fi
  chown node:node /home/node/.claude.json
else
  echo "[entrypoint] .claude.json OK"
fi

# ── Fix ownership so the node user (claude subprocess) can read/write claude files
chown -R node:node /home/node/.claude
echo "[entrypoint] Set /home/node/.claude ownership to node"

# Auth: prefer Claude Pro OAuth credentials; fall back to API key
if [ -f "/home/node/.claude/.credentials.json" ]; then
  echo "[entrypoint] Auth: Claude Pro OAuth credentials found"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "[entrypoint] Auth: using ANTHROPIC_API_KEY"
else
  echo "[entrypoint] ERROR: no auth — need either /home/node/.claude/.credentials.json or ANTHROPIC_API_KEY" >&2
  exit 1
fi

echo "[entrypoint] All checks passed. Starting Discord bot..."
exec python3 /app/bot.py
