# deploy/azure_deploy_ui.ps1
# Deploys the governance UI as a long-running Azure Container App on port 9080.
# Usage: .\deploy\azure_deploy_ui.ps1
# Run from repo root where az CLI is available.

$ErrorActionPreference = "Stop"

# ── Config (override via .env: AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP / AZURE_LOCATION / AZURE_ACR_NAME) ──
. "$PSScriptRoot\_load_env.ps1"
$cfg = Import-DotEnv
$SUBSCRIPTION = Get-EnvOr $cfg 'AZURE_SUBSCRIPTION_ID' '7a42e0f2-3b2f-4b16-8bf2-458746103d58'
$RG           = Get-EnvOr $cfg 'AZURE_RESOURCE_GROUP'  'govtest-scale-rg'
$LOCATION     = Get-EnvOr $cfg 'AZURE_LOCATION'        'eastus'
$ACR_NAME     = Get-EnvOr $cfg 'AZURE_ACR_NAME'        'govtestscaleacr'
$ACA_ENV      = "govtest-env"
$APP_NAME     = "govtest-ui"
$IMAGE_TAG    = "governance-ui:latest"

Write-Host "`n=== Azure Container App UI Deployment ===" -ForegroundColor Cyan

# ── 1. Set subscription ───────────────────────────────────────────────────────
Write-Host "[1/6] Setting subscription..." -ForegroundColor Yellow
az account set --subscription $SUBSCRIPTION

# ── 2. Ensure ACR exists ──────────────────────────────────────────────────────
Write-Host "[2/6] Ensuring ACR '$ACR_NAME' exists..." -ForegroundColor Yellow
az acr create --resource-group $RG --name $ACR_NAME --sku Basic --admin-enabled true --output none 2>$null
Write-Host "  Done." -ForegroundColor Green

# ── 3. Build & push via ACR Tasks ────────────────────────────────────────────
Write-Host "[3/6] Building image in ACR (takes ~5 min)..." -ForegroundColor Yellow
az acr build `
  --registry $ACR_NAME `
  --image $IMAGE_TAG `
  --file docker/Dockerfile.ui `
  .
Write-Host "  Image pushed: $ACR_NAME.azurecr.io/$IMAGE_TAG" -ForegroundColor Green

# ── 4. Ensure Container Apps environment ──────────────────────────────────────
Write-Host "[4/6] Ensuring Container Apps environment '$ACA_ENV'..." -ForegroundColor Yellow
$ErrorActionPreference = "Continue"
az containerapp env create --name $ACA_ENV --resource-group $RG --location $LOCATION --output none 2>$null
$ErrorActionPreference = "Stop"
Write-Host "  Done." -ForegroundColor Green

# ── 5. Get ACR credentials ────────────────────────────────────────────────────
Write-Host "[5/6] Fetching ACR credentials..." -ForegroundColor Yellow
$ACR_SERVER = "$ACR_NAME.azurecr.io"
$ACR_USER   = az acr credential show --name $ACR_NAME --query username -o tsv
$ACR_PASS   = az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv

# ── 6. Read .env and deploy Container App ─────────────────────────────────────
Write-Host "[6/6] Deploying Container App '$APP_NAME'..." -ForegroundColor Yellow

$env_vars = @{}
Get-Content ".env" | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
    $parts = $_ -split "=", 2
    if ($parts.Count -eq 2 -and $parts[1].Trim() -ne "") {
        $env_vars[$parts[0].Trim()] = $parts[1].Trim()
    }
}

$REMOTE_IMAGE = "$ACR_SERVER/$IMAGE_TAG"

# Delete existing app to allow clean update
$ErrorActionPreference = "Continue"
$appExists = az containerapp show --name $APP_NAME --resource-group $RG --query name -o tsv 2>$null
$ErrorActionPreference = "Stop"
if ($appExists) {
    Write-Host "  Deleting existing app for fresh deploy..." -ForegroundColor Gray
    az containerapp delete --name $APP_NAME --resource-group $RG --yes --output none
}

az containerapp create `
  --name $APP_NAME `
  --resource-group $RG `
  --environment $ACA_ENV `
  --image $REMOTE_IMAGE `
  --target-port 9080 `
  --ingress external `
  --min-replicas 1 `
  --max-replicas 1 `
  --cpu 2 --memory "4Gi" `
  --registry-server $ACR_SERVER `
  --registry-username $ACR_USER `
  --registry-password $ACR_PASS `
  --env-vars `
    "IDMC_USER=$($env_vars['IDMC_USER'])" `
    "IDMC_LOGIN_HOST=$($env_vars['IDMC_LOGIN_HOST'])" `
    "IDMC_FRS_HOST=$($env_vars['IDMC_FRS_HOST'])" `
    "IDMC_DQ_HOST=$($env_vars['IDMC_DQ_HOST'])" `
    "IDMC_IDENTITY_HOST=$($env_vars['IDMC_IDENTITY_HOST'])" `
    "IDMC_SERVER_URL=$($env_vars['IDMC_SERVER_URL'])" `
    "IDMC_ORG_ID=$($env_vars['IDMC_ORG_ID'])" `
    "IDMC_SESSION_ID=$($env_vars['IDMC_SESSION_ID'])" `
    "IDMC_JWT=$($env_vars['IDMC_JWT'])" `
    "IDMC_JWT_MINTED_AT=$($env_vars['IDMC_JWT_MINTED_AT'])" `
    "CDGC_API_BASE=$($env_vars['CDGC_API_BASE'])" `
    "SNOWFLAKE_ACCOUNT=$($env_vars['SNOWFLAKE_ACCOUNT'])" `
    "SNOWFLAKE_USER=$($env_vars['SNOWFLAKE_USER'])" `
    "SNOWFLAKE_WAREHOUSE=$($env_vars['SNOWFLAKE_WAREHOUSE'])" `
    "SNOWFLAKE_ROLE=$($env_vars['SNOWFLAKE_ROLE'])" `
    "SNOWFLAKE_GOVTEST_DB=$($env_vars['SNOWFLAKE_GOVTEST_DB'])" `
    "SNOWFLAKE_PRIVATE_KEY_B64=$($env_vars['SNOWFLAKE_PRIVATE_KEY_B64'])" `
    "ANTHROPIC_API_KEY=$($env_vars['ANTHROPIC_API_KEY'])" `
    "GOVERNANCE_ENGINE_URL=http://127.0.0.1:9765/mcp" `
    "AI_GOVERNANCE_URL=http://127.0.0.1:9770/mcp" `
    "CDQ_FOLDER_ID=$($env_vars['CDQ_FOLDER_ID'])" `
    "IDMC_DQ_CONNECTION_ID=$($env_vars['IDMC_DQ_CONNECTION_ID'])" `
    "IDMC_DQ_RUNTIME_ENV_ID=$($env_vars['IDMC_DQ_RUNTIME_ENV_ID'])" `
    "IDMC_DQ_TEMPLATE_MAPPING_ID=$($env_vars['IDMC_DQ_TEMPLATE_MAPPING_ID'])" `
    "IDMC_DQ_SCHEMA_PATH=$($env_vars['IDMC_DQ_SCHEMA_PATH'])" `
  --secrets `
    "idmc-pass=$($env_vars['IDMC_PASS'])" `
  --output none

$fqdn = az containerapp show --name $APP_NAME --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv

Write-Host ""
Write-Host "=== Deployment complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Governance UI is live at:" -ForegroundColor White
Write-Host "  https://$fqdn" -ForegroundColor Green
Write-Host ""
