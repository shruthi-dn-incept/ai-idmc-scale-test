# Load .env into current process environment
Get-Content "$PSScriptRoot\.env" | Where-Object { $_ -match "^[A-Z]" -and $_ -match "=" } | ForEach-Object {
    $parts = $_ -split "=", 2
    $key   = $parts[0].Trim()
    $value = $parts[1].Trim()
    Set-Item "env:$key" $value
}

# Required env vars for governance_engine_mcp (reads at import time)
$env:IDMC_FRS_HOST      = "dmp-us.informaticacloud.com"
$env:IDMC_DQ_HOST       = "usw1-dqcloud.dmp-us.informaticacloud.com"
$env:IDMC_IDENTITY_HOST = "dmp-us.informaticacloud.com"
$env:CDGC_API_BASE      = "https://cdgc-api.dmp-us.informaticacloud.com"

Write-Host "Starting governance-engine  (:9765)..."
$p1 = Start-Process python -ArgumentList "governance_engine_mcp.py" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4

Write-Host "Starting ai-governance      (:9770)..."
$p2 = Start-Process python -ArgumentList "ai_governance_mcp.py" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4

Write-Host "Starting governance-ui      (:9080)..."
$env:AI_GOVERNANCE_URL     = "http://127.0.0.1:9770/mcp"
$env:GOVERNANCE_ENGINE_URL = "http://127.0.0.1:9765/mcp"
$p3 = Start-Process python -ArgumentList "governance_ui.py" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 5

$ports = netstat -ano | Select-String ":9765 |:9770 |:9080 " | Select-String "LISTENING"
if ($ports) {
    Write-Host "`nAll servers running:"
    $ports | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "`nWARNING: one or more servers failed to start"
}

Write-Host "`nPIDs: governance-engine=$($p1.Id)  ai-governance=$($p2.Id)  governance-ui=$($p3.Id)"
