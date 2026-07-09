# deploy/azure_deploy_import_job.ps1
# Builds the image (baking in the generated DQRO .xlsx) and (re)deploys the CDGC
# bulk-import ACA Job. Runs on Azure (CDGC writes 503 locally).
# Usage:  .\deploy\azure_deploy_import_job.ps1 [dqroFile] [policy]
#   dqroFile: path inside the repo/image (default templates/CDGC_DQRO_FULL.xlsx)
#   policy:   CONTINUE_ON_ERROR_WARNING (default) | STOP_ON_ERROR
param(
  [string]$DqroFile = "templates/CDGC_DQRO_FULL.xlsx",
  [string]$Policy   = "CONTINUE_ON_ERROR_WARNING"
)

$ErrorActionPreference = "Continue"
. "$PSScriptRoot\_load_env.ps1"
$cfg = Import-DotEnv

$SUB  = Get-EnvOr $cfg 'AZURE_SUBSCRIPTION_ID' '7a42e0f2-3b2f-4b16-8bf2-458746103d58'
$RG   = Get-EnvOr $cfg 'AZURE_RESOURCE_GROUP'  'govtest-scale-rg'
$ACR  = Get-EnvOr $cfg 'AZURE_ACR_NAME'        'govtestscaleacr'
$ENVN = "govtest-env"
$JOB  = "govtest-import-job"
$IMAGE = "governance-stack:latest"

Write-Host "=== Deploy CDGC bulk-import Job | file=$DqroFile policy=$Policy ===" -ForegroundColor Cyan
az account set --subscription $SUB

$ACR_SERVER = "$ACR.azurecr.io"

# Build image so the freshly-generated DQRO file + import scripts are baked in.
Write-Host "  building image (bakes in $DqroFile)..." -ForegroundColor Yellow
az acr build --registry $ACR --image $IMAGE --file docker/Dockerfile . --output none

$ACR_USER = az acr credential show --name $ACR --query username -o tsv
$ACR_PASS = az acr credential show --name $ACR --query "passwords[0].value" -o tsv

# Load .env (repo root) for runtime env vars
$e = @{}
Get-Content (Join-Path $PSScriptRoot "..\.env") | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
    $p = $_ -split "=", 2
    if ($p.Count -eq 2 -and $p[1].Trim() -ne "") { $e[$p[0].Trim()] = $p[1].Trim() }
}

$exists = az containerapp job show --name $JOB --resource-group $RG --query name -o tsv 2>$null
if ($exists) { Write-Host "  deleting existing job..."; az containerapp job delete --name $JOB --resource-group $RG --yes --output none }

Write-Host "  creating job..." -ForegroundColor Yellow
az containerapp job create `
  --name $JOB --resource-group $RG --environment $ENVN `
  --trigger-type Manual --replica-timeout 7200 --replica-retry-limit 0 `
  --image "$ACR_SERVER/$IMAGE" --cpu 2 --memory "4Gi" `
  --registry-server $ACR_SERVER --registry-username $ACR_USER --registry-password $ACR_PASS `
  --command "/bin/bash" --args "scripts/start_bulk_import.sh" "$DqroFile" "$Policy" `
  --secrets "idmc-pass=$($e['IDMC_PASS'])" `
  --env-vars `
    "IDMC_USER=$($e['IDMC_USER'])" "IDMC_PASS=secretref:idmc-pass" `
    "IDMC_LOGIN_HOST=$($e['IDMC_LOGIN_HOST'])" "IDMC_SERVER_URL=$($e['IDMC_SERVER_URL'])" `
    "IDMC_FRS_HOST=$($e['IDMC_FRS_HOST'])" "IDMC_DQ_HOST=$($e['IDMC_DQ_HOST'])" `
    "IDMC_IDENTITY_HOST=$($e['IDMC_IDENTITY_HOST'])" "IDMC_ORG_ID=$($e['IDMC_ORG_ID'])" `
    "CDGC_API_BASE=$($e['CDGC_API_BASE'])" `
  --output none

Write-Host "=== Import job deployed. Trigger with: ===" -ForegroundColor Green
Write-Host "  az containerapp job start --name $JOB --resource-group $RG" -ForegroundColor White
