"""
Health check — disk space, container status, auto-restart.
Designed to run inside the claude-discord-bot container as root.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import shutil
import socket

logger = logging.getLogger("claude-bot.health")

DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "85"))
DISK_CRIT_PCT = float(os.environ.get("DISK_CRIT_PCT", "95"))
DOCKER_SOCKET = "/var/run/docker.sock"


def _docker(path: str, *, post: bool = False) -> list | dict | None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(DOCKER_SOCKET)
        conn = http.client.HTTPConnection("localhost")
        conn.sock = s
        method = "POST" if post else "GET"
        conn.request(method, path, headers={"Host": "localhost", "Content-Length": "0"})
        resp = conn.getresponse()
        body = resp.read()
    if body and not post:
        return json.loads(body)
    return None


def check_disk() -> list[str]:
    alerts = []
    for path, label in [("/mnt/user", "array"), ("/mnt/cache", "cache")]:
        if not os.path.isdir(path):
            continue
        try:
            total, used, free = shutil.disk_usage(path)
            pct = used / total * 100
            free_gb = free / 1024 ** 3
            if pct >= DISK_CRIT_PCT:
                alerts.append(f"CRITICAL: `{label}` at **{pct:.1f}%** — only {free_gb:.1f} GB free")
            elif pct >= DISK_WARN_PCT:
                alerts.append(f"WARNING: `{label}` at **{pct:.1f}%** — {free_gb:.1f} GB free")
        except Exception as exc:
            logger.warning("Disk check failed for %s: %s", path, exc)
    return alerts


def check_containers(monitored: list[str]) -> tuple[list[str], list[str]]:
    """Returns (alert_lines, restarted_names)."""
    if not monitored:
        return [], []

    try:
        all_containers = _docker("/containers/json?all=1")
    except Exception as exc:
        return [f"CRITICAL: Docker socket unreachable — {exc}"], []

    state_by_name: dict[str, str] = {}
    for c in all_containers:
        for name in c.get("Names", []):
            state_by_name[name.lstrip("/")] = c["State"]

    alerts: list[str] = []
    restarted: list[str] = []

    for name in monitored:
        state = state_by_name.get(name)
        if state is None:
            alerts.append(f"CRITICAL: `{name}` not found")
        elif state != "running":
            try:
                _docker(f"/containers/{name}/start", post=True)
                restarted.append(name)
                alerts.append(f"WARNING: `{name}` was `{state}` — restarted successfully")
            except Exception as exc:
                alerts.append(f"CRITICAL: `{name}` is `{state}` — restart failed: {exc}")

    return alerts, restarted


def run(monitored: list[str]) -> str | None:
    """
    Run all checks. Returns a Discord-formatted alert message,
    or None if everything is healthy.
    """
    lines: list[str] = []
    lines += check_disk()
    container_alerts, _ = check_containers(monitored)
    lines += container_alerts

    if not lines:
        return None

    body = "\n".join(f"- {line}" for line in lines)
    return f"**Health Alert**\n{body}"


def status_report(monitored: list[str]) -> str:
    """
    Full status snapshot for on-demand !health command.
    Always returns a message (not just on problems).
    """
    lines: list[str] = []

    # Disk
    for path, label in [("/mnt/user", "array"), ("/mnt/cache", "cache")]:
        if not os.path.isdir(path):
            continue
        try:
            total, used, free = shutil.disk_usage(path)
            pct = used / total * 100
            free_gb = free / 1024 ** 3
            total_gb = total / 1024 ** 3
            icon = "🔴" if pct >= DISK_CRIT_PCT else "🟡" if pct >= DISK_WARN_PCT else "🟢"
            lines.append(f"{icon} `{label}`: {pct:.1f}% used ({free_gb:.1f}/{total_gb:.1f} GB free)")
        except Exception as exc:
            lines.append(f"🔴 `{label}`: error — {exc}")

    lines.append("")

    # Containers
    if monitored:
        try:
            all_containers = _docker("/containers/json?all=1")
            state_by_name = {n.lstrip("/"): c["State"] for c in all_containers for n in c.get("Names", [])}
            for name in monitored:
                state = state_by_name.get(name, "missing")
                icon = "🟢" if state == "running" else "🔴"
                lines.append(f"{icon} `{name}`: {state}")
        except Exception as exc:
            lines.append(f"🔴 Docker: {exc}")
    else:
        lines.append("No containers configured for monitoring.")

    return "**Health Status**\n" + "\n".join(lines)
