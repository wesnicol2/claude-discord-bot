#!/bin/sh
# entrypoint.sh – Step 1 placeholder
# In Step 2 this is replaced with the actual Discord bot launcher.
# Current purpose: validate the runtime environment and keep the container alive
# so security tests can be run against it.
set -eu

echo "[entrypoint] Container started as uid=$(id -u) gid=$(id -g) user=$(id -un)"

# Sanity-check mounts
for dir in /workspace /app/logs /config /home/node/.claude; do
  if [ -d "$dir" ]; then
    echo "[entrypoint] Mount OK: $dir"
  else
    echo "[entrypoint] ERROR: expected mount missing: $dir" >&2
    exit 1
  fi
done

# Verify we are NOT root
if [ "$(id -u)" -eq 0 ]; then
  echo "[entrypoint] FATAL: running as root – refusing to start" >&2
  exit 1
fi

echo "[entrypoint] All checks passed. Waiting for Step 2 bot code..."
exec sleep infinity
