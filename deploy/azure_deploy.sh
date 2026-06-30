#!/usr/bin/env bash
# deploy/azure_deploy.sh
# Deploys the governance stack to Azure Container Apps as a Job.
# Pre-reqs: az CLI logged in, Docker running locally.
# Usage: bash deploy/azure_deploy.sh

set -euo pipefail

# ── Config (from .env or overridden here) ─────────────────────────────────────
SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-7a42e0f2-3b2f-4b16-8bf2-458746103d58}"
TENANT="${AZURE_TENANT_ID:-886f2e5c-ba78-477b-8f74-83bfc5e23cd2}"
RG="${AZURE_RESOURCE_GROUP:-govtest-scale-rg}"
LOCATION="${AZURE_LOCATION:-eastus}"
ACR_NAME="${AZURE_ACR_NAME:-govtestacr}"          # must be globally unique, lowercase
ACA_ENV="${AZURE_ACA_ENV:-govtest-env}"
IMAGE_TAG="governance-stack:latest"
REMOTE_IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_TAG}"

echo "=== Azure Container Apps Deployment ==="
echo "  Subscription : $SUBSCRIPTION"
echo "  Resource group: $RG"
echo "  Location     : $LOCATION"
echo "  ACR          : $ACR_NAME"
echo ""

# ── 1. Ensure we're on the right subscription ─────────────────────────────────
az account set --subscription "$SUBSCRIPTION"

# ── 2. Create resource group (idempotent) ─────────────────────────────────────
echo "[1/7] Resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

# ── 3. Create Azure Container Registry ───────────────────────────────────────
echo "[2/7] Container registry..."
az acr create \
  --resource-group "$RG" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output none

# ── 4. Build & push image ─────────────────────────────────────────────────────
echo "[3/7] Building image..."
az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_TAG" \
  --file Dockerfile \
  .

# ── 5. Create Container Apps environment ──────────────────────────────────────
echo "[4/7] Container Apps environment..."
az containerapp env create \
  --name "$ACA_ENV" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --output none 2>/dev/null || echo "  (already exists)"

# ── 6. Pull ACR credentials for secret wiring ─────────────────────────────────
echo "[5/7] Fetching ACR credentials..."
ACR_SERVER="${ACR_NAME}.azurecr.io"
ACR_USER=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# ── 7. Load secrets from .env.docker ──────────────────────────────────────────
echo "[6/7] Reading secrets from .env.docker..."
source .env.docker

# ── 8. Deploy as ACA Job ──────────────────────────────────────────────────────
# A Job runs to completion then stops — scale-to-zero, no idle cost.
echo "[7/7] Deploying Container Apps Job..."

az containerapp job create \
  --name "govtest-scale-job" \
  --resource-group "$RG" \
  --environment "$ACA_ENV" \
  --trigger-type Manual \
  --replica-timeout 7200 \
  --image "$REMOTE_IMAGE" \
  --cpu 2 --memory 4Gi \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_USER" \
  --registry-password "$ACR_PASS" \
  --env-vars \
    "IDMC_USER=$IDMC_USER" \
    "IDMC_LOGIN_HOST=$IDMC_LOGIN_HOST" \
    "IDMC_SERVER_URL=$IDMC_SERVER_URL" \
    "IDMC_ORG_ID=$IDMC_ORG_ID" \
    "CDGC_API_BASE=$CDGC_API_BASE" \
    "SNOWFLAKE_ACCOUNT=$SNOWFLAKE_ACCOUNT" \
    "SNOWFLAKE_USER=$SNOWFLAKE_USER" \
    "SNOWFLAKE_WAREHOUSE=$SNOWFLAKE_WAREHOUSE" \
    "SNOWFLAKE_ROLE=$SNOWFLAKE_ROLE" \
    "SNOWFLAKE_GOVTEST_DB=$SNOWFLAKE_GOVTEST_DB" \
    "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
    "GOVERNANCE_MCP_PORT=9765" \
    "AI_GOVERNANCE_MCP_PORT=9770" \
    "GOVERNANCE_UI_PORT=9080" \
  --secrets \
    "idmc-pass=$IDMC_PASS" \
    "sf-password=$SNOWFLAKE_PASSWORD" \
  --command "python" "run_scale_test.py" \
  --output none 2>/dev/null || \
az containerapp job update \
  --name "govtest-scale-job" \
  --resource-group "$RG" \
  --image "$REMOTE_IMAGE" \
  --output none

echo ""
echo "=== Deployment complete ==="
echo ""
echo "To trigger a scale test run:"
echo "  az containerapp job start --name govtest-scale-job --resource-group $RG"
echo ""
echo "To tail logs:"
echo "  az containerapp job execution list --name govtest-scale-job --resource-group $RG -o table"
