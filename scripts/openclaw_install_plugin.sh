#!/usr/bin/env bash
# Install the resume-verifier OpenClaw plugin into an onboarded NemoClaw sandbox and wire it to the
# verifier service on this host. Non-interactive; run it after scripts/openclaw_onboard.sh.
#
#   bash scripts/openclaw_install_plugin.sh [--dry-run] [--verify-only] <sandbox-name>
#
#   --dry-run      print every command, change nothing (nothing is built, staged or sent)
#   --verify-only  skip build/install/config; only restart-free checks (Slack readiness + host reachability)
#
# Environment: ENV_FILE (default <repo>/.env; INTERNAL_TOKEN is read from it, never printed),
#   SLACK_ACTIONS=plugin|agent|off (default plugin), INSTALL_EXTRA_FLAGS (extra `openclaw plugins install` flags).
# The verifier service must run on the host and listen on a non-loopback address, e.g.
#   .venv/bin/python -m uvicorn verifier.core.service:app --host 0.0.0.0 --port 8200
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
PLUGIN_DIR="$REPO_ROOT/verifier/stack/openclaw/resume-verifier"
PRESET_FILE="$REPO_ROOT/verifier/stack/openclaw/nemoclaw-policy-verifier.yaml"
PRESET_NAME="rv-verifier"
PLUGIN_ID="resume-verifier"
SANDBOX_URL="http://host.openshell.internal:8200"   # how the sandbox reaches the host service
HOST_URL="http://localhost:8200"                    # how this host reaches it
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"

DRY_RUN=0; VERIFY_ONLY=0; NAME=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) NAME="$arg" ;;
  esac
done
[ -n "$NAME" ] || { echo "usage: $0 [--dry-run] [--verify-only] <sandbox-name>" >&2; exit 2; }
SLACK_ACTIONS="${SLACK_ACTIONS:-}"

step() { printf '\n== %s ==\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
# Run a command (or just print it in dry-run mode).
run() {
  printf '+ %s\n' "$*"
  [ "$DRY_RUN" = 1 ] || "$@"
}
# nemoclaw <sandbox> <args...>
nc() { run nemoclaw "$NAME" "$@"; }

PYTHON="$REPO_ROOT/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
envval() {
  [ -f "$ENV_FILE" ] || return 0
  ENV_FILE="$ENV_FILE" "$PYTHON" -c '
import os, sys
from dotenv import dotenv_values
print(dotenv_values(os.environ["ENV_FILE"]).get(sys.argv[1]) or "", end="")' "$1"
}

# ---------------------------------------------------------------------------------------------
step "0. Preflight"
command -v nemoclaw >/dev/null || die "nemoclaw not found (expected in ~/.local/bin)"
command -v node >/dev/null && command -v npm >/dev/null || die "node/npm not found (expected in ~/.local/node/bin)"
[ -n "$PYTHON" ] || die "python3 with python-dotenv is needed to read $ENV_FILE"
INTERNAL_TOKEN="${INTERNAL_TOKEN:-$(envval INTERNAL_TOKEN)}"
[ -n "$INTERNAL_TOKEN" ] || die "INTERNAL_TOKEN is not set in $ENV_FILE (the plugin and the service must share it)"
echo "INTERNAL_TOKEN: present (not printed)"
SLACK_ACTIONS="${SLACK_ACTIONS:-$(envval SLACK_ACTIONS)}"; SLACK_ACTIONS="${SLACK_ACTIONS:-plugin}"
case "$SLACK_ACTIONS" in plugin|agent|off) ;; *) die "SLACK_ACTIONS must be plugin, agent or off (got: $SLACK_ACTIONS)";; esac
echo "sandbox: $NAME   slackActions: $SLACK_ACTIONS   verifierUrl (in sandbox): $SANDBOX_URL"
if [ "$DRY_RUN" = 0 ]; then
  nemoclaw "$NAME" status >/dev/null 2>&1 || die "sandbox '$NAME' is not running or does not exist (nemoclaw list; nemoclaw $NAME recover)"
else
  echo "+ nemoclaw $NAME status        # must succeed"
fi

STAGE_PARENT=""
cleanup() { [ -n "$STAGE_PARENT" ] && rm -rf "$STAGE_PARENT"; return 0; }
trap cleanup EXIT

if [ "$VERIFY_ONLY" = 0 ]; then
# ---------------------------------------------------------------------------------------------
step "1. Build and test the plugin"
echo "+ cd $PLUGIN_DIR"
if [ "$DRY_RUN" = 1 ]; then
  echo "+ [ -d node_modules ] || npm ci"
  echo "+ npm run check"
  echo "+ npm run build"
else
  cd "$PLUGIN_DIR"
  [ -d node_modules ] || run npm ci
  run npm run check
  run npm run build
  cd "$REPO_ROOT"
fi

# ---------------------------------------------------------------------------------------------
step "2. Network policy: allow host.openshell.internal:8200 /internal/** (preset '$PRESET_NAME')"
[ -f "$PRESET_FILE" ] || die "missing $PRESET_FILE"
nc policy add --from-file "$PRESET_FILE" --dry-run
nc policy add --from-file "$PRESET_FILE" --yes

# ---------------------------------------------------------------------------------------------
step "3. Stage, upload and install the plugin"
# Stage outside the repo: uploads honour .gitignore, and dist/ and node_modules/ are ignored there.
# Ships what package.json "files" lists, plus package.json (devDependencies dropped) and the one
# runtime dependency (typebox) so the install needs no registry access.
STAGE_PARENT="$(mktemp -d -t rv-plugin-stage-XXXXXX)"
STAGE="$STAGE_PARENT/rv-plugin"
echo "+ stage plugin files into $STAGE (dist/ openclaw.plugin.json skills/ AGENT_INSTRUCTIONS.md [README.md] package.json node_modules/typebox)"
if [ "$DRY_RUN" = 0 ]; then
  [ -f "$PLUGIN_DIR/dist/index.js" ] || die "dist/index.js missing; the build step did not produce it"
  mkdir -p "$STAGE/node_modules"
  cp -R "$PLUGIN_DIR/dist" "$PLUGIN_DIR/skills" "$STAGE/"
  cp "$PLUGIN_DIR/openclaw.plugin.json" "$PLUGIN_DIR/AGENT_INSTRUCTIONS.md" "$STAGE/"
  [ -f "$PLUGIN_DIR/README.md" ] && cp "$PLUGIN_DIR/README.md" "$STAGE/"
  cp -R "$PLUGIN_DIR/node_modules/typebox" "$STAGE/node_modules/typebox"
  node -e '
    const fs = require("fs");
    const p = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    delete p.devDependencies; delete p.scripts;
    fs.writeFileSync(process.argv[2], JSON.stringify(p, null, 2) + "\n");' \
    "$PLUGIN_DIR/package.json" "$STAGE/package.json"
fi
# Clear any previous copy so a re-run does not nest the directory, then upload.
nc exec -- rm -rf /sandbox/rv-plugin
if [ "$DRY_RUN" = 1 ]; then
  echo "+ (cd $STAGE_PARENT && nemoclaw $NAME upload $STAGE /sandbox/)"
  REMOTE_DIR="/sandbox/rv-plugin"
else
  (cd "$STAGE_PARENT" && run nemoclaw "$NAME" upload "$STAGE" /sandbox/)
  # openshell may place the directory itself or its contents in /sandbox/; find the manifest to be sure.
  manifest="$(nemoclaw "$NAME" exec -- find /sandbox -maxdepth 3 -name openclaw.plugin.json \
              -not -path '*/node_modules/*' -not -path '/sandbox/.openclaw/*' 2>/dev/null | head -1 | tr -d '\r' || true)"
  [ -n "$manifest" ] || die "upload finished but no openclaw.plugin.json was found under /sandbox"
  REMOTE_DIR="$(dirname "$manifest")"
fi
echo "plugin directory in the sandbox: $REMOTE_DIR"
# --force: confirm a local source and allow re-installing over a previous copy.
# shellcheck disable=SC2086
nc exec -- openclaw plugins install "$REMOTE_DIR" --force --accept-capabilities ${INSTALL_EXTRA_FLAGS:-}

# ---------------------------------------------------------------------------------------------
step "4. Plugin config (verifierUrl, token, slackActions) and enable"
# The token travels over stdin into a 0600 file that is deleted right after `config set` reads it:
# it never appears in a command line, shell history or this script's output.
CONFIG_JS='
const p = "plugins.entries.'"$PLUGIN_ID"'.config.";
process.stdout.write(JSON.stringify([
  { path: p + "verifierUrl", value: process.argv[1] },
  { path: p + "token",       value: process.env.RV_TOKEN },
  { path: p + "slackActions", value: process.argv[2] },
  // Bot replies go in the thread of the message that triggered them (design doc H5).
  { path: "channels.slack.replyToMode", value: "all" },
]));'
REMOTE_BATCH="/tmp/rv-config.batch.json"
echo "+ RV_TOKEN=<INTERNAL_TOKEN> node -e '<build batch JSON>' $SANDBOX_URL $SLACK_ACTIONS \\"
echo "    | nemoclaw $NAME exec --stdin -- sh -c 'umask 077; cat > $REMOTE_BATCH'"
echo "+ nemoclaw $NAME exec -- sh -c 'openclaw config set --batch-file $REMOTE_BATCH; rc=\$?; rm -f $REMOTE_BATCH; exit \$rc'"
if [ "$DRY_RUN" = 0 ]; then
  RV_TOKEN="$INTERNAL_TOKEN" node -e "$CONFIG_JS" "$SANDBOX_URL" "$SLACK_ACTIONS" \
    | nemoclaw "$NAME" exec --stdin -- sh -c "umask 077; cat > $REMOTE_BATCH"
  nemoclaw "$NAME" exec -- sh -c "openclaw config set --batch-file $REMOTE_BATCH; rc=\$?; rm -f $REMOTE_BATCH; exit \$rc"
fi
nc exec -- openclaw plugins enable "$PLUGIN_ID"

# ---------------------------------------------------------------------------------------------
step "5. Restart the gateway and wait for Slack"
nc gateway restart
if [ "$DRY_RUN" = 1 ]; then
  echo "+ nemoclaw $NAME channels status --channel slack --wait --timeout 180 --json"
  echo "+ nemoclaw $NAME exec -- openclaw plugins inspect $PLUGIN_ID --runtime --json   # token lines filtered out; expects get_verdict"
fi
fi  # VERIFY_ONLY

if [ "$DRY_RUN" = 0 ]; then
  step "5b. Slack readiness and plugin runtime"
  if ! nemoclaw "$NAME" channels status --channel slack --wait --timeout 180 --json; then
    warn "Slack is not ready. Check SLACK_APP_TOKEN / Socket Mode in the Slack app, and: nemoclaw $NAME logs --tail 100"
    SLACK_FAIL=1
  fi
  inspect="$(nemoclaw "$NAME" exec -- openclaw plugins inspect "$PLUGIN_ID" --runtime --json 2>&1 | grep -vi token || true)"
  if grep -q get_verdict <<<"$inspect"; then
    echo "plugin runtime: OK (tools registered)"
  else
    warn "plugin runtime inspect did not list get_verdict. Output (token lines removed):"
    printf '%s\n' "$inspect" | head -40 >&2
    INSPECT_FAIL=1
  fi
fi

# ---------------------------------------------------------------------------------------------
step "6. Verifier reachability from inside the sandbox"
RESULT=OK
# The token reaches curl through a curl config on stdin (-K -), never through a command line.
if [ "$DRY_RUN" = 1 ]; then
  echo "+ printf 'header = \"X-Verifier-Token: <token>\"' | curl -s -m 3 -o /dev/null -w '%{http_code}' -K - $HOST_URL/internal/health   # host side: is the service up?"
  echo "+ printf 'header = \"X-Verifier-Token: <token>\"' | nemoclaw $NAME exec --stdin -- curl -sS -m 10 -o /dev/null -w '%{http_code}' -K - $SANDBOX_URL/internal/health"
else
  host_code="$(printf 'header = "X-Verifier-Token: %s"\n' "$INTERNAL_TOKEN" \
               | curl -s -m 3 -o /dev/null -w '%{http_code}' -K - "$HOST_URL/internal/health" 2>/dev/null || true)"
  if [ "$host_code" != 200 ]; then
    RESULT=FAIL
    echo "verifier service: NOT RUNNING on $HOST_URL (HTTP ${host_code:-none}; 401 would mean a token mismatch). Start it, listening on all interfaces:"
    echo "  .venv/bin/python -m uvicorn verifier.core.service:app --host 0.0.0.0 --port 8200"
    echo "then re-run: bash scripts/openclaw_install_plugin.sh --verify-only $NAME"
  else
    listen="$(ss -ltn 'sport = :8200' 2>/dev/null | awk 'NR>1{print $4}' | tr '\n' ' ')"
    echo "verifier service: up on host; listening on: ${listen:-unknown}"
    if [[ -n "$listen" && "$listen" != *0.0.0.0:8200* && "$listen" != *"*:8200"* && "$listen" != *"[::]:8200"* ]]; then
      warn "the service listens on loopback only, so the sandbox cannot reach it. Restart uvicorn with --host 0.0.0.0."
    fi
    code="$(printf 'header = "X-Verifier-Token: %s"\n' "$INTERNAL_TOKEN" \
            | nemoclaw "$NAME" exec --stdin -- curl -sS -m 10 -o /dev/null -w '%{http_code}' -K - "$SANDBOX_URL/internal/health" 2>/dev/null || true)"
    case "${code:-}" in
      200) echo "sandbox -> $SANDBOX_URL/internal/health: HTTP 200 OK" ;;
      401) RESULT=FAIL; echo "sandbox reaches the service but the token is rejected (HTTP 401): INTERNAL_TOKEN differs between .env and the running service." ;;
      403) RESULT=FAIL; echo "HTTP 403 from the sandbox proxy: the network policy blocked it. Check: nemoclaw $NAME policy list  (expect $PRESET_NAME)" ;;
      *)   RESULT=FAIL; echo "sandbox cannot reach $SANDBOX_URL (got '${code:-nothing}')."
           echo "  Likely a host firewall: let the Docker bridge reach port 8200, e.g. sudo ufw allow from 172.16.0.0/12 to any port 8200 proto tcp"
           echo "  Also: nemoclaw $NAME doctor" ;;
    esac
  fi
fi

step "Result"
if [ "$DRY_RUN" = 1 ]; then
  echo "dry run: nothing was changed."
  exit 0
fi
if [ "$RESULT" = OK ] && [ -z "${SLACK_FAIL:-}" ] && [ -z "${INSPECT_FAIL:-}" ]; then
  echo "OK: plugin installed, Slack connected, verifier reachable. Click Re-check on a card in Slack."
  echo "If clicks do nothing, retry with: SLACK_ACTIONS=agent bash scripts/openclaw_install_plugin.sh $NAME   (then =off)"
  exit 0
fi
echo "FAIL: see the messages above (verifier reachability: $RESULT, Slack: ${SLACK_FAIL:+not ready}${SLACK_FAIL:-ok}, plugin runtime: ${INSPECT_FAIL:+problem}${INSPECT_FAIL:-ok})."
exit 3
