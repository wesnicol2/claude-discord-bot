"""
Unit tests for bot.py – run inside Docker with discord.py installed.
Tests core logic without making any real Discord or Claude connections.
"""

import asyncio
import importlib.util
import json
import os
import pathlib
import stat
import sys

# ── Environment setup (must happen before importing bot module) ────────────────
os.environ.setdefault("DISCORD_TOKEN", "fake_token_for_test")
os.environ.setdefault("DISCORD_ALLOWED_USERS", "771511808024379412")
os.environ.setdefault("DISCORD_CHANNEL_ID", "1492285575594643496")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
os.environ.setdefault("LOG_PATH", "/tmp")
os.environ.setdefault("CONFIG_PATH", "/tmp")
os.environ.setdefault("WORKSPACE_PATH", "/tmp/workspace")

# Write a temp allowed-tools.json for testing
pathlib.Path("/tmp/allowed-tools.json").write_text(json.dumps({
    "allowedTools": ["Read", "Write", "Glob"],
    "deniedTools": ["Bash"],
}))
pathlib.Path("/tmp/workspace").mkdir(parents=True, exist_ok=True)

# ── Patch discord so bot.run() doesn't actually connect ───────────────────────
import discord
import discord.ext.commands
discord.ext.commands.Bot.run = lambda *a, **kw: None

# ── Load the bot module ────────────────────────────────────────────────────────
bot_path = pathlib.Path(__file__).parent / "bot.py"
spec = importlib.util.spec_from_file_location("bot", bot_path)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    print("Module load: OK")
except SystemExit as e:
    print(f"SystemExit during load (code={e.code}) – check env vars")
    sys.exit(int(str(e.code)) if e.code is not None else 1)
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)


# ────────────────────────────────────────────────────────────────────────────────
# Test helpers
# ────────────────────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS  {label}")
        PASS += 1
    else:
        print(f"  FAIL  {label}{' — ' + detail if detail else ''}")
        FAIL += 1


# ────────────────────────────────────────────────────────────────────────────────
# 1. chunk_text
# ────────────────────────────────────────────────────────────────────────────────
print("\n── chunk_text ──")

chunk_text = mod.chunk_text

chunks = chunk_text("")
check("empty → ['(no output)']", chunks == ["(no output)"])

short = "hello world"
chunks = chunk_text(short)
check("short string stays as-is", chunks == [short])

long_text = "a" * 3000
chunks = chunk_text(long_text, max_len=1900)
check("3000-char text splits into >1 chunk", len(chunks) > 1)
check("each chunk ≤ 1900 chars", all(len(c) <= 1900 for c in chunks))
check("reassembled == original", "".join(chunks) == long_text)

multiline = "word\n" * 600         # 3000 chars with newlines
chunks = chunk_text(multiline, max_len=1900)
check("multiline splits correctly", all(len(c) <= 1900 for c in chunks))
check("multiline total chars correct", sum(len(c) for c in chunks) <= len(multiline))

exact = "x" * 1900
check("exactly-max-len text = 1 chunk", chunk_text(exact, max_len=1900) == [exact])

# ────────────────────────────────────────────────────────────────────────────────
# 2. load_allowed_tools
# ────────────────────────────────────────────────────────────────────────────────
print("\n── load_allowed_tools ──")

tools = mod.load_allowed_tools()
check("returns list", isinstance(tools, list))
check("contains expected tools", tools == ["Read", "Write", "Glob"], str(tools))

# missing file — patch the module constant directly (env is read at import time)
orig_cfg = mod.CONFIG_PATH
mod.CONFIG_PATH = "/nonexistent"
tools_missing = mod.load_allowed_tools()
check("missing config → returns []", tools_missing == [])
mod.CONFIG_PATH = orig_cfg

# ────────────────────────────────────────────────────────────────────────────────
# 3. Environment variable parsing
# ────────────────────────────────────────────────────────────────────────────────
print("\n── env var parsing ──")

check("ALLOWED_USER_IDS parsed", mod.ALLOWED_USER_IDS == {771511808024379412},
      str(mod.ALLOWED_USER_IDS))
check("ALLOWED_CHANNEL_ID parsed", mod.ALLOWED_CHANNEL_ID == 1492285575594643496,
      str(mod.ALLOWED_CHANNEL_ID))
check("CLAUDE_TIMEOUT is int", isinstance(mod.CLAUDE_TIMEOUT, int))
check("HEARTBEAT_INTERVAL is int", isinstance(mod.HEARTBEAT_INTERVAL, int))

# ────────────────────────────────────────────────────────────────────────────────
# 4. Subprocess invocation with a fake claude binary
# ────────────────────────────────────────────────────────────────────────────────
print("\n── subprocess (fake claude) ──")

# Write a fake claude binary that echoes its stdin back
fake_claude = pathlib.Path("/tmp/claude")
fake_claude.write_text("#!/bin/sh\nprintf 'Hello from Claude! Message received.'; cat\n")
mode = fake_claude.stat().st_mode
fake_claude.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "")


class FakeChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_subprocess_normal():
    ch = FakeChannel()
    await mod._invoke_claude_locked("Hello Claude!", ch)
    check("at least one Discord message sent", len(ch.sent) > 0)
    combined = " ".join(ch.sent)
    check("response contains expected text", "Claude" in combined, combined[:200])


async def test_subprocess_long_response():
    """Test that a long response is correctly chunked into multiple Discord messages."""
    long_fake = pathlib.Path("/tmp/claude")
    long_response = "word " * 1000  # ~5000 chars
    long_fake.write_text(f"#!/bin/sh\necho '{long_response}'\n")
    mode = long_fake.stat().st_mode
    long_fake.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    ch = FakeChannel()
    await mod._invoke_claude_locked("generate long", ch)
    check("long response splits into multiple messages", len(ch.sent) > 1,
          f"got {len(ch.sent)} messages")
    for msg in ch.sent:
        check(f"each message ≤ {mod.DISCORD_HARD_LIMIT} chars", len(msg) <= mod.DISCORD_HARD_LIMIT,
              f"len={len(msg)}")

    # Restore normal fake claude
    fake_claude.write_text("#!/bin/sh\nprintf 'Hello from Claude! Message received.'; cat\n")
    fake_claude.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def test_subprocess_nonzero_exit():
    """Test handling of non-zero exit with error output on stderr."""
    err_fake = pathlib.Path("/tmp/claude")
    err_fake.write_text("#!/bin/sh\necho 'something went wrong' >&2; exit 1\n")
    mode = err_fake.stat().st_mode
    err_fake.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    ch = FakeChannel()
    await mod._invoke_claude_locked("fail please", ch)
    check("error exit → error message sent", len(ch.sent) > 0)
    combined = " ".join(ch.sent)
    check("error message mentions exit code", "error" in combined.lower() or "exit" in combined.lower(),
          combined[:200])

    # Restore normal fake claude
    fake_claude.write_text("#!/bin/sh\nprintf 'Hello from Claude! Message received.'; cat\n")
    fake_claude.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def test_subprocess_missing_binary():
    """Test graceful handling when claude binary is missing."""
    os.environ["PATH"] = "/nonexistent"

    ch = FakeChannel()
    await mod._invoke_claude_locked("test message", ch)
    check("missing binary → error message sent", len(ch.sent) > 0)
    combined = " ".join(ch.sent)
    check("error message mentions claude", "claude" in combined.lower() or "Error" in combined,
          combined[:200])

    os.environ["PATH"] = "/tmp:" + os.environ.get("PATH", "").replace("/nonexistent:", "")


asyncio.run(test_subprocess_normal())
asyncio.run(test_subprocess_long_response())
asyncio.run(test_subprocess_nonzero_exit())
asyncio.run(test_subprocess_missing_binary())

# ────────────────────────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
