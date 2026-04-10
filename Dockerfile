################################################################################
# Stage 1 – dependency installer (runs as root to install global npm packages)
################################################################################
FROM node:20-alpine AS builder

# Install Claude Code CLI globally while we still have root
RUN npm install -g @anthropic-ai/claude-code@latest 2>&1 | tail -5

################################################################################
# Stage 2 – hardened runtime image
#
# node:20-alpine ships with a "node" user at UID=1000, GID=1000.
# We use it directly – no need to create a new user.
################################################################################
FROM node:20-alpine AS runtime

# ── Minimal OS tooling + Python ───────────────────────────────────────────────
RUN apk add --no-cache \
      tini \
      curl \
      bind-tools \
      python3 \
      py3-pip && \
    # Remove package manager cache
    rm -rf /var/cache/apk/*

# ── Copy the globally-installed Claude Code from the builder stage ────────────
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=builder /usr/local/bin/claude       /usr/local/bin/claude

# ── Create runtime directories with correct ownership ────────────────────────
# Mount points are created here so Docker can validate bind-mount targets.
# /config is owned by root so the container user (node/1000) cannot write
# even if the read_only: true flag were accidentally removed.
RUN mkdir -p \
      /app/logs \
      /workspace \
      /config    \
      /home/node/.claude && \
    chown -R node:node \
      /app \
      /workspace \
      /home/node/.claude && \
    chown -R root:root /config && \
    chmod 755 /config

WORKDIR /app

# ── Install Python dependencies (as root, before dropping privileges) ─────────
COPY bot/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# ── Copy bot source code ──────────────────────────────────────────────────────
COPY --chown=node:node bot/ /app/

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY --chown=node:node scripts/entrypoint.sh /entrypoint.sh
RUN chmod 550 /entrypoint.sh

# ── Python runtime flags ──────────────────────────────────────────────────────
# PYTHONUNBUFFERED: flush stdout immediately (important for log streaming)
# PYTHONDONTWRITEBYTECODE: skip .pyc files (required for read-only filesystem)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Drop to non-root for all subsequent RUN and the default CMD ───────────────
USER node

# Expose no ports – the bot is outbound-only (Discord WebSocket + Anthropic API)
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
