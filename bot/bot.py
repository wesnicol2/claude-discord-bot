"""
Claude Discord Bot – Step 2
Listens to a single authorised user in a single channel, pipes their
messages to the Claude Code CLI, and returns the response.

Security notes:
  • Runs as non-root UID 1000 (node) inside a read-only container.
  • Only the configured user/channel combination is processed.
  • Claude is invoked non-interactively via --print mode.
  • Subprocess uses exec (no shell), so message text cannot inject commands.
  • One Claude session at a time (asyncio.Lock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import discord
from discord.ext import commands

# ─── Configuration ─────────────────────────────────────────────────────────────

def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set.")
    return value


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _set_env(name: str) -> set[int]:
    """Parse a comma-separated list of integer IDs from an env var."""
    raw = os.environ.get(name, "").strip()
    return {int(v.strip()) for v in raw.split(",") if v.strip().isdigit()}


DISCORD_TOKEN      = _required_env("DISCORD_TOKEN")
ALLOWED_USER_IDS   = _set_env("DISCORD_ALLOWED_USERS")
ALLOWED_CHANNEL_ID = _int_env("DISCORD_CHANNEL_ID", 0)

CLAUDE_TIMEOUT      = _int_env("CLAUDE_TIMEOUT", 600)       # seconds (10 min)
HEARTBEAT_INTERVAL  = _int_env("HEARTBEAT_INTERVAL", 120)   # seconds (2 min)
WORKSPACE_PATH      = os.environ.get("WORKSPACE_PATH", "/workspace")
LOG_PATH            = os.environ.get("LOG_PATH", "/app/logs")
CONFIG_PATH         = os.environ.get("CONFIG_PATH", "/config")

DISCORD_HARD_LIMIT  = 2000   # Discord's enforced limit
DISCORD_CHUNK_SIZE  = 1900   # our safe limit (leaves room for formatting)
CODE_BLOCK_OVERHEAD = 8      # len("```\n\n```")

# ─── Logging ───────────────────────────────────────────────────────────────────

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.append(logging.FileHandler(Path(LOG_PATH) / "bot.log"))
except OSError:
    pass  # LOG_PATH may not exist in test environments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("claude-bot")

# ─── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True          # Privileged intent – must be enabled in portal
bot = commands.Bot(command_prefix="!", intents=intents)

# Serialise Claude sessions: only one active request at a time
_claude_lock = asyncio.Lock()

# ─── Allowed-tools config ──────────────────────────────────────────────────────

def load_allowed_tools() -> list[str]:
    """Read the tool allowlist from config/allowed-tools.json at call time."""
    cfg = Path(CONFIG_PATH) / "allowed-tools.json"
    try:
        data = json.loads(cfg.read_text())
        tools = data.get("allowedTools", [])
        if isinstance(tools, list):
            return [str(t) for t in tools if t]
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load allowed-tools.json: %s", exc)
    return []

# ─── Response chunking ─────────────────────────────────────────────────────────

def chunk_text(text: str, max_len: int = DISCORD_CHUNK_SIZE) -> list[str]:
    """
    Split *text* into a list of strings each no longer than *max_len* chars.
    Prefers splitting on newline boundaries to avoid breaking code mid-line.
    """
    if not text:
        return ["(no output)"]

    chunks: list[str] = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    if text:
        chunks.append(text)

    return chunks


async def send_chunked(
    channel: discord.abc.Messageable,
    text: str,
    total_parts: int | None = None,
) -> None:
    """Send *text* to *channel*, wrapping in code blocks and splitting as needed."""
    chunks = chunk_text(text)
    n = len(chunks)

    for i, chunk in enumerate(chunks, start=1):
        part_suffix = f" — part {i}/{n}" if n > 1 else ""
        header = f"**Claude{part_suffix}:**\n"

        # Use a fenced code block when content is multi-line or long
        if "\n" in chunk or len(chunk) > 80:
            # Ensure the whole message fits within Discord's hard limit
            max_inner = DISCORD_HARD_LIMIT - len(header) - CODE_BLOCK_OVERHEAD
            inner = chunk[:max_inner]
            body = f"```\n{inner}\n```"
        else:
            body = chunk

        await channel.send(header + body)

# ─── Claude invocation ─────────────────────────────────────────────────────────

async def invoke_claude(message_text: str, channel: discord.abc.Messageable) -> None:
    """Public entry point – queues behind the global lock."""
    async with _claude_lock:
        await _invoke_claude_locked(message_text, channel)


async def _invoke_claude_locked(
    message_text: str,
    channel: discord.abc.Messageable,
) -> None:
    """
    Runs Claude Code CLI non-interactively.

    • Message is written to Claude's stdin (avoids any argument-injection risk).
    • stdout/stderr are streamed line-by-line; last_activity is updated on each line.
    • A heartbeat coroutine fires every HEARTBEAT_INTERVAL seconds to post
      a "still working" update and enforce the hard CLAUDE_TIMEOUT.
    """
    allowed_tools = load_allowed_tools()

    # --no-session-persistence: each Discord message is an independent session;
    # prevents cross-message state leakage while keeping CLAUDE.md auto-discovery
    # and OAuth credential loading intact (--bare would disable both).
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        "--no-session-persistence",
    ]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]

    # Auth: copy OAuth credentials to /tmp/.claude/ so Claude Code can find them.
    # HOME=/tmp gives Claude a clean writable scratch space. We explicitly unset
    # ANTHROPIC_API_KEY so Claude uses OAuth (Pro sub) rather than a pay-per-use key.
    # CLAUDE.md is loaded via --append-system-prompt-file since auto-discovery
    # looks in ~/. claude/ (=/tmp/.claude/) which won't have CLAUDE.md at runtime.
    import shutil as _shutil
    _claude_tmp = Path("/tmp/.claude")
    _claude_tmp.mkdir(parents=True, exist_ok=True)
    _creds_src = Path("/home/node/.claude/.credentials.json")
    if _creds_src.exists():
        _shutil.copy2(_creds_src, _claude_tmp / ".credentials.json")
        (_claude_tmp / ".credentials.json").chmod(0o600)

    claude_md = Path("/home/node/.claude/CLAUDE.md")
    if claude_md.exists():
        cmd += ["--append-system-prompt-file", str(claude_md)]

    env = {
        **os.environ,
        "HOME":             "/tmp",
        "NO_COLOR":         "1",
        "TERM":             "dumb",
        "PYTHONUNBUFFERED": "1",
    }
    # Remove API key so Claude Code falls back to OAuth credentials
    env.pop("ANTHROPIC_API_KEY", None)

    logger.info(
        "Invoking Claude | msg_len=%d | tools=%s",
        len(message_text),
        allowed_tools or "default",
    )

    # ── Spawn process ─────────────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_PATH,
            env=env,
            limit=4 * 1024 * 1024,   # 4 MiB stream buffer
        )
    except FileNotFoundError:
        await channel.send("**Error:** `claude` CLI not found. Check the Docker image.")
        logger.error("claude binary not found in PATH")
        return
    except PermissionError as exc:
        await channel.send(f"**Error:** Cannot execute `claude`: {exc}")
        return

    # ── Feed message via stdin ────────────────────────────────────────────────
    assert proc.stdin is not None
    try:
        proc.stdin.write(message_text.encode("utf-8"))
        await proc.stdin.drain()
    finally:
        proc.stdin.close()

    # ── Shared state for heartbeat ────────────────────────────────────────────
    start_time        = time.monotonic()
    last_activity_ref = [time.monotonic()]   # mutable container so coroutines can update it
    timed_out         = False
    stdout_lines: list[bytes] = []
    stderr_lines: list[bytes] = []

    # ── Stream readers ────────────────────────────────────────────────────────
    async def drain(stream: asyncio.StreamReader, buf: list[bytes]) -> None:
        async for line in stream:
            buf.append(line)
            last_activity_ref[0] = time.monotonic()

    # ── Heartbeat + timeout enforcer ──────────────────────────────────────────
    async def heartbeat() -> None:
        nonlocal timed_out
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            elapsed = time.monotonic() - start_time

            if elapsed >= CLAUDE_TIMEOUT:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await channel.send(
                    f"**Timeout:** Claude did not finish within {CLAUDE_TIMEOUT}s. "
                    "Request cancelled."
                )
                return

            idle = time.monotonic() - last_activity_ref[0]
            if idle >= HEARTBEAT_INTERVAL - 5:
                await channel.send(f"*Still working… ({int(elapsed)}s elapsed)*")

    # ── Run concurrently ──────────────────────────────────────────────────────
    assert proc.stdout is not None
    assert proc.stderr is not None

    hb_task = asyncio.create_task(heartbeat())
    try:
        await asyncio.gather(
            drain(proc.stdout, stdout_lines),
            drain(proc.stderr, stderr_lines),
        )
        # Give the process a moment to exit cleanly
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

    if timed_out:
        return   # Message already sent in heartbeat()

    # ── Collect output ────────────────────────────────────────────────────────
    stdout = b"".join(stdout_lines).decode("utf-8", errors="replace").strip()
    stderr = b"".join(stderr_lines).decode("utf-8", errors="replace").strip()
    rc     = proc.returncode
    elapsed = time.monotonic() - start_time

    logger.info(
        "Claude exited | rc=%d | stdout=%d bytes | stderr=%d bytes | elapsed=%.1fs",
        rc, len(stdout), len(stderr), elapsed,
    )

    # ── Handle errors ─────────────────────────────────────────────────────────
    if rc != 0 and not stdout:
        error_body = stderr or f"Claude exited with code {rc} and produced no output."
        await channel.send(
            f"**Claude error (exit {rc}):**\n```\n{error_body[:1800]}\n```"
        )
        return

    if stderr:
        logger.warning("Claude stderr (rc=%d): %.400s", rc, stderr)

    # ── Send response ─────────────────────────────────────────────────────────
    response = stdout or stderr or f"(Claude exited {rc} with no output)"
    await send_chunked(channel, response)

# ─── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    assert bot.user
    logger.info(
        "Bot ready | tag=%s | id=%s | channel=%d | allowed_users=%s",
        bot.user,
        bot.user.id,
        ALLOWED_CHANNEL_ID,
        ALLOWED_USER_IDS,
    )
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for your messages",
        )
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    # Always ignore our own messages
    if message.author == bot.user:
        return

    # ── Security gate ─────────────────────────────────────────────────────────
    if message.author.id not in ALLOWED_USER_IDS:
        # Silent drop – do not reveal the bot is listening
        return
    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    content = message.content.strip()
    if not content:
        return

    logger.info(
        "Processing | user=%s (%d) | channel=%d | preview=%.120r",
        message.author,
        message.author.id,
        message.channel.id,
        content,
    )

    # Acknowledge receipt with a clock reaction
    try:
        await message.add_reaction("⏳")
    except discord.HTTPException:
        pass

    try:
        await invoke_claude(content, message.channel)
    except Exception as exc:
        logger.exception("Unhandled exception in invoke_claude: %s", exc)
        try:
            await message.channel.send(f"**Internal error:** {exc}")
        except discord.HTTPException:
            pass
    finally:
        # Remove the clock reaction regardless of outcome
        try:
            await message.remove_reaction("⏳", bot.user)
        except Exception:
            pass

    # Allow prefix commands to work too (currently none defined beyond "!")
    await bot.process_commands(message)


@bot.event
async def on_error(event: str, *args, **kwargs) -> None:
    logger.exception("Unhandled Discord event error in %s", event)


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if ALLOWED_CHANNEL_ID == 0:
        logger.error("DISCORD_CHANNEL_ID is not set – refusing to start.")
        sys.exit(1)
    if not ALLOWED_USER_IDS:
        logger.error("DISCORD_ALLOWED_USERS is not set – refusing to start.")
        sys.exit(1)

    logger.info(
        "Starting Claude Discord Bot | channel=%d | allowed_users=%s",
        ALLOWED_CHANNEL_ID,
        ALLOWED_USER_IDS,
    )
    bot.run(DISCORD_TOKEN, log_handler=None)   # We manage logging ourselves
