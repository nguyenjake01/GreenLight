#!/bin/bash
# Stop the demo stack started by scripts/demo_up.sh (the mock ATS and the verifier only; the
# OpenShell gateway, vLLM and OpenClaw are shared and left alone).
#
#   bash scripts/demo_down.sh                       # the latest stack (/tmp/rv-demo-latest)
#   RUNS_DIR=/tmp/rv-demo-XXXX bash scripts/demo_down.sh
#   STOP_GATEWAY=1 bash scripts/demo_down.sh        # also stop the OpenShell run-check gateway
set -uo pipefail
cd "$(dirname "$0")/.."

RUNS_DIR="$(readlink -f "${RUNS_DIR:-${1:-${RV_DEMO_POINTER:-/tmp/rv-demo-latest}}}")"
PIDS_FILE="$RUNS_DIR/_logs/pids"
[ -f "$PIDS_FILE" ] || { echo "no pids file at $PIDS_FILE (set RUNS_DIR=<runs dir> or pass it as an argument)"; exit 1; }

group_cmd() { ps -eo pgid=,args= | awk -v g="$1" '$1 == g' | cut -c1-200; }

while read -r name pgid; do
  [ -n "${pgid:-}" ] || continue
  # Only touch a process group whose command is ours.
  if group_cmd "$pgid" | grep -q "verifier\."; then
    kill -TERM -- "-$pgid" 2>/dev/null && echo "stopping $name (process group $pgid)"
    for _ in $(seq 1 10); do [ -n "$(group_cmd "$pgid")" ] || break; sleep 1; done
    group_cmd "$pgid" | grep -q "verifier\." && { kill -KILL -- "-$pgid" 2>/dev/null; echo "  $name needed SIGKILL"; }
  else
    echo "$name (group $pgid) is not running"
  fi
done < "$PIDS_FILE"

if [ "${STOP_GATEWAY:-0}" = "1" ]; then
  sg docker -c "bash verifier/stack/openshell/gateway.sh stop" || true
fi
echo "done. Runs are kept in $RUNS_DIR"
