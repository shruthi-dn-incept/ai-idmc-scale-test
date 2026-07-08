#!/bin/bash
# ACA Job entrypoint: start MCP servers, then run the hardened full-catalog runner.
# Args: $1 = workers (default 8), $2 = table limit (number, or empty/"all" = full catalog)
set -e

echo "=== Starting governance_engine_mcp (:9765) ==="
python governance_engine_mcp.py &
echo "=== Starting ai_governance_mcp (:9770) ==="
python ai_governance_mcp.py &

echo "=== Waiting for MCP servers ==="
for PORT in 9765 9770; do
  for i in $(seq 1 40); do
    if python -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),1); s.close()" 2>/dev/null; then
      echo "  Port $PORT ready"; break
    fi
    sleep 2
  done
done

WORKERS="${1:-8}"
LIMIT="${2:-all}"
echo "=== Running scale_full_pipeline (workers=$WORKERS limit=$LIMIT) ==="
if [ "$LIMIT" = "all" ]; then
  python scale_full_pipeline.py --workers "$WORKERS"
else
  python scale_full_pipeline.py --workers "$WORKERS" --limit "$LIMIT"
fi
echo "=== Done ==="
