#!/bin/bash
# Local, offline OpenShell gateway for the run-check sandbox (Docker compute driver, mTLS).
# Verified with OpenShell 0.0.116 on the GB10. State lives in $RV_OPENSHELL_HOME.
#
#   bash verifier/stack/openshell/gateway.sh setup     # once: certs + CLI gateway entry
#   nohup sg docker -c "bash verifier/stack/openshell/gateway.sh start" > gateway.log 2>&1 &
#   bash verifier/stack/openshell/gateway.sh stop
#
# Then run the verifier with SANDBOX_BACKEND=openshell and the same XDG_* variables as below
# (`source <(bash verifier/stack/openshell/gateway.sh env)`), so the CLI finds this gateway.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OS_HOME="${RV_OPENSHELL_HOME:-$HOME/.rv-openshell}"
TLS="$OS_HOME/xdg/state/openshell/tls"
PORT="${RV_OPENSHELL_PORT:-18080}"

client_env() {
  echo "export PATH=\$HOME/.local/bin:\$PATH"
  echo "export XDG_STATE_HOME=$OS_HOME/xdg/state XDG_CONFIG_HOME=$OS_HOME/xdg/config"
}

case "${1:-}" in
  env)
    client_env ;;
  setup)
    eval "$(client_env)"
    mkdir -p "$TLS"
    openshell-gateway generate-certs --output-dir "$TLS" --server-san host.openshell.internal \
      --server-san 127.0.0.1 --server-san localhost
    openshell gateway add "https://127.0.0.1:$PORT" --local --name openshell ;;
  start)
    eval "$(client_env)"
    export OPENSHELL_DRIVERS=docker
    export OPENSHELL_BIND_ADDRESS=127.0.0.1
    export OPENSHELL_SERVER_PORT="$PORT"
    export OPENSHELL_DB_URL="sqlite:$OS_HOME/openshell.db"
    export OPENSHELL_GRPC_ENDPOINT="https://host.openshell.internal:$PORT"
    export OPENSHELL_DOCKER_SUPERVISOR_BIN="$HOME/.local/bin/openshell-sandbox"
    export OPENSHELL_LOCAL_TLS_DIR="$TLS"
    export OPENSHELL_TLS_CERT="$TLS/server/tls.crt" OPENSHELL_TLS_KEY="$TLS/server/tls.key"
    export OPENSHELL_TLS_CLIENT_CA="$TLS/ca.crt"
    export OPENSHELL_ENABLE_MTLS_AUTH=true   # docker sandboxes refuse a plaintext gateway
    export OPENSHELL_DOCKER_TLS_CA="$TLS/ca.crt"
    export OPENSHELL_DOCKER_TLS_CERT="$TLS/client/tls.crt" OPENSHELL_DOCKER_TLS_KEY="$TLS/client/tls.key"
    export OPENSHELL_GATEWAY_CONFIG="$HERE/gateway.toml"   # sandbox_pids_limit = 256
    exec openshell-gateway ;;
  stop)
    # The process name is truncated to 15 characters, so `pkill -x openshell-gateway` misses it.
    pkill -x openshell-gatew || true ;;
  *)
    echo "usage: $0 setup|start|stop|env" >&2; exit 2 ;;
esac
