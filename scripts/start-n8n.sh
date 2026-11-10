#!/usr/bin/env bash
# Dedicated n8n instance for the Lead-to-Delivery CRM deal-won demo.
#
#   scripts/start-n8n.sh start    # run the editor on http://127.0.0.1:5690
#   scripts/start-n8n.sh import   # import n8n/crm-deal-won-demo.json (instance stopped)
#   scripts/start-n8n.sh config   # print the non-secret settings this instance uses
#   scripts/start-n8n.sh n8n ARGS # any n8n CLI command against THIS instance only
#
# Isolation: its own data folder (var/n8n, gitignored, mode 700), its own ports
# (editor 5690, task-runner broker 5691; other local projects use 5678-5681 and
# 5699, and n8n's default broker port 5679) and its own encryption key,
# which n8n generates on first start in var/n8n/.n8n/config (mode 600) and
# reuses afterwards. Never point this at another instance's folder.
#
# Retention: the backend's first response contains an onboarding link with a
# client bearer token, so this instance keeps NO execution data: successful,
# failed and manual executions and in-progress data are all discarded. These
# settings apply to this instance only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
N8N_BIN="${N8N_BIN:-$(command -v n8n || true)}"
[ -n "$N8N_BIN" ] || { echo "n8n not found on PATH" >&2; exit 1; }

umask 077
export N8N_USER_FOLDER="$ROOT/var/n8n"
mkdir -p "$N8N_USER_FOLDER"
chmod 700 "$ROOT/var" "$N8N_USER_FOLDER"

# Network: local only.
export N8N_PORT="${LTD_N8N_PORT:-5690}"
export N8N_RUNNERS_BROKER_PORT="${LTD_N8N_BROKER_PORT:-5691}"  # default 5679 is often taken
export N8N_HOST=127.0.0.1
export N8N_LISTEN_ADDRESS=127.0.0.1
export N8N_PROTOCOL=http
export N8N_SECURE_COOKIE=false            # plain-http localhost editor

# Execution-data retention: keep nothing (values verified against n8n 2.41.7).
export EXECUTIONS_DATA_SAVE_ON_SUCCESS=none
export EXECUTIONS_DATA_SAVE_ON_ERROR=none
export EXECUTIONS_DATA_SAVE_ON_PROGRESS=false
export EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS=false

# Hardening.
export N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true
export N8N_BLOCK_ENV_ACCESS_IN_NODE=true  # no $env in expressions/Code
export N8N_DIAGNOSTICS_ENABLED=false
export N8N_VERSION_NOTIFICATIONS_ENABLED=false
export N8N_TEMPLATES_ENABLED=false
export N8N_PERSONALIZATION_ENABLED=false
export N8N_HIRING_BANNER_ENABLED=false
export N8N_LOG_LEVEL=info
export N8N_LOG_OUTPUT=console

port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

case "${1:-start}" in
  start)
    for port in "$N8N_PORT" "$N8N_RUNNERS_BROKER_PORT"; do
      if port_in_use "$port"; then
        echo "Port $port is already in use; refusing to start (is another n8n running?)." >&2
        exit 1
      fi
    done
    echo "Lead-to-Delivery n8n: http://127.0.0.1:$N8N_PORT  (data: var/n8n, executions not saved)"
    exec "$N8N_BIN" start
    ;;
  import)
    if port_in_use "$N8N_PORT"; then
      echo "Stop the instance on port $N8N_PORT before importing." >&2
      exit 1
    fi
    exec "$N8N_BIN" import:workflow --input="$ROOT/n8n/crm-deal-won-demo.json"
    ;;
  config)
    env | grep -E '^(N8N_USER_FOLDER|N8N_PORT|N8N_RUNNERS_BROKER_PORT|N8N_HOST|N8N_LISTEN_ADDRESS|EXECUTIONS_DATA_SAVE_[A-Z_]+|N8N_BLOCK_ENV_ACCESS_IN_NODE|N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS)=' | sort
    ;;
  n8n)
    shift
    exec "$N8N_BIN" "$@"
    ;;
  *)
    echo "usage: $0 [start|import|config|n8n ARGS...]" >&2
    exit 2
    ;;
esac
