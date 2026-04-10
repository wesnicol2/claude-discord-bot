#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# test-security.sh – Claude Discord Bot Security Test Suite (Step 1)
#
# Tests every security requirement of the container:
#   T01  Non-root user (UID/GID = 1000)
#   T02  Read-only root filesystem
#   T03  /workspace is writable by the bot user
#   T04  /app/logs is writable by the bot user
#   T05  /config is read-only (cannot write)
#   T06  /home/node/.claude is read-only (cannot write)
#   T07  No new privileges (NoNewPrivs=1 in /proc/1/status)
#   T08  All capabilities dropped (CapEff=0)
#   T09  PID limit enforced (pids_limit ≤ 100)
#   T10  Memory limit set
#   T11  CPU limit set
#   T12  Outbound internet reachable (DNS + HTTPS)
#   T13  Host filesystem not accessible from container
#   T14  Other containers unreachable (ICC disabled)
#   T15  tini/dumb-init is PID 1 (proper signal handling)
#   T16  No writable paths on root fs outside mounts
#   T17  /tmp is a writable tmpfs (not host mount)
#   T18  Claude Code CLI installed and executable
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CONTAINER="claude-discord-bot"
PASS=0
FAIL=0
SKIP=0
RESULTS=()

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { echo -e "${GREEN}  PASS${RESET}  $1"; PASS=$((PASS+1)); RESULTS+=("PASS: $1"); }
fail() { echo -e "${RED}  FAIL${RESET}  $1"; FAIL=$((FAIL+1)); RESULTS+=("FAIL: $1"); }
skip() { echo -e "${YELLOW}  SKIP${RESET}  $1"; SKIP=$((SKIP+1)); RESULTS+=("SKIP: $1"); }
info() { echo -e "${CYAN}        $1${RESET}"; }

# Helper: run a command inside the container
cexec() { docker exec "$CONTAINER" sh -c "$@" 2>&1; }

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  Claude Discord Bot – Security Test Suite (Step 1)${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo ""

# ── Pre-flight: container must be running ─────────────────────────────────────
echo -e "${BOLD}[Pre-flight]${RESET} Checking container is running..."
STATUS=$(docker inspect --format='{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
if [ "$STATUS" != "running" ]; then
  echo -e "${RED}Container '$CONTAINER' is not running (status: $STATUS). Aborting.${RESET}"
  exit 1
fi
echo -e "  Container status: ${GREEN}$STATUS${RESET}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
echo -e "${BOLD}[T01] Non-root user (UID/GID 1000)${RESET}"
UID_IN=$(cexec "id -u")
GID_IN=$(cexec "id -g")
USER_IN=$(cexec "id -un")
info "uid=$UID_IN  gid=$GID_IN  user=$USER_IN"
if [ "$UID_IN" = "1000" ] && [ "$GID_IN" = "1000" ]; then
  pass "T01: Running as UID=1000 GID=1000 (user: $USER_IN)"
else
  fail "T01: Expected uid=1000 gid=1000, got uid=$UID_IN gid=$GID_IN"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T02] Read-only root filesystem${RESET}"
RO_TEST=$(cexec "touch /ro-test-$$ 2>&1 && echo writable || echo readonly")
info "Write attempt to / → $RO_TEST"
if echo "$RO_TEST" | grep -q "readonly\|Read-only\|read-only"; then
  pass "T02: Root filesystem is read-only"
else
  fail "T02: Root filesystem is writable (expected read-only)"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T03] /workspace is writable${RESET}"
WS_TEST=$(cexec "touch /workspace/.write-test-$$ && rm /workspace/.write-test-$$ && echo ok || echo fail")
info "/workspace write test → $WS_TEST"
if [ "$WS_TEST" = "ok" ]; then
  pass "T03: /workspace is writable"
else
  fail "T03: /workspace is not writable – Claude Code cannot function"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T04] /app/logs is writable${RESET}"
LOG_TEST=$(cexec "touch /app/logs/.write-test-$$ && rm /app/logs/.write-test-$$ && echo ok || echo fail")
info "/app/logs write test → $LOG_TEST"
if [ "$LOG_TEST" = "ok" ]; then
  pass "T04: /app/logs is writable"
else
  fail "T04: /app/logs is not writable – bot cannot log"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T05] /config is read-only${RESET}"
CFG_TEST=$(cexec "touch /config/.write-test-$$ 2>&1 && echo writable || echo readonly")
info "/config write attempt → $CFG_TEST"
if echo "$CFG_TEST" | grep -q "readonly\|Read-only\|read-only\|Permission denied"; then
  pass "T05: /config is read-only"
else
  fail "T05: /config is writable – should be read-only"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T06] /home/node/.claude is read-only${RESET}"
CLAUDE_TEST=$(cexec "touch /home/node/.claude/.write-test-$$ 2>&1 && echo writable || echo readonly")
info "/home/node/.claude write attempt → $CLAUDE_TEST"
if echo "$CLAUDE_TEST" | grep -q "readonly\|Read-only\|read-only\|Permission denied"; then
  pass "T06: /home/node/.claude is read-only"
else
  fail "T06: /home/node/.claude is writable – Claude config should be immutable"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T07] No new privileges (NoNewPrivs)${RESET}"
NNP=$(cexec "grep NoNewPrivs /proc/1/status 2>/dev/null || echo 'not available'")
info "NoNewPrivs from /proc/1/status: $NNP"
if echo "$NNP" | grep -q "NoNewPrivs:[[:space:]]*1"; then
  pass "T07: NoNewPrivs=1 – setuid escalation blocked"
else
  fail "T07: NoNewPrivs not set to 1 (got: $NNP)"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T08] All capabilities dropped (CapEff=0)${RESET}"
CAP_EFF=$(cexec "grep CapEff /proc/1/status 2>/dev/null | awk '{print \$2}'")
info "CapEff from /proc/1/status: $CAP_EFF"
if [ "$CAP_EFF" = "0000000000000000" ] || [ "$CAP_EFF" = "0" ]; then
  pass "T08: CapEff=0 – all capabilities dropped"
else
  # Decode which caps remain
  CAP_NUM=$(printf '%d' "0x$CAP_EFF" 2>/dev/null || echo "unknown")
  fail "T08: CapEff=$CAP_EFF (non-zero) – unexpected capabilities present"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T09] PID limit enforced${RESET}"
PIDS_LIMIT=$(docker inspect --format='{{.HostConfig.PidsLimit}}' "$CONTAINER" 2>/dev/null)
info "PidsLimit from Docker inspect: $PIDS_LIMIT"
if [ -n "$PIDS_LIMIT" ] && [ "$PIDS_LIMIT" -gt 0 ] && [ "$PIDS_LIMIT" -le 100 ]; then
  pass "T09: PidsLimit=$PIDS_LIMIT (≤ 100)"
else
  fail "T09: PidsLimit=$PIDS_LIMIT – expected 1-100"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T10] Memory limit set${RESET}"
MEM_LIMIT=$(docker inspect --format='{{.HostConfig.Memory}}' "$CONTAINER" 2>/dev/null)
info "Memory limit (bytes): $MEM_LIMIT"
if [ -n "$MEM_LIMIT" ] && [ "$MEM_LIMIT" -gt 0 ]; then
  MEM_MB=$((MEM_LIMIT / 1024 / 1024))
  pass "T10: Memory limit = ${MEM_MB} MiB"
else
  fail "T10: No memory limit set – container can exhaust host RAM"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T11] CPU limit set${RESET}"
CPU_QUOTA=$(docker inspect --format='{{.HostConfig.NanoCpus}}' "$CONTAINER" 2>/dev/null)
CPU_PERIOD=$(docker inspect --format='{{.HostConfig.CpuPeriod}}' "$CONTAINER" 2>/dev/null)
CPU_CFS=$(docker inspect --format='{{.HostConfig.CpuQuota}}' "$CONTAINER" 2>/dev/null)
info "NanoCpus=$CPU_QUOTA  CpuPeriod=$CPU_PERIOD  CpuQuota=$CPU_CFS"
if [ -n "$CPU_QUOTA" ] && [ "$CPU_QUOTA" -gt 0 ]; then
  CPU_CORES=$(echo "scale=2; $CPU_QUOTA / 1000000000" | bc 2>/dev/null || echo "set")
  pass "T11: CPU limit = ${CPU_CORES} cores"
elif [ -n "$CPU_CFS" ] && [ "$CPU_CFS" -gt 0 ]; then
  pass "T11: CPU CFS quota = $CPU_CFS"
else
  fail "T11: No CPU limit detected (NanoCpus=$CPU_QUOTA, CpuQuota=$CPU_CFS)"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T12] Outbound internet reachable${RESET}"
DNS_TEST=$(cexec "nslookup discord.com 2>&1 | head -3 || echo fail")
info "DNS test (discord.com): $(echo "$DNS_TEST" | head -1)"
if echo "$DNS_TEST" | grep -qiE "Address|answer|Non-authoritative"; then
  pass "T12a: DNS resolution works (discord.com)"
else
  # Try alternative
  DNS2=$(cexec "wget -q --spider --timeout=5 https://discord.com 2>&1 && echo ok || echo fail")
  if [ "$DNS2" = "ok" ]; then
    pass "T12a: DNS + HTTPS reachable (discord.com)"
  else
    fail "T12a: Cannot resolve discord.com – bot cannot reach Discord API"
  fi
fi

# Any HTTP response code (200, 401, 403, 404) means the host is reachable.
# Only connection-level failures (refused, timeout, DNS failure) mean blocked.
HTTPS_RAW=$(cexec "wget -S --spider --timeout=10 https://api.anthropic.com 2>&1 || true")
info "HTTPS test (api.anthropic.com): $(echo "$HTTPS_RAW" | grep -m1 'HTTP\|connected\|failed\|refused\|timeout' || echo "$HTTPS_RAW" | head -1)"
if echo "$HTTPS_RAW" | grep -qiE "HTTP/[0-9]|connected to|remote file exists"; then
  pass "T12b: HTTPS reachable (api.anthropic.com – got HTTP response)"
else
  fail "T12b: Cannot reach api.anthropic.com – Claude API blocked (no HTTP response)"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T13] Host filesystem not accessible${RESET}"
# Try to access common host paths that should not be visible
HOST_ESCAPE=$(cexec "ls /mnt 2>&1 || echo blocked")
info "/mnt from inside container: $HOST_ESCAPE"
if echo "$HOST_ESCAPE" | grep -qiE "blocked|No such|Permission denied|cannot|empty"; then
  pass "T13: Host /mnt not accessible from container"
else
  # Check if what's listed is actually the container's own dirs or host
  MOUNTS=$(cexec "cat /proc/mounts | grep -v '^overlay\|^tmpfs\|^proc\|^sysfs\|^devpts\|^cgroup\|^mqueue\|^shm' | grep '/mnt' | wc -l")
  if [ "$MOUNTS" = "0" ]; then
    pass "T13: No host /mnt mounts found in container"
  else
    fail "T13: Container may have access to host paths – /mnt visible: $HOST_ESCAPE"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T14] ICC disabled (other containers unreachable)${RESET}"
# Get the network name from docker inspect
NET_NAME=$(docker inspect --format='{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$CONTAINER")
info "Container network: $NET_NAME"
ICC=$(docker network inspect "$NET_NAME" --format='{{index .Options "com.docker.network.bridge.enable_icc"}}' 2>/dev/null || echo "unknown")
info "enable_icc option: $ICC"
if [ "$ICC" = "false" ]; then
  pass "T14: ICC disabled on network $NET_NAME"
else
  fail "T14: ICC not explicitly disabled (value='$ICC') – other containers may be reachable"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T15] tini is PID 1 (proper signal handling)${RESET}"
PID1=$(cexec "cat /proc/1/comm 2>/dev/null || ls -la /proc/1/exe 2>/dev/null | awk '{print \$NF}'")
info "PID 1 comm: $PID1"
if echo "$PID1" | grep -qiE "tini|dumb-init"; then
  pass "T15: PID 1 is $PID1 (proper init for signal handling)"
else
  fail "T15: PID 1 is '$PID1' – expected tini or dumb-init. Zombie process risk."
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T16] No writable paths on root fs (spot check)${RESET}"
RO_PATHS=("/usr" "/bin" "/sbin" "/lib" "/etc" "/opt")
ALL_RO=true
for p in "${RO_PATHS[@]}"; do
  RESULT=$(cexec "touch $p/.write-test-$$ 2>&1 && echo writable || echo readonly")
  if echo "$RESULT" | grep -q "writable"; then
    info "$p → WRITABLE (unexpected)"
    ALL_RO=false
  else
    info "$p → read-only"
  fi
done
if $ALL_RO; then
  pass "T16: Core OS directories (${RO_PATHS[*]}) are all read-only"
else
  fail "T16: Some core OS directories are writable on the read-only root fs"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T17] /tmp is a writable tmpfs (not host mount)${RESET}"
TMP_TYPE=$(cexec "grep ' /tmp ' /proc/mounts | awk '{print \$3}'")
info "/tmp filesystem type: $TMP_TYPE"
TMP_WRITE=$(cexec "touch /tmp/.write-test-$$ && rm /tmp/.write-test-$$ && echo ok || echo fail")
info "/tmp write test: $TMP_WRITE"
if [ "$TMP_TYPE" = "tmpfs" ] && [ "$TMP_WRITE" = "ok" ]; then
  pass "T17: /tmp is a writable tmpfs (in-memory, no host exposure)"
elif [ "$TMP_WRITE" = "ok" ]; then
  fail "T17: /tmp is writable but type is '$TMP_TYPE' (not tmpfs) – may expose host filesystem"
else
  fail "T17: /tmp is not writable (type=$TMP_TYPE)"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}[T18] Claude Code CLI installed and accessible${RESET}"
CLAUDE_VER=$(cexec "claude --version 2>&1 || echo not-found")
info "claude --version: $CLAUDE_VER"
if echo "$CLAUDE_VER" | grep -qiE "claude|[0-9]+\.[0-9]+"; then
  pass "T18: Claude Code CLI available ($CLAUDE_VER)"
else
  fail "T18: Claude Code CLI not found – install it in the Dockerfile"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  TEST REPORT SUMMARY${RESET}"
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
TOTAL=$((PASS + FAIL + SKIP))
echo ""
echo -e "  Total tests : ${BOLD}$TOTAL${RESET}"
echo -e "  ${GREEN}Passed${RESET}      : ${BOLD}$PASS${RESET}"
echo -e "  ${RED}Failed${RESET}      : ${BOLD}$FAIL${RESET}"
echo -e "  ${YELLOW}Skipped${RESET}     : ${BOLD}$SKIP${RESET}"
echo ""
echo -e "${BOLD}  Results:${RESET}"
for r in "${RESULTS[@]}"; do
  if [[ "$r" == PASS* ]]; then echo -e "    ${GREEN}${r}${RESET}"
  elif [[ "$r" == FAIL* ]]; then echo -e "    ${RED}${r}${RESET}"
  else echo -e "    ${YELLOW}${r}${RESET}"; fi
done
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo -e "${GREEN}${BOLD}  All security requirements met.${RESET}"
  exit 0
else
  echo -e "${RED}${BOLD}  $FAIL test(s) failed. Review output above.${RESET}"
  exit 1
fi
