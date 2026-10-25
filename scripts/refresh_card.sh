#!/bin/bash
# Re-render an application's Slack card in place from its saved verdict (no new post).
# Use it when OpenClaw stripped the card's buttons: a click that lands while the plugin isn't
# loaded (e.g. during a gateway restart) makes OpenClaw replace the actions block.
#
#   RUNS_DIR=/tmp/rv-fullrun-XXXX bash scripts/refresh_card.sh <application_id>
set -euo pipefail
cd "$(dirname "$0")/.."
[ -n "${RUNS_DIR:-}" ] && [ -n "${1:-}" ] || { echo "usage: RUNS_DIR=<runs dir> $0 <application_id>" >&2; exit 2; }
export VERIFIER_REAL="${VERIFIER_REAL:-render,slack,ats}"
.venv/bin/python - "$1" <<'EOF'
import sys
from dotenv import load_dotenv
load_dotenv(".env")
from verifier.core import pipeline
flow = pipeline._load_flow(sys.argv[1])
if flow is None or flow.card is None:
    sys.exit(f"no card for application {sys.argv[1]} in this RUNS_DIR")
flow.refresh_card()
print("card re-rendered:", flow.card)
EOF
