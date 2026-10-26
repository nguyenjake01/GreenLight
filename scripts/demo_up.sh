#!/bin/bash
# Long-running demo stack for recording (design doc §6.4, J11). Like full_run.sh, but it applies
# no candidate and leaves the servers running: the recorder clicks "Simulate application" on the
# mock ATS index to start each run. Stop with Ctrl-C here, or `bash scripts/demo_down.sh`.
#
#   SLACK=real bash scripts/demo_up.sh        # the demo: real Slack card, OpenShell run checks
#   bash scripts/demo_up.sh                   # rehearsal: Slack stubbed (cards go to $RUNS_DIR/_slack_stub.jsonl)
#   DETACH=1 SLACK=real bash scripts/demo_up.sh   # start, print the URLs, return (stop with demo_down.sh)
#
# Settings come from .env; the variables below override them.
#   SLACK=real|stub          real posts to SLACK_CHANNEL_ID (default stub)
#   SANDBOX_BACKEND          openshell (default, the demo backend; its gateway is started if needed) | docker
#   RUN_CHECK                on (default) | off | baseline_only
#   ATS_PORT / VERIFIER_PORT 8101 / 8201 with SLACK=real (the OpenClaw plugin reaches the verifier on 8201, so keep it
#                            for the demo); 8100 / 8200 with Slack stubbed, so test stacks never take the bot's ports
#   ATS_HOST                 127.0.0.1 (default); 0.0.0.0 to open the mock ATS from another machine
#   VERIFIER_HOST            0.0.0.0 (default), so the OpenClaw sandbox can reach the verifier
#   ATS_PUBLIC_URL           what "Open in Greenhouse" points at (default http://localhost:$ATS_PORT);
#                            set it to the GB10's address if the browser is on another machine
#   RUNS_DIR                 default: a fresh /tmp/rv-demo-XXXX
#   GITHUB_FETCH_MODE        replay (default)
#   MOCK_ATS_HIDE_SIMULATE   1|true|yes hides the "Simulate application" buttons on the mock ATS index and shows a link to the
#                            candidate-facing careers page (/apply) instead. Export it before running; the mock ATS inherits it.
#                            Canned runs still work from a terminal (see docs/demo-runbook.md, "Live upload").
#   MOCK_ATS_PASSCODE        set it to put the mock ATS behind a passcode for tunnel visitors (requests that carry a
#                            Cf-Connecting-Ip header, i.e. via cloudflared). Local requests (the verifier, this machine's
#                            browser) are never gated. Unset or empty: no gate. The value is never printed.
#   OPENCLAW_SANDBOX         rv-openclaw (the sandbox checked when SLACK=real)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY=.venv/bin/python

SLACK="${SLACK:-stub}"
MODULES="llm,extract,agent,judge,resume,fetch,rubric,render,annotate,ats,harness,sandbox"
[ "$SLACK" = "real" ] && MODULES="$MODULES,slack"

# Real Slack uses 8101/8201 (the port the OpenClaw plugin calls); stub stacks keep 8100/8200.
if [ "$SLACK" = "real" ]; then DEF_ATS=8101; DEF_VERIFIER=8201; else DEF_ATS=8100; DEF_VERIFIER=8200; fi
ATS_PORT="${ATS_PORT:-$DEF_ATS}"
VERIFIER_PORT="${VERIFIER_PORT:-$DEF_VERIFIER}"
ATS_HOST="${ATS_HOST:-127.0.0.1}"
VERIFIER_HOST="${VERIFIER_HOST:-0.0.0.0}"
OPENCLAW_SANDBOX="${OPENCLAW_SANDBOX:-rv-openclaw}"

MADE_RUNS=""; [ -n "${RUNS_DIR:-}" ] || MADE_RUNS=1
export RUNS_DIR="${RUNS_DIR:-$(mktemp -d -t rv-demo-XXXX)}"
mkdir -p "$RUNS_DIR"
export VERIFIER_REAL="$MODULES"
export SANDBOX_BACKEND="${SANDBOX_BACKEND:-openshell}"
export GITHUB_FETCH_MODE="${GITHUB_FETCH_MODE:-replay}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}"
export LLM_MODEL="${LLM_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
# The two servers find each other through these (config.py defaults assume 8100/8200).
export ATS_PUBLIC_URL="${ATS_PUBLIC_URL:-http://localhost:$ATS_PORT}"
export HARVEST_AUTH_URL="http://127.0.0.1:$ATS_PORT/token"
export HARVEST_BASE_URL="http://127.0.0.1:$ATS_PORT/v3"
export VERIFIER_WEBHOOK_URL="http://127.0.0.1:$VERIFIER_PORT/webhooks/greenhouse"
LOGS="$RUNS_DIR/_logs"; mkdir -p "$LOGS"
POINTER="${RV_DEMO_POINTER:-/tmp/rv-demo-latest}"

warn() { echo "WARN: $*" >&2; }
# A failed preflight leaves no empty runs dir behind.
die()  { echo "ERROR: $*" >&2; [ -n "$MADE_RUNS" ] && rm -rf "$RUNS_DIR"; exit 1; }
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
# Run a command with the docker group (older shells only get it through sg).
dk() { if id -nG | grep -qw docker; then bash -c "$*"; else sg docker -c "$*"; fi; }

echo "== Resume Verifier demo stack =="
echo "runs: $RUNS_DIR"
case "$(printf %s "${MOCK_ATS_HIDE_SIMULATE:-}" | tr A-Z a-z)" in
  1|true|yes) export MOCK_ATS_HIDE_SIMULATE; echo "mock ATS: Simulate buttons hidden (MOCK_ATS_HIDE_SIMULATE on); candidates apply at /apply" ;;
  *) echo "mock ATS: Simulate buttons shown (MOCK_ATS_HIDE_SIMULATE off)" ;;
esac
if [ -n "${MOCK_ATS_PASSCODE:-}" ]; then export MOCK_ATS_PASSCODE; echo "mock ATS: passcode gate on for tunnel visitors"
else echo "mock ATS: passcode gate off"; fi
echo "modules: $VERIFIER_REAL   sandbox: $SANDBOX_BACKEND   run check: ${RUN_CHECK:-on}   slack: $SLACK"

# ── Preflight ────────────────────────────────────────────────────────────────
echo; echo "== Preflight =="
[ -x "$PY" ] || die "$PY not found (create the venv first)"

curl -sf -m 5 "${LLM_HEALTH_URL:-http://localhost:8000/v1/models}" > /dev/null \
  || die "vLLM is not answering on :8000. Restart: sg docker -c \"bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh\" (docs/handoff.md)"
echo "ok   vLLM answers on :8000"

for pair in "mock ATS:$ATS_PORT" "verifier:$VERIFIER_PORT"; do
  if port_busy "${pair##*:}"; then
    die "port ${pair##*:} (${pair%%:*}) is already in use. Run scripts/demo_down.sh, or pick another with ATS_PORT / VERIFIER_PORT."
  fi
done
echo "ok   ports $ATS_PORT and $VERIFIER_PORT are free"

# .env: names only, never values.
MISSING_FATAL=(); MISSING_WARN=()
if [ -f .env ]; then
  KEYS="$($PY -c 'from dotenv import dotenv_values as d; print(" ".join(k for k, v in d(".env").items() if v))')"
else
  KEYS=""; warn ".env not found"
fi
has_key() { case " $KEYS " in *" $1 "*) return 0 ;; esac; [ -n "${!1:-}" ]; }
if [ "$SLACK" = "real" ]; then
  for k in SLACK_BOT_TOKEN SLACK_CHANNEL_ID; do has_key "$k" || MISSING_FATAL+=("$k"); done
fi
for k in GREENHOUSE_WEBHOOK_SECRET INTERNAL_TOKEN; do has_key "$k" || MISSING_WARN+=("$k"); done
[ "$GITHUB_FETCH_MODE" = "replay" ] || has_key GITHUB_TOKEN || MISSING_WARN+=("GITHUB_TOKEN")
[ ${#MISSING_FATAL[@]} -eq 0 ] || die ".env is missing: ${MISSING_FATAL[*]} (needed for SLACK=real)"
if [ ${#MISSING_WARN[@]} -gt 0 ]; then
  warn ".env is missing: ${MISSING_WARN[*]} (GREENHOUSE_WEBHOOK_SECRET falls back to a dev secret; INTERNAL_TOKEN is needed by the OpenClaw bot)"
else
  echo "ok   .env has the needed keys"
fi

# Run-check image (only matters when run checks are on).
if [ "${RUN_CHECK:-on}" = "on" ] && [ "$SANDBOX_BACKEND" != "none" ]; then
  if ! dk "docker image inspect ${RUNCHECK_IMAGE:-resume-verifier-runcheck:latest} >/dev/null 2>&1"; then
    warn "run-check image ${RUNCHECK_IMAGE:-resume-verifier-runcheck:latest} not found (docker build -f docker/runcheck.Dockerfile ...); run checks will fail"
  else
    echo "ok   run-check image present"
  fi
fi

# OpenClaw sandbox (only when the real card is posted): a warning, not fatal.
if [ "$SLACK" = "real" ]; then
  if (PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"; command -v nemoclaw >/dev/null); then
    NC_OUT="$(dk "env -u XDG_STATE_HOME -u XDG_CONFIG_HOME PATH=$HOME/.local/node/bin:$HOME/.local/bin:\$PATH timeout 40 nemoclaw $OPENCLAW_SANDBOX channels status --channel slack --json" 2>&1 || true)"
    if printf '%s' "$NC_OUT" | grep -qE '"verdict"[[:space:]]*:[[:space:]]*"healthy"'; then
      echo "ok   OpenClaw sandbox $OPENCLAW_SANDBOX: Slack channel ready"
    else
      warn "OpenClaw sandbox $OPENCLAW_SANDBOX: Slack channel not confirmed ready. Card buttons and the @-mention need it."
      warn "     check: nemoclaw $OPENCLAW_SANDBOX channels status --channel slack --json   (output below, tokens hidden)"
      printf '%s\n' "$NC_OUT" | grep -E '"(verdict|label|severity|detail)"' | head -12 | sed 's/^/     /' >&2
    fi
  else
    warn "nemoclaw not found in ~/.local/node/bin; cannot check the OpenClaw sandbox"
  fi
fi

# OpenShell backend: the CLI must find the local gateway (verifier/stack/openshell/gateway.sh).
if [ "$SANDBOX_BACKEND" = "openshell" ]; then
  export PATH="$HOME/.local/bin:$PATH"
  source <(bash verifier/stack/openshell/gateway.sh env)
  if ! openshell gateway info 2>/dev/null | grep -q healthy; then
    [ -d "${RV_OPENSHELL_HOME:-$HOME/.rv-openshell}" ] || bash verifier/stack/openshell/gateway.sh setup
    echo "starting the OpenShell gateway (log: $LOGS/gateway.log; stop: bash verifier/stack/openshell/gateway.sh stop)"
    nohup sg docker -c "bash verifier/stack/openshell/gateway.sh start" > "$LOGS/gateway.log" 2>&1 &
    for _ in $(seq 1 30); do openshell gateway info 2>/dev/null | grep -q healthy && break; sleep 1; done
    openshell gateway info 2>/dev/null | grep -q healthy || die "OpenShell gateway did not start; see $LOGS/gateway.log"
  fi
  echo "ok   OpenShell gateway healthy"
fi

# ── Start the servers ────────────────────────────────────────────────────────
# Each server gets its own process group (setsid), so stopping also kills uvicorn under sg.
PIDS=()
stop() {
  [ "${DETACH:-0}" = "1" ] && return
  for p in "${PIDS[@]}"; do kill -TERM -- "-$p" 2>/dev/null || true; done
}
trap stop EXIT
trap 'echo; echo "stopping..."; exit 0' INT TERM

setsid $PY -m uvicorn verifier.ats.mock_app:app --host "$ATS_HOST" --port "$ATS_PORT" > "$LOGS/ats.log" 2>&1 & PIDS+=($!)
if id -nG | grep -qw docker; then
  setsid $PY -m uvicorn verifier.core.service:app --host "$VERIFIER_HOST" --port "$VERIFIER_PORT" > "$LOGS/service.log" 2>&1 & PIDS+=($!)
else
  setsid sg docker -c "$PY -m uvicorn verifier.core.service:app --host $VERIFIER_HOST --port $VERIFIER_PORT" > "$LOGS/service.log" 2>&1 & PIDS+=($!)
fi
MADE_RUNS=""
printf '%s\n' "ats ${PIDS[0]}" "verifier ${PIDS[1]}" > "$LOGS/pids"
ln -sfn "$RUNS_DIR" "$POINTER" 2>/dev/null || true

READY=0
for _ in $(seq 1 40); do
  if curl -sf -m 1 "http://127.0.0.1:$ATS_PORT/" > /dev/null 2>&1 \
     && curl -s -m 1 "http://127.0.0.1:$VERIFIER_PORT/internal/health" > /dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
[ "$READY" = 1 ] || die "the servers did not come up; see $LOGS/ats.log and $LOGS/service.log"

echo
echo "== Demo stack is up =="
echo "Mock ATS (open this, click 'Simulate application'): $ATS_PUBLIC_URL/"
echo "Candidate pages open from the card's 'Open in Greenhouse' button ($ATS_PUBLIC_URL/candidates/<id>?application_id=<id>)"
echo "Verifier health:   http://127.0.0.1:$VERIFIER_PORT/internal/health   (listening on $VERIFIER_HOST)"
echo "Runs dir:          $RUNS_DIR"
echo "Logs:              $LOGS/{ats,service}.log   (stub Slack log: $RUNS_DIR/_slack_stub.jsonl)"
echo "Restore a stripped card: RUNS_DIR=$RUNS_DIR bash scripts/refresh_card.sh <application_id>"
if [ "${DETACH:-0}" = "1" ]; then
  echo "Detached. Stop with: RUNS_DIR=$RUNS_DIR bash scripts/demo_down.sh"
  exit 0
fi
echo "Stop with Ctrl-C here, or: RUNS_DIR=$RUNS_DIR bash scripts/demo_down.sh"

# Stay in the foreground; leave if a server dies.
while true; do
  sleep 5
  for i in 0 1; do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      echo "a server exited unexpectedly; see $LOGS/ats.log and $LOGS/service.log" >&2
      exit 1
    fi
  done
done
