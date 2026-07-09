#!/bin/bash
# ACA Job entrypoint: run the full end-to-end scale pipeline in-container (on Azure).
# Materializes .env from injected env vars (secrets stay out of the image), then
# runs the orchestrator which does extract -> taxonomy -> domain -> system/dataset
# -> DQRO import -> curate -> DQ scan, collecting stats.json.
# Args: passed through to the orchestrator (e.g. "--clean")
set -e

env | grep -E '^(IDMC_|CDGC_|CDQ_|SNOWFLAKE_|ANTHROPIC_)' > .env
echo "=== materialized .env with $(wc -l < .env) keys ==="

# Pipeline args come via PIPELINE_ARGS env var (avoids az --args flag-parsing of --skip).
echo "=== run_scale_pipeline ${PIPELINE_ARGS} $@ ==="
python -u -m idmc_governance.scale.orchestrator ${PIPELINE_ARGS} "$@"
echo "=== pipeline entrypoint done ==="
