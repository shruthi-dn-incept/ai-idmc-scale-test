#!/bin/bash
# Entrypoint for the UI Docker container.
# Starts both MCP servers, waits for them, then runs governance_ui in foreground.
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

echo "=== Starting governance_engine_mcp on :9765 ==="
python governance_engine_mcp.py &

echo "=== Starting ai_governance_mcp on :9770 ==="
python ai_governance_mcp.py &

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

echo "=== Starting governance_ui on :9080 ==="
exec python governance_ui.py
