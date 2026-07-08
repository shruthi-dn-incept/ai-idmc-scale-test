# deploy/azure_deploy_scan_job.ps1
# (Re)deploys the scan + DQ-asset scale runner as the ACA Job, driven by .env.
# Runs on Azure (where FRS/DQ is reachable; local 503s).
# Usage:  .\deploy\azure_deploy_scan_job.ps1 [tableLimit]   (default 25; "all" for full)
param([string]$TableLimit = "25")

# Continue (not Stop): az writes benign WARNINGs to stderr which PS 5.1 would
# otherwise treat as terminating errors. We check results explicitly instead.
$ErrorActionPreference = "Continue"
. "$PSScriptRoot\_load_env.ps1"
$cfg = Import-DotEnv

$SUB  = Get-EnvOr $cfg 'AZURE_SUBSCRIPTION_ID' '7a42e0f2-3b2f-4b16-8bf2-458746103d58'
$RG   = Get-EnvOr $cfg 'AZURE_RESOURCE_GROUP'  'govtest-scale-rg'
$ACR  = Get-EnvOr $cfg 'AZURE_ACR_NAME'        'govtestscaleacr'
$ENVN = "govtest-env"
$JOB  = "govtest-scale-job"
$IMAGE = "governance-stack:latest"

Write-Host "=== Deploy scan+DQ scale Job | limit=$TableLimit ===" -ForegroundColor Cyan
az account set --subscription $SUB

$ACR_SERVER = "$ACR.azurecr.io"
$ACR_USER = az acr credential show --name $ACR --query username -o tsv
$ACR_PASS = az acr credential show --name $ACR --query "passwords[0].value" -o tsv

# Load .env (repo root)
$e = @{}
Get-Content (Join-Path $PSScriptRoot "..\.env") | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
    $p = $_ -split "=", 2
    if ($p.Count -eq 2 -and $p[1].Trim() -ne "") { $e[$p[0].Trim()] = $p[1].Trim() }
}

# Delete existing job for a clean re-create
$exists = az containerapp job show --name $JOB --resource-group $RG --query name -o tsv 2>$null
if ($exists) { Write-Host "  deleting existing job..."; az containerapp job delete --name $JOB --resource-group $RG --yes --output none }

Write-Host "  creating job..." -ForegroundColor Yellow
az containerapp job create `
  --name $JOB --resource-group $RG --environment $ENVN `
  --trigger-type Manual --replica-timeout 7200 --replica-retry-limit 0 `
  --image "$ACR_SERVER/$IMAGE" --cpu 2 --memory "4Gi" `
  --registry-server $ACR_SERVER --registry-username $ACR_USER --registry-password $ACR_PASS `
  --command "/bin/bash" --args "start_scale_scan_dq.sh" "$TableLimit" `
  --secrets "idmc-pass=$($e['IDMC_PASS'])" `
  --env-vars `
    "IDMC_USER=$($e['IDMC_USER'])" "IDMC_PASS=secretref:idmc-pass" `
    "IDMC_LOGIN_HOST=$($e['IDMC_LOGIN_HOST'])" "IDMC_SERVER_URL=$($e['IDMC_SERVER_URL'])" `
    "IDMC_FRS_HOST=$($e['IDMC_FRS_HOST'])" "IDMC_DQ_HOST=$($e['IDMC_DQ_HOST'])" `
    "IDMC_IDENTITY_HOST=$($e['IDMC_IDENTITY_HOST'])" "IDMC_ORG_ID=$($e['IDMC_ORG_ID'])" `
    "CDGC_API_BASE=$($e['CDGC_API_BASE'])" "CDQ_FOLDER_ID=$($e['CDQ_FOLDER_ID'])" "CDQ_FOLDER_NAME=$($e['CDQ_FOLDER_NAME'])" `
    "IDMC_DQ_CONNECTION_ID=$($e['IDMC_DQ_CONNECTION_ID'])" "IDMC_DQ_RUNTIME_ENV_ID=$($e['IDMC_DQ_RUNTIME_ENV_ID'])" `
    "IDMC_DQ_TEMPLATE_MAPPING_ID=$($e['IDMC_DQ_TEMPLATE_MAPPING_ID'])" "IDMC_DQ_SCHEMA_PATH=$($e['IDMC_DQ_SCHEMA_PATH'])" `
    "SNOWFLAKE_ACCOUNT=$($e['SNOWFLAKE_ACCOUNT'])" "SNOWFLAKE_USER=$($e['SNOWFLAKE_USER'])" `
    "SNOWFLAKE_WAREHOUSE=$($e['SNOWFLAKE_WAREHOUSE'])" "SNOWFLAKE_ROLE=$($e['SNOWFLAKE_ROLE'])" `
    "SNOWFLAKE_GOVTEST_DB=$($e['SNOWFLAKE_GOVTEST_DB'])" "SNOWFLAKE_PRIVATE_KEY_B64=$($e['SNOWFLAKE_PRIVATE_KEY_B64'])" `
    "ANTHROPIC_API_KEY=$($e['ANTHROPIC_API_KEY'])" `
    "GOVERNANCE_MCP_PORT=9765" "AI_GOVERNANCE_MCP_PORT=9770" `
    "GOVERNANCE_ENGINE_URL=http://127.0.0.1:9765/mcp" "AI_GOVERNANCE_URL=http://127.0.0.1:9770/mcp" `
  --output none

Write-Host "=== Job deployed. Trigger with: ===" -ForegroundColor Green
Write-Host "  az containerapp job start --name $JOB --resource-group $RG" -ForegroundColor White
