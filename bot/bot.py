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
import pwd
import sys
import time
import urllib.request
import urllib.error
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

# ─── OAuth constants ────────────────────────────────────────────────────────────

CREDENTIALS_PATH  = Path("/home/node/.claude/.credentials.json")
OAUTH_CLIENT_ID   = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
TOKEN_ENDPOINT    = "https://api.anthropic.com/v1/oauth/token"
REFRESH_MARGIN_MS = 5 * 60 * 1000   # refresh if token expires within 5 minutes
REAUTH_TIMEOUT    = 300              # seconds to wait for user to sign in

# ─── Logging ───────────────────────────────────────────────────────────────────

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.append(logging.FileHandler(Path(LOG_PATH) / "bot.log"))
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("claude-bot")

# ─── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Serialise Claude sessions: only one active request at a time
_claude_lock = asyncio.Lock()

# When True, the next invocation starts a fresh session instead of continuing
_start_fresh: bool = False

# Held for the entire duration of a re-auth flow; prevents parallel flows
_auth_lock = asyncio.Lock()

# When set, the next incoming message is treated as an OAuth code
_pending_auth_code: asyncio.Future[str] | None = None

# ─── Privilege helpers ─────────────────────────────────────────────────────────

# Claude refuses --dangerously-skip-permissions / bypassPermissions when run as
# root, so the claude subprocess must drop to the unprivileged node user.
_node_pw = pwd.getpwnam("node")

def _drop_to_node() -> None:
    os.setgid(_node_pw.pw_gid)
    os.setuid(_node_pw.pw_uid)

# ─── Allowed-tools config ──────────────────────────────────────────────────────

def load_allowed_tools() -> list[str]:
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


async def send_chunked(channel: discord.abc.Messageable, text: str) -> None:
    chunks = chunk_text(text)
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        part_suffix = f" *(part {i}/{n})*" if n > 1 else ""
        await channel.send(chunk + part_suffix)

# ─── OAuth token management ─────────────────────────────────────────────────────

def _post_token(payload: dict) -> dict:
    """Synchronous token endpoint call — run in a thread executor."""
    # Log payload with secrets redacted
    safe = {k: (v[:8] + "…" if k in ("code", "refresh_token", "code_verifier") and v else v)
            for k, v in payload.items()}
    logger.info("[token] POST %s  body=%s", TOKEN_ENDPOINT, safe)

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            logger.info("[token] Response %d: %s", resp.status, body[:300])
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        logger.error("[token] HTTP %d: %s", exc.code, body[:500])
        raise


def _save_credentials(access_token: str, refresh_token: str, expires_in: int) -> None:
    expires_at_ms = int(time.time() * 1000 + expires_in * 1000)
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        creds = {}
    oauth = creds.get("claudeAiOauth", {})
    oauth["accessToken"]  = access_token
    oauth["refreshToken"] = refresh_token
    oauth["expiresAt"]    = expires_at_ms
    creds["claudeAiOauth"] = oauth
    CREDENTIALS_PATH.write_text(json.dumps(creds))
    logger.info("[token] Credentials saved — access token valid for %ds", expires_in)


async def _ensure_token_fresh() -> bool:
    """
    Check the stored OAuth access token and refresh it if it's about to expire.

    Returns True  if credentials are usable (fresh or successfully refreshed).
    Returns False if the refresh token is invalid and full re-auth is needed.
    """
    try:
        creds         = json.loads(CREDENTIALS_PATH.read_text())
        oauth         = creds.get("claudeAiOauth", {})
        refresh_token = oauth.get("refreshToken", "")
        expires_at_ms = oauth.get("expiresAt", 0)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[token] Could not read credentials: %s — re-auth needed", exc)
        return False

    now_ms      = time.time() * 1000
    remaining_s = (expires_at_ms - now_ms) / 1000

    if remaining_s > REFRESH_MARGIN_MS / 1000:
        return True   # token is fresh — nothing to do

    if not refresh_token:
        logger.warning("[token] Token expired and no refresh_token — re-auth needed")
        return False

    logger.info("[token] Token expires in %.0fs — refreshing", remaining_s)
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _post_token, {
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     OAUTH_CLIENT_ID,
        })
        _save_credentials(data["access_token"], data["refresh_token"], data["expires_in"])
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("[token] Refresh HTTP %d: %s", exc.code, body[:300])
        if exc.code in (400, 401):
            # invalid_grant — refresh token revoked, need full re-auth
            return False
        return True   # temporary server error; try with existing token anyway
    except Exception as exc:
        logger.error("[token] Refresh error: %s", exc)
        return True   # unknown error; try anyway rather than forcing re-auth


async def _handle_reauth(channel: discord.abc.Messageable) -> bool:
    """
    Run `claude auth login` as a subprocess to perform a full re-authentication.

    The CLI registers a server-side session with Anthropic and outputs a sign-in URL.
    After the user signs in, Anthropic's callback page displays a `code#state` string.
    The user pastes that into Discord; we feed it to the CLI's stdin to complete the
    token exchange.  We also race against the process exiting on its own in case the
    CLI completes via server-side polling.

    Returns True on success, False on failure/timeout.
    """
    global _pending_auth_code

    env = {
        **os.environ,
        "HOME":     "/home/node",
        "SHELL":    "/bin/bash",
        "NO_COLOR": "1",
        "TERM":     "dumb",
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            preexec_fn=_drop_to_node,
        )
    except FileNotFoundError:
        await channel.send("**Error:** `claude` CLI not found.")
        return False

    assert proc.stdout is not None
    assert proc.stdin is not None

    # Drain stdout continuously so the process never blocks on its pipe.
    url_ready: asyncio.Event = asyncio.Event()
    url_value: list[str] = []

    async def _read_output() -> None:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                logger.info("[auth] %s", line)
            if "https://" in line and not url_ready.is_set():
                url_value.append(line[line.find("https://"):].strip())
                url_ready.set()

    read_task = asyncio.create_task(_read_output())

    try:
        await asyncio.wait_for(url_ready.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        read_task.cancel()
        await channel.send("**Auth error:** No sign-in URL received within 30s. Check logs.")
        return False

    await channel.send(
        f"**Authentication required.**\n"
        f"Sign in at this link:\n{url_value[0]}\n\n"
        f"*After signing in, Anthropic will show you a code that looks like* `XXXXXXXX#YYYYYYYY`\n"
        f"*Copy the entire string and paste it here.*"
    )
    logger.info("[auth] Posted sign-in URL — waiting for code or process exit")

    loop = asyncio.get_running_loop()
    _pending_auth_code = loop.create_future()
    proc_done   = asyncio.ensure_future(proc.wait())
    code_pasted = asyncio.ensure_future(asyncio.shield(_pending_auth_code))

    try:
        done, _ = await asyncio.wait(
            {proc_done, code_pasted},
            timeout=REAUTH_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        _pending_auth_code = None

    # ── Timeout ───────────────────────────────────────────────────────────────
    if not done:
        proc.kill()
        await proc.wait()
        proc_done.cancel()
        code_pasted.cancel()
        read_task.cancel()
        logger.warning("[auth] Timed out waiting for sign-in")
        await channel.send("**Auth timed out.** Send any message to try again.")
        return False

    # ── Path A: CLI exited on its own (server-side polling completed) ─────────
    if proc_done in done:
        code_pasted.cancel()
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass
        rc = proc.returncode
        if rc == 0:
            logger.info("[auth] Completed via polling (no code needed)")
            return True
        logger.error("[auth] CLI exited %d before code was submitted", rc)
        await channel.send(f"**Authentication failed** (exit {rc}). Send any message to try again.")
        return False

    # ── Path B: user pasted the code ─────────────────────────────────────────
    try:
        pasted = code_pasted.result()
    except Exception as exc:
        logger.error("[auth] Could not read pasted code: %s", exc)
        proc.kill()
        await proc.wait()
        read_task.cancel()
        await channel.send("**Auth error.** Send any message to try again.")
        return False

    # Pass the full pasted string including #state — the CLI needs both parts
    full_input = pasted.strip()
    logger.info("[auth] Submitting code to CLI stdin (%d chars)", len(full_input))
    await channel.send("*Verifying…*")

    try:
        proc.stdin.write((full_input + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception as exc:
        logger.error("[auth] stdin write failed: %s", exc)
        proc.kill()
        await proc.wait()
        read_task.cancel()
        await channel.send("**Auth error:** Could not submit code. Send any message to try again.")
        return False

    # Wait generously — token exchange can take time on slow networks
    try:
        await asyncio.wait_for(proc.wait(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error("[auth] Timed out after code submission")
        await channel.send("**Auth timed out** after submitting the code. Send any message to try again.")
        return False
    finally:
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass

    rc = proc.returncode
    if rc == 0:
        logger.info("[auth] Completed after code submission")
        return True
    logger.error("[auth] CLI exited %d after code submission", rc)
    await channel.send(f"**Authentication failed** (exit {rc}). Send any message to try again.")
    return False

# ─── Claude invocation ─────────────────────────────────────────────────────────

async def invoke_claude(
    message_text: str,
    channel: discord.abc.Messageable,
    new_session: bool = False,
) -> None:
    """
    Public entry point.
    Ensures the token is fresh, then runs Claude.
    If the token cannot be refreshed, initiates a full re-auth flow first.
    """
    token_ok = await _ensure_token_fresh()

    if not token_ok:
        if _auth_lock.locked():
            return   # another message already triggered re-auth
        async with _auth_lock:
            success = await _handle_reauth(channel)
        if not success:
            return
        await channel.send("*Re-authenticated. Retrying your message…*")

    async with _claude_lock:
        await _invoke_claude_locked(message_text, channel, new_session=new_session)


async def _invoke_claude_locked(
    message_text: str,
    channel: discord.abc.Messageable,
    new_session: bool = False,
) -> None:
    """
    Runs Claude Code CLI non-interactively.
    Caller must hold _claude_lock.
    """
    allowed_tools = load_allowed_tools()

    cmd = ["claude", "--print", "--output-format", "text", "--dangerously-skip-permissions"]
    if not new_session:
        cmd.append("--continue")
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]

    env = {
        **os.environ,
        "HOME":             "/home/node",
        "SHELL":            "/bin/bash",
        "NO_COLOR":         "1",
        "TERM":             "dumb",
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("ANTHROPIC_API_KEY", None)

    logger.info("Invoking Claude | msg_len=%d | tools=%s", len(message_text), allowed_tools or "default")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_PATH,
            env=env,
            limit=4 * 1024 * 1024,
            preexec_fn=_drop_to_node,
        )
    except FileNotFoundError:
        await channel.send("**Error:** `claude` CLI not found. Check the Docker image.")
        logger.error("claude binary not found in PATH")
        return
    except PermissionError as exc:
        await channel.send(f"**Error:** Cannot execute `claude`: {exc}")
        return

    assert proc.stdin is not None
    try:
        proc.stdin.write(message_text.encode("utf-8"))
        await proc.stdin.drain()
    finally:
        proc.stdin.close()

    start_time        = time.monotonic()
    last_activity_ref = [time.monotonic()]
    timed_out         = False
    stdout_lines: list[bytes] = []
    stderr_lines: list[bytes] = []

    async def drain(stream: asyncio.StreamReader, buf: list[bytes]) -> None:
        async for line in stream:
            buf.append(line)
            last_activity_ref[0] = time.monotonic()

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
                    f"**Timeout:** Claude did not finish within {CLAUDE_TIMEOUT}s. Request cancelled."
                )
                return
            idle = time.monotonic() - last_activity_ref[0]
            if idle >= HEARTBEAT_INTERVAL - 5:
                await channel.send(f"*Still working… ({int(elapsed)}s elapsed)*")

    assert proc.stdout is not None
    assert proc.stderr is not None

    hb_task = asyncio.create_task(heartbeat())
    try:
        await asyncio.gather(
            drain(proc.stdout, stdout_lines),
            drain(proc.stderr, stderr_lines),
        )
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
        return

    stdout  = b"".join(stdout_lines).decode("utf-8", errors="replace").strip()
    stderr  = b"".join(stderr_lines).decode("utf-8", errors="replace").strip()
    rc      = proc.returncode
    elapsed = time.monotonic() - start_time

    logger.info(
        "Claude exited | rc=%d | stdout=%d bytes | stderr=%d bytes | elapsed=%.1fs",
        rc, len(stdout), len(stderr), elapsed,
    )

    if rc != 0 and not stdout:
        error_body = stderr or f"Claude exited with code {rc} and produced no output."
        await channel.send(f"**Claude error (exit {rc}):**\n```\n{error_body[:1800]}\n```")
        return

    if stderr:
        logger.warning("Claude stderr (rc=%d): %.400s", rc, stderr)

    response = stdout or stderr or f"(Claude exited {rc} with no output)"
    await send_chunked(channel, response)

# ─── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    assert bot.user
    logger.info(
        "Bot ready | tag=%s | id=%s | channel=%d | allowed_users=%s",
        bot.user, bot.user.id, ALLOWED_CHANNEL_ID, ALLOWED_USER_IDS,
    )
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="for your messages")
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    if message.author.id not in ALLOWED_USER_IDS:
        return
    if message.channel.id != ALLOWED_CHANNEL_ID:
        return

    global _start_fresh, _pending_auth_code

    content = message.content.strip()
    if not content:
        return

    # ── Auth code intercept ───────────────────────────────────────────────────
    if _pending_auth_code is not None and not _pending_auth_code.done():
        logger.info("[auth] Code received from Discord (%d chars)", len(content))
        _pending_auth_code.set_result(content)
        await message.channel.send("*Code received — exchanging for tokens…*")
        return

    # ── Auth in-progress guard ────────────────────────────────────────────────
    if _auth_lock.locked():
        await message.channel.send("*Authentication in progress — please wait.*")
        return

    # ── !new command ──────────────────────────────────────────────────────────
    if content.lower() == "!new":
        _start_fresh = True
        await message.channel.send("Starting a fresh session on your next message.")
        return

    logger.info(
        "Processing | user=%s (%d) | channel=%d | preview=%.120r",
        message.author, message.author.id, message.channel.id, content,
    )

    new_session = _start_fresh
    _start_fresh = False
    if new_session:
        logger.info("Starting new session as requested by !new")

    try:
        await message.add_reaction("⏳")
    except discord.HTTPException:
        pass

    try:
        await invoke_claude(content, message.channel, new_session=new_session)
    except Exception as exc:
        logger.exception("Unhandled exception in invoke_claude: %s", exc)
        try:
            await message.channel.send(f"**Internal error:** {exc}")
        except discord.HTTPException:
            pass
    finally:
        try:
            await message.remove_reaction("⏳", bot.user)
        except Exception:
            pass

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
        ALLOWED_CHANNEL_ID, ALLOWED_USER_IDS,
    )
    bot.run(DISCORD_TOKEN, log_handler=None)
