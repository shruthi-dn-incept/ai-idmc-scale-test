# deploy/azure_deploy_pipeline_job.ps1
# Builds the image and (re)deploys the full end-to-end scale pipeline as ONE ACA Job.
# Runs entirely on Azure: extract -> taxonomy -> domain -> system/dataset ->
# DQRO import -> curate -> DQ scan, writing stats.json.
# Usage:  .\deploy\azure_deploy_pipeline_job.ps1 [pipelineArgs]
#   pipelineArgs: passed to the orchestrator (default "--clean")
param([string]$PipelineArgs = "--clean")

$ErrorActionPreference = "Continue"
. "$PSScriptRoot\_load_env.ps1"
$cfg = Import-DotEnv

$SUB  = Get-EnvOr $cfg 'AZURE_SUBSCRIPTION_ID' '7a42e0f2-3b2f-4b16-8bf2-458746103d58'
$RG   = Get-EnvOr $cfg 'AZURE_RESOURCE_GROUP'  'govtest-scale-rg'
$ACR  = Get-EnvOr $cfg 'AZURE_ACR_NAME'        'govtestscaleacr'
$ENVN = "govtest-env"
$JOB  = "govtest-pipeline-job"
$IMAGE = "governance-stack:latest"

Write-Host "=== Deploy full-pipeline Job | args=$PipelineArgs ===" -ForegroundColor Cyan
az account set --subscription $SUB
$ACR_SERVER = "$ACR.azurecr.io"

Write-Host "  building image..." -ForegroundColor Yellow
az acr build --registry $ACR --image $IMAGE --file docker/Dockerfile . --output none
$ACR_USER = az acr credential show --name $ACR --query username -o tsv
$ACR_PASS = az acr credential show --name $ACR --query "passwords[0].value" -o tsv

$e = @{}
Get-Content (Join-Path $PSScriptRoot "..\.env") | Where-Object { $_ -match "^\s*[^#].*=.*" } | ForEach-Object {
    $p = $_ -split "=", 2
    if ($p.Count -eq 2 -and $p[1].Trim() -ne "") { $e[$p[0].Trim()] = $p[1].Trim() }
}

$exists = az containerapp job show --name $JOB --resource-group $RG --query name -o tsv 2>$null
if ($exists) { Write-Host "  deleting existing job..."; az containerapp job delete --name $JOB --resource-group $RG --yes --output none }

Write-Host "  creating job (replica-timeout 2h, 2 vCPU / 4Gi)..." -ForegroundColor Yellow
az containerapp job create `
  --name $JOB --resource-group $RG --environment $ENVN `
  --trigger-type Manual --replica-timeout 10800 --replica-retry-limit 0 `
  --image "$ACR_SERVER/$IMAGE" --cpu 2 --memory "4Gi" `
  --registry-server $ACR_SERVER --registry-username $ACR_USER --registry-password $ACR_PASS `
  --command "/bin/bash" --args "scripts/start_scale_pipeline.sh" `
  --secrets "idmc-pass=$($e['IDMC_PASS'])" "sf-key=$($e['SNOWFLAKE_PRIVATE_KEY_B64'])" "anthropic=$($e['ANTHROPIC_API_KEY'])" `
  --env-vars `
    "PIPELINE_ARGS=$PipelineArgs" `
    "IDMC_USER=$($e['IDMC_USER'])" "IDMC_PASS=secretref:idmc-pass" `
    "IDMC_LOGIN_HOST=$($e['IDMC_LOGIN_HOST'])" "IDMC_SERVER_URL=$($e['IDMC_SERVER_URL'])" `
    "IDMC_FRS_HOST=$($e['IDMC_FRS_HOST'])" "IDMC_DQ_HOST=$($e['IDMC_DQ_HOST'])" `
    "IDMC_IDENTITY_HOST=$($e['IDMC_IDENTITY_HOST'])" "IDMC_ORG_ID=$($e['IDMC_ORG_ID'])" `
    "CDGC_API_BASE=$($e['CDGC_API_BASE'])" "CDQ_FOLDER_ID=$($e['CDQ_FOLDER_ID'])" "CDQ_FOLDER_NAME=$($e['CDQ_FOLDER_NAME'])" `
    "IDMC_DQ_CONNECTION_ID=$($e['IDMC_DQ_CONNECTION_ID'])" "IDMC_DQ_RUNTIME_ENV_ID=$($e['IDMC_DQ_RUNTIME_ENV_ID'])" `
    "SNOWFLAKE_ACCOUNT=$($e['SNOWFLAKE_ACCOUNT'])" "SNOWFLAKE_USER=$($e['SNOWFLAKE_USER'])" `
    "SNOWFLAKE_WAREHOUSE=$($e['SNOWFLAKE_WAREHOUSE'])" "SNOWFLAKE_ROLE=$($e['SNOWFLAKE_ROLE'])" `
    "SNOWFLAKE_GOVTEST_DB=$($e['SNOWFLAKE_GOVTEST_DB'])" "SNOWFLAKE_PRIVATE_KEY_B64=secretref:sf-key" `
    "ANTHROPIC_API_KEY=secretref:anthropic" `
  --output none

Write-Host "=== Pipeline job deployed. Trigger with: ===" -ForegroundColor Green
Write-Host "  az containerapp job start --name $JOB --resource-group $RG" -ForegroundColor White
