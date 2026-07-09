# Build context is the repo root. Build with: az acr build --file docker/Dockerfile .
FROM python:3.11-slim

WORKDIR /app

# Copy the whole repo (.dockerignore excludes secrets, caches, generated files)
COPY . .

# Editable install of the idmc_governance package (deps come from pyproject.toml)
RUN pip install --no-cache-dir -e .

# Default: scale pipeline. ACA Jobs override --command/--args at deploy time
# (e.g. scripts/start_bulk_import.sh). UI uses docker/Dockerfile.ui.
CMD ["bash", "scripts/start_scale_pipeline.sh"]
