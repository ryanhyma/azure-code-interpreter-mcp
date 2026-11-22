#!/usr/bin/env bash
# Boundary test (SC-14): MCP on the docker0 bridge, downloads loopback-only,
# nothing on the LAN address or 0.0.0.0.
set -u

MCP_HOST="${AZCI_MCP_HOST:-172.17.0.1}"
MCP_PORT="${AZCI_MCP_PORT:-8935}"
DL_HOST="${AZCI_DOWNLOAD_HOST:-127.0.0.1}"
DL_PORT="${AZCI_DOWNLOAD_PORT:-8936}"

fail() { echo "FAIL: $1" >&2; exit 1; }
ok() { echo "ok: $1"; }

# 1. exact listeners
ss -ltn | grep -q "${MCP_HOST}:${MCP_PORT}" || fail "MCP not listening on ${MCP_HOST}:${MCP_PORT}"
ss -ltn | grep -q "${DL_HOST}:${DL_PORT}" || fail "downloads not listening on ${DL_HOST}:${DL_PORT}"
ss -ltn | grep -q "0.0.0.0:${MCP_PORT}" && fail "MCP bound to 0.0.0.0:${MCP_PORT}"
ss -ltn | grep -q "0.0.0.0:${DL_PORT}" && fail "downloads bound to 0.0.0.0:${DL_PORT}"
ok "listeners exact (${MCP_HOST}:${MCP_PORT} + ${DL_HOST}:${DL_PORT})"

# 2. bridge health 200
curl -sf -m 5 "http://${MCP_HOST}:${MCP_PORT}/health" >/dev/null || fail "bridge health not 200"
ok "bridge MCP health 200"

# 3. loopback health 200
curl -sf -m 5 "http://${DL_HOST}:${DL_PORT}/health" >/dev/null || fail "loopback download health not 200"
ok "loopback download health 200"

# 4. LAN address must refuse both listeners
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$LAN_IP" ]; then
  curl -s -m 3 "http://${LAN_IP}:${MCP_PORT}/health" >/dev/null 2>&1 && fail "MCP reachable on LAN ${LAN_IP}"
  curl -s -m 3 "http://${LAN_IP}:${DL_PORT}/health" >/dev/null 2>&1 && fail "downloads reachable on LAN ${LAN_IP}"
  ok "LAN ${LAN_IP} refuses both listeners"
fi

echo "boundary ok"
