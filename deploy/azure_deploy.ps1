# deploy/azure_deploy.ps1
# Run from the repo root in any PowerShell terminal where 'az' is available.
# Usage: .\deploy\azure_deploy.ps1

$ErrorActionPreference = "Stop"

# ── Config ─────────────────────────────────────────────────────────────────────
$SUBSCRIPTION = "7a42e0f2-3b2f-4b16-8bf2-458746103d58"
$RG           = "govtest-scale-rg"
$LOCATION     = "eastus"
$ACR_NAME     = "govtestscaleacr"   # must be globally unique, lowercase, no hyphens
$ACA_ENV      = "govtest-env"
$JOB_NAME     = "govtest-scale-job"
$IMAGE_TAG    = "governance-stack:latest"

Write-Host "`n=== Azure Container Apps Deployment ===" -ForegroundColor Cyan
Write-Host "  Subscription : $SUBSCRIPTION"
Write-Host "  Resource group: $RG / $LOCATION"
Write-Host "  ACR          : $ACR_NAME"
Write-Host ""

# ── 1. Set subscription ───────────────────────────────────────────────────────
Write-Host "[1/6] Setting subscription..." -ForegroundColor Yellow
az account set --subscription $SUBSCRIPTION

# ── 2. Create ACR ─────────────────────────────────────────────────────────────
Write-Host "[2/6] Creating Container Registry '$ACR_NAME'..." -ForegroundColor Yellow
az acr create `
  --resource-group $RG `
  --name $ACR_NAME `
  --sku Basic `
  --admin-enabled true `
  --output none
Write-Host "  Done." -ForegroundColor Green

# ── 3. Build & push image via ACR Tasks (no local Docker needed) ──────────────
Write-Host "[3/6] Building image in ACR (this takes ~5 min)..." -ForegroundColor Yellow
az acr build `
  --registry $ACR_NAME `
  --image $IMAGE_TAG `
  --file Dockerfile `
  .
Write-Host "  Image pushed." -ForegroundColor Green

# ── 4. Create Container Apps environment ──────────────────────────────────────
Write-Host "[4/6] Creating Container Apps environment..." -ForegroundColor Yellow
$ErrorActionPreference = "Continue"
az containerapp env create `
  --name $ACA_ENV `
  --resource-group $RG `
  --location $LOCATION `
  --output none 2>$null
$ErrorActionPreference = "Stop"
Write-Host "  Done (created or already existed)." -ForegroundColor Green

# ── 5. Get ACR credentials ────────────────────────────────────────────────────
Write-Host "[5/6] Fetching ACR credentials..." -ForegroundColor Yellow
$ACR_SERVER = "$ACR_NAME.azurecr.io"
$ACR_USER   = az acr credential show --name $ACR_NAME --query username -o tsv
$ACR_PASS   = az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv

# ── 6. Load secrets from .env.docker ──────────────────────────────────────────
Write-Host "[6/6] Reading .env.docker and deploying ACA Job..." -ForegroundColor Yellow
$env_vars = @{}
Get-Content ".env.docker" | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
    $parts = $_ -split "=", 2
    if ($parts.Count -eq 2) { $env_vars[$parts[0].Trim()] = $parts[1].Trim() }
}

$REMOTE_IMAGE = "$ACR_SERVER/$IMAGE_TAG"

# Delete existing job if present (to allow clean update)
$ErrorActionPreference = "Continue"
$jobExists = az containerapp job show --name $JOB_NAME --resource-group $RG --query name -o tsv 2>$null
$ErrorActionPreference = "Stop"
if ($jobExists) {
    Write-Host "  Deleting existing job for fresh deploy..." -ForegroundColor Gray
    az containerapp job delete --name $JOB_NAME --resource-group $RG --yes --output none
}

az containerapp job create `
  --name $JOB_NAME `
  --resource-group $RG `
  --environment $ACA_ENV `
  --trigger-type Manual `
  --replica-timeout 7200 `
  --replica-retry-limit 1 `
  --image $REMOTE_IMAGE `
  --cpu 2 --memory "4Gi" `
  --registry-server $ACR_SERVER `
  --registry-username $ACR_USER `
  --registry-password $ACR_PASS `
  --env-vars `
    "IDMC_USER=$($env_vars['IDMC_USER'])" `
    "IDMC_LOGIN_HOST=$($env_vars['IDMC_LOGIN_HOST'])" `
    "IDMC_SERVER_URL=$($env_vars['IDMC_SERVER_URL'])" `
    "IDMC_ORG_ID=$($env_vars['IDMC_ORG_ID'])" `
    "CDGC_API_BASE=$($env_vars['CDGC_API_BASE'])" `
    "SNOWFLAKE_ACCOUNT=$($env_vars['SNOWFLAKE_ACCOUNT'])" `
    "SNOWFLAKE_USER=$($env_vars['SNOWFLAKE_USER'])" `
    "SNOWFLAKE_WAREHOUSE=INCEPT_WH" `
    "SNOWFLAKE_ROLE=ACCOUNTADMIN" `
    "SNOWFLAKE_GOVTEST_DB=GOVERNANCE_SCALE_TEST" `
    "ANTHROPIC_API_KEY=$($env_vars['ANTHROPIC_API_KEY'])" `
    "GOVERNANCE_ENGINE_URL=http://127.0.0.1:9765/mcp" `
    "AI_GOVERNANCE_URL=http://127.0.0.1:9770/mcp" `
  --secrets `
    "idmc-pass=$($env_vars['IDMC_PASS'])" `
    "sf-password=$($env_vars['SNOWFLAKE_PASSWORD'])" `
  --output none

Write-Host ""
Write-Host "=== Deployment complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "To trigger the scale test:" -ForegroundColor White
Write-Host "  az containerapp job start --name $JOB_NAME --resource-group $RG" -ForegroundColor Green
Write-Host ""
Write-Host "To watch execution status:" -ForegroundColor White
Write-Host "  az containerapp job execution list --name $JOB_NAME --resource-group $RG -o table" -ForegroundColor Green
