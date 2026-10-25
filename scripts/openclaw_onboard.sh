#!/usr/bin/env bash
# Onboard a NemoClaw sandbox that runs OpenClaw, wired to our Slack app and to the vLLM that is
# already running on this host. INTERACTIVE: run it yourself in a terminal; you accept the
# third-party-software terms in the wizard (this script never auto-accepts them).
#
#   bash scripts/openclaw_onboard.sh [sandbox-name]     # default name: rv-openclaw
#   bash scripts/openclaw_onboard.sh --dry-run          # checks everything, prints the command, runs nothing
#
# Reads SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID (and optionally SLACK_ALLOWED_USERS) from
# .env (override the file with ENV_FILE=/path). Token values are never printed.
# Afterwards:  bash scripts/openclaw_install_plugin.sh <sandbox-name>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"

DRY_RUN=0
NAME=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) NAME="$arg" ;;
  esac
done
NAME="${NAME:-rv-openclaw}"
if ! [[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
  echo "sandbox name must be lowercase letters, digits and hyphens: $NAME" >&2; exit 2
fi

step() { printf '\n== %s ==\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Value of KEY in the env file (python-dotenv: no shell evaluation of the file). Empty if unset.
PYTHON="$REPO_ROOT/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
envval() {
  [ -f "$ENV_FILE" ] || return 0
  ENV_FILE="$ENV_FILE" "$PYTHON" -c '
import os, sys
from dotenv import dotenv_values
print(dotenv_values(os.environ["ENV_FILE"]).get(sys.argv[1]) or "", end="")' "$1"
}

# ---------------------------------------------------------------------------------------------
step "1. Slack settings in $ENV_FILE"
[ -f "$ENV_FILE" ] || die "$ENV_FILE does not exist"
[ -n "$PYTHON" ] || die "python3 (with python-dotenv, e.g. the repo .venv) is needed to read the env file"

SLACK_BOT_TOKEN="$(envval SLACK_BOT_TOKEN)"
SLACK_APP_TOKEN="$(envval SLACK_APP_TOKEN)"
SLACK_CHANNEL_ID="$(envval SLACK_CHANNEL_ID)"
SLACK_ALLOWED_USERS="$(envval SLACK_ALLOWED_USERS)"

missing=0
if [[ "$SLACK_BOT_TOKEN" != xoxb-* ]]; then
  missing=1
  echo "MISSING: SLACK_BOT_TOKEN (must start with xoxb-). Slack app -> OAuth & Permissions -> Bot User OAuth Token." >&2
fi
if [[ "$SLACK_APP_TOKEN" != xapp-* ]]; then
  missing=1
  echo "MISSING: SLACK_APP_TOKEN. Add SLACK_APP_TOKEN=xapp-... to $ENV_FILE:" >&2
  echo "         Slack app -> Basic Information -> App-Level Tokens -> Generate Token, scope connections:write." >&2
  echo "         Also turn on Socket Mode and Interactivity & Shortcuts in the same Slack app, then reinstall it." >&2
fi
if [ -z "$SLACK_CHANNEL_ID" ]; then
  missing=1
  echo "MISSING: SLACK_CHANNEL_ID (the C... id of the verifier channel; it becomes the Slack allowlist)." >&2
fi
[ "$missing" = 0 ] || exit 1
echo "SLACK_BOT_TOKEN: present   SLACK_APP_TOKEN: present   SLACK_CHANNEL_ID: $SLACK_CHANNEL_ID" \
     "  SLACK_ALLOWED_USERS: ${SLACK_ALLOWED_USERS:-(not set: any member of the allowed channel)}"

# ---------------------------------------------------------------------------------------------
step "2. Tools and the running vLLM"
command -v nemoclaw >/dev/null || die "nemoclaw not found (expected in ~/.local/bin; PATH is $PATH)"
command -v openshell >/dev/null || die "openshell not found (expected in ~/.local/bin)"

# Docker in this shell? Some shells lack the docker group; retry once under `sg docker`.
if ! docker info >/dev/null 2>&1; then
  if [ -z "${RV_IN_SG:-}" ] && command -v sg >/dev/null && sg docker -c "docker info" >/dev/null 2>&1; then
    echo "docker needs the docker group here; re-running under: sg docker"
    flag=""; [ "$DRY_RUN" = 1 ] && flag="--dry-run"
    exec sg docker -c "RV_IN_SG=1 ENV_FILE=$(printf '%q' "$ENV_FILE") bash $(printf '%q' "${BASH_SOURCE[0]}") $flag $(printf '%q' "$NAME")"
  fi
  die "docker is not usable from this shell (try: sg docker -c 'bash scripts/openclaw_onboard.sh')"
fi

VLLM_PORT="${NEMOCLAW_VLLM_PORT:-8000}"
MODEL="${RV_OPENCLAW_MODEL:-nvidia/Qwen3.6-35B-A3B-NVFP4}"
models_json="$(curl -sf -m 5 "http://localhost:$VLLM_PORT/v1/models")" \
  || die "vLLM does not answer on http://localhost:$VLLM_PORT/v1/models (restart: sg docker -c 'bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh')"
grep -qF "\"id\":\"$MODEL\"" <<<"$models_json" \
  || die "vLLM is up but does not serve $MODEL (it serves: $(grep -o '"id":"[^"]*"' <<<"$models_json" | head -3 | tr '\n' ' '))"
echo "vLLM on :$VLLM_PORT serves $MODEL (it will be reused; nothing is stopped or downloaded)."

if nemoclaw list --json 2>/dev/null | grep -q "\"$NAME\""; then
  die "a NemoClaw sandbox named '$NAME' already exists. Pick another name, or: nemoclaw $NAME destroy"
fi

# ---------------------------------------------------------------------------------------------
step "3. Environment handed to the wizard"
# Slack: the wizard's Messaging step reads these (docs: manage-sandboxes/set-up-slack.mdx,
# enable-channels-during-onboarding.mdx). Only the exports below are set; nothing is auto-confirmed.
export SLACK_BOT_TOKEN SLACK_APP_TOKEN
export SLACK_ALLOWED_CHANNELS="$SLACK_CHANNEL_ID"
if [ -n "$SLACK_ALLOWED_USERS" ]; then export SLACK_ALLOWED_USERS; else unset SLACK_ALLOWED_USERS; fi
# Inference: NEMOCLAW_PROVIDER=vllm makes "Local vLLM" the default menu entry (source:
# src/lib/onboard/provider-selection-prompt.ts); the wizard still asks you to confirm. NEMOCLAW_MODEL
# matches what /v1/models reports, so a model prompt (if any) defaults to it.
export NEMOCLAW_PROVIDER=vllm
export NEMOCLAW_MODEL="$MODEL"
export NEMOCLAW_VLLM_PORT="$VLLM_PORT"
echo "agent=openclaw  sandbox=$NAME  provider=vllm  model=$MODEL"
echo "Slack allowlist: channels=$SLACK_ALLOWED_CHANNELS users=${SLACK_ALLOWED_USERS:-(unset)}"

cat <<GUIDE

== What the wizard will ask, and what to answer ==
  Third-party software notice ........ read it, then accept (this script does not accept it for you)
  Inference provider ................. "Local vLLM" (item pre-selected; it reuses the server on :$VLLM_PORT)
  Model .............................. $MODEL (the one vLLM reports; keep the default)
  Sandbox name ....................... $NAME (already passed with --name)
  Messaging channels ................. press the number for Slack to toggle it ON, then Enter.
                                       It reuses the tokens and allowlist exported above. Leave the others off.
                                       If Slack validation fails, onboarding skips Slack: check both tokens and rerun.
  Policy tier ........................ Balanced (default). The Slack preset is added for you.
  Policy presets screen .............. keep the defaults. Our Resume Verifier preset is added afterwards by
                                       scripts/openclaw_install_plugin.sh, not here.
  Review / apply configuration ....... Apply configuration
If it is interrupted, resume with:  nemoclaw onboard --resume
If Apply fails with "Native local inference requires providers_v2_enabled=true on gateway 'nemoclaw'",
enable it on NemoClaw's gateway (port 8080; our run-check gateway is separate), then resume:
  openshell settings set -g nemoclaw --global --key providers_v2_enabled --value true --yes
The vLLM "max_model_len=262144 ... unified-memory headroom" warning is advisory; onboarding continues.
GUIDE

# ---------------------------------------------------------------------------------------------
step "4. Launch"
cmd=(nemoclaw onboard --agent openclaw --name "$NAME")
if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] would exec: ${cmd[*]}"
  exit 0
fi
rc=0
"${cmd[@]}" || rc=$?
if [ "$rc" = 0 ]; then
  printf '\nOnboarding finished. Next:\n  bash scripts/openclaw_install_plugin.sh %s\n' "$NAME"
else
  printf '\nnemoclaw onboard exited with status %s. Fix the reported problem, then: nemoclaw onboard --resume\n' "$rc" >&2
fi
exit "$rc"
