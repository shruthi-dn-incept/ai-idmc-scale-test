#!/bin/bash
# Starts MCP servers then runs the scale test.
# Used as the ACA Job entrypoint.
set -e

echo "=== Starting governance_engine_mcp on port 9765 ==="
python governance_engine_mcp.py &
PID_ENGINE=$!

echo "=== Starting ai_governance_mcp on port 9770 ==="
python ai_governance_mcp.py &
PID_AI=$!

# Wait for both servers to be ready
echo "=== Waiting for MCP servers to be ready ==="
for PORT in 9765 9770; do
  for i in $(seq 1 30); do
    if python -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),1); s.close()" 2>/dev/null; then
      echo "  Port $PORT ready"
      break
    fi
    echo "  Waiting for port $PORT (attempt $i)..."
    sleep 2
  done
done

echo "=== Running scale test ==="
python run_scale_test.py --tiers 100,500,1000,4000

echo "=== Done ==="
