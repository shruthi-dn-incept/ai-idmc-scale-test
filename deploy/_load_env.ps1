# deploy/_load_env.ps1
# Shared helper for the deploy scripts. Dot-source it, then use Import-DotEnv /
# Get-EnvOr to read Azure config from the repo-root .env. This lets a NEW Azure
# account be configured by editing ONE file (.env) instead of every script:
#   AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_LOCATION,
#   AZURE_ACR_NAME (globally unique), AZURE_VM_SIZE
# Any value not set in .env falls back to the script's hardcoded default.

function Import-DotEnv {
    param([string]$Path = (Join-Path $PSScriptRoot "..\.env"))
    $h = @{}
    if (Test-Path $Path) {
        Get-Content $Path | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
            $parts = $_ -split '=', 2
            if ($parts.Count -eq 2) { $h[$parts[0].Trim()] = $parts[1].Trim() }
        }
    }
    return $h
}

function Get-EnvOr {
    param([hashtable]$Env, [string]$Key, [string]$Default)
    if ($Env.ContainsKey($Key) -and $Env[$Key] -ne "") { return $Env[$Key] }
    return $Default
}
