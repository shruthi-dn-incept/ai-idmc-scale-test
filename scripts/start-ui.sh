#!/bin/bash
# Entrypoint for the UI Docker container.
# Starts both MCP servers, waits for them, then runs the UI in foreground.
set -e

export IDMC_FRS_HOST="${IDMC_FRS_HOST:-dmp-us.informaticacloud.com}"
export IDMC_DQ_HOST="${IDMC_DQ_HOST:-usw1-dqcloud.dmp-us.informaticacloud.com}"
export IDMC_IDENTITY_HOST="${IDMC_IDENTITY_HOST:-dmp-us.informaticacloud.com}"
export CDGC_API_BASE="${CDGC_API_BASE:-https://cdgc-api.dmp-us.informaticacloud.com}"
export AI_GOVERNANCE_URL="http://127.0.0.1:9770/mcp"
export GOVERNANCE_ENGINE_URL="http://127.0.0.1:9765/mcp"
export GOVERNANCE_MCP_PORT=9765
export AI_GOVERNANCE_MCP_PORT=9770
export GOVERNANCE_UI_PORT=9080
export GOVERNANCE_UI_HOST=0.0.0.0

# Servers read creds from a repo-root .env (not os.environ). Materialize it from
# the injected env vars so secrets stay OUT of the image.
env | grep -E '^(IDMC_|CDGC_|CDQ_|ANTHROPIC_)' > .env
echo "=== materialized .env with $(wc -l < .env) keys ==="

echo "=== Starting governance_engine on :9765 ==="
python -m idmc_governance.servers.governance_engine &

echo "=== Starting ai_governance on :9770 ==="
python -m idmc_governance.servers.ai_governance &

echo "=== Waiting for MCP servers ==="
for PORT in 9765 9770; do
  for i in $(seq 1 30); do
    if python -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),1); s.close()" 2>/dev/null; then
      echo "  :$PORT ready"
      break
    fi
    echo "  Waiting for :$PORT (attempt $i)..."
    sleep 2
  done
done

echo "=== Starting UI on :9080 ==="
exec python -m idmc_governance.ui.app
