#!/bin/bash
# Full-system run (design doc §6.4) on the GB10: mock ATS on :8100, verifier on :8200, real
# modules, then one candidate applies. Waits for the run checks, prints the verdict, stops both
# servers. Settings come from .env; the variables below override them.
#
#   bash scripts/full_run.sh                 # Henry, Slack stubbed
#   SLACK=real bash scripts/full_run.sh      # post the card to SLACK_CHANNEL_ID
#   bash scripts/full_run.sh fake            # another fixture (see: python -m verifier.ats.apply --list)
#   KEEP=1 bash scripts/full_run.sh          # leave the servers running afterwards
#
# Runs land in $RUNS_DIR (default: a fresh temp dir, printed at the start).
set -euo pipefail
cd "$(dirname "$0")/.."

CANDIDATE="${1:-henry}"
MODULES="llm,extract,agent,judge,resume,fetch,rubric,render,annotate,ats,harness,sandbox"
[ "${SLACK:-stub}" = "real" ] && MODULES="$MODULES,slack"

export RUNS_DIR="${RUNS_DIR:-$(mktemp -d -t rv-fullrun-XXXX)}"
export VERIFIER_REAL="$MODULES"
export SANDBOX_BACKEND="${SANDBOX_BACKEND:-docker}"
export GITHUB_FETCH_MODE="${GITHUB_FETCH_MODE:-replay}"
# The verifier runs on the host here, next to the vLLM container (design doc §5.2).
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
export LLM_MODEL="${LLM_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
LOGS="$RUNS_DIR/_logs"; mkdir -p "$LOGS"
PY=.venv/bin/python
echo "runs: $RUNS_DIR   modules: $VERIFIER_REAL   sandbox: $SANDBOX_BACKEND"

curl -sf -m 5 "${LLM_HEALTH_URL:-http://localhost:8000/v1/models}" > /dev/null \
  || { echo "vLLM is not answering on :8000 (see docs/handoff.md)"; exit 1; }

for port in 8100 8200; do
  if curl -s -m 1 -o /dev/null "localhost:$port/"; then echo "port $port is already in use"; exit 1; fi
done

# Each server gets its own process group (setsid), so stop() also kills uvicorn under sg.
PIDS=()
stop() { [ "${KEEP:-0}" = "1" ] && { echo "servers left running (process groups): ${PIDS[*]}"; return; }
         for p in "${PIDS[@]}"; do kill -TERM -- "-$p" 2>/dev/null || true; done; }
trap stop EXIT

setsid $PY -m uvicorn verifier.ats.mock_app:app --port 8100 > "$LOGS/ats.log" 2>&1 & PIDS+=($!)
# The Docker backend needs the docker group; older shells only get it through sg.
if id -nG | grep -qw docker; then
  setsid $PY -m uvicorn verifier.core.service:app --port 8200 > "$LOGS/service.log" 2>&1 & PIDS+=($!)
else
  setsid sg docker -c "$PY -m uvicorn verifier.core.service:app --port 8200" > "$LOGS/service.log" 2>&1 & PIDS+=($!)
fi
for _ in $(seq 1 30); do
  curl -sf -m 1 localhost:8100/ > /dev/null 2>&1 && curl -s -m 1 localhost:8200/internal/health > /dev/null 2>&1 && break
  sleep 1
done

APP_ID=$($PY -m verifier.ats.apply "$CANDIDATE" | $PY -c "import json,sys; print(json.load(sys.stdin)['application_id'])")
echo "applied: $CANDIDATE -> application $APP_ID"

$PY - "$RUNS_DIR/$APP_ID" <<'EOF'
import glob, json, sys, time
rd = sys.argv[1]
deadline = time.time() + 600
while time.time() < deadline:
    try:
        meta = json.load(open(f"{rd}/meta.json"))
    except (OSError, ValueError):
        meta = {}
    runs = meta.get("runs") or {}
    settled = all(v in ("done", "skipped", "error") for v in runs.values())
    if meta.get("writebacks") and settled:
        break
    failed = [e for e in meta.get("stage_errors", []) if e.get("stage") in ("resume", "extract")]
    if failed:
        sys.exit(f"stage {failed[0]['stage']} failed: {failed[0]['error']}")
    time.sleep(3)
else:
    sys.exit("timed out after 10 min; see _logs/service.log")
v = json.load(open(sorted(glob.glob(f"{rd}/verdicts/*.json"))[-1]))
emoji = {"supported": "🟢", "partial": "🟡", "contradicted": "🔴", "no_evidence": "⚪"}
for c in v["claims"]:
    print(f"  {c['claim_id']:>4} {emoji[c['label']]} {c['top_reason']}")
print("  runs:", meta.get("runs"), "| stage errors:", len(meta.get("stage_errors", [])),
      "| card:", (meta.get("slack") or {}).get("card_permalink") or "stub")
EOF
