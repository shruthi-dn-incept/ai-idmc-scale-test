# install.ps1 — One-time setup on the Cloud PC
# Run as Administrator to also open the firewall (optional — only needed if others access the UI from another machine)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path $PSScriptRoot -Parent

Write-Host "=== IDMC Governance Engine - Install ===" -ForegroundColor Cyan

# 1. Check Python
Write-Host "`n[1/3] Checking Python..." -ForegroundColor Yellow
try {
    $pyver = & python --version 2>&1
    Write-Host "  Found: $pyver" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Python not found. Install Python 3.11+ from https://python.org" -ForegroundColor Red
    exit 1
}

# 2. Install Python dependencies
Write-Host "`n[2/3] Installing Python dependencies..." -ForegroundColor Yellow
Set-Location $ROOT
& python -m pip install -r requirements.txt --quiet
Write-Host "  Done." -ForegroundColor Green

# 3. Open firewall port 8080 (requires Administrator — skipped if not running as admin)
Write-Host "`n[3/3] Opening Windows Firewall port 8080..." -ForegroundColor Yellow
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  Skipped (not running as Administrator)." -ForegroundColor Yellow
    Write-Host "  localhost:8080 still works. Re-run as Administrator only if others need to access the UI from another machine." -ForegroundColor Gray
} else {
    $ruleName = "IDMC Governance UI (8080)"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Rule already exists." -ForegroundColor Green
    } else {
        New-NetFirewallRule -DisplayName $ruleName `
            -Direction Inbound -Protocol TCP -LocalPort 8080 `
            -Action Allow -Profile Any | Out-Null
        Write-Host "  Firewall rule created." -ForegroundColor Green
    }
}

Write-Host "`n=== Install complete ===" -ForegroundColor Cyan
Write-Host "  Next: follow DEMO_V2.md — Step 0 starts the servers" -ForegroundColor White
