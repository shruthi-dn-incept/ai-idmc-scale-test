#!/bin/bash
# ACA Job entrypoint: bulk-import a DQRO .xlsx into CDGC via the import API + poll.
# Writes hit CDGC (local 503s), so this runs on Azure.
# Args: $1 = path to the .xlsx (default templates/CDGC_DQRO_FULL.xlsx)
#       $2 = validation policy (default CONTINUE_ON_ERROR_WARNING)
set -e

FILE="${1:-templates/CDGC_DQRO_FULL.xlsx}"
POLICY="${2:-CONTINUE_ON_ERROR_WARNING}"

# The auth layer (governance_engine_mcp) reads creds from /app/.env, not os.environ.
# Materialize it from the injected env vars so secrets stay OUT of the image.
env | grep -E '^(IDMC_|CDGC_|CDQ_)' > .env
echo "=== materialized .env with $(wc -l < .env) keys ==="

echo "=== CDGC bulk import: $FILE (policy=$POLICY) ==="
python cdgc_bulk_import.py "$FILE" --policy "$POLICY"
echo "=== Done ==="
