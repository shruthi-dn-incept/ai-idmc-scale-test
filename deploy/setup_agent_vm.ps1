# deploy/setup_agent_vm.ps1
# Provisions the Informatica Secure Agent VM on Azure and applies every fix
# this project depends on. Reproduces `govtest-agent-vm` from scratch.
#
# Usage (from repo root, with az CLI logged in):
#   .\deploy\setup_agent_vm.ps1 -AdminPassword '<vm-password>' -AgentInstallToken '<idmc-token>'
#
# The install token is generated in IDMC:
#   Administrator -> Runtime Environments -> Download Secure Agent -> Linux 64
#   (copy the install token shown; it is a SECRET - never commit it)
#
# NOTE: This script installs the OS/agent prerequisites we discovered the hard way:
#   1. libidn11  - Ubuntu 22.04 dropped it; without it pmdtm fails every mapping
#                  with "libidn.so.11: cannot open shared object file" (exit 127),
#                  surfaced by IDMC only as "Job failed before Ingestion".
#   2. infaagent.service - the agent has NO auto-start; a reboot silently downs it.
#   3. 200 GB OS disk - the default 30 GB fills with session logs during scale runs.

param(
    [Parameter(Mandatory = $true)][string]$AdminPassword,
    [string]$AgentInstallToken = "",   # optional: if set, registers the agent non-interactively
    [string]$AdminUser         = "azureuser"
)

$ErrorActionPreference = "Stop"

# -- Config ---------------------------------------------------------------------
$SUBSCRIPTION = "7a42e0f2-3b2f-4b16-8bf2-458746103d58"
$RG           = "govtest-scale-rg"
$LOCATION     = "eastus"
$VM_NAME      = "govtest-agent-vm"
$VM_SIZE      = "Standard_D8s_v3"      # 8 vCPU / 32 GB - RAM is not the bottleneck
$OS_IMAGE     = "Ubuntu2204"
$OS_DISK_GB   = 200

Write-Host "`n=== Secure Agent VM Setup ===" -ForegroundColor Cyan
Write-Host "  Subscription : $SUBSCRIPTION"
Write-Host "  Resource grp : $RG / $LOCATION"
Write-Host "  VM           : $VM_NAME ($VM_SIZE, $OS_IMAGE, ${OS_DISK_GB}GB disk)"
Write-Host ""

# -- 1. Subscription + resource group ------------------------------------------
Write-Host "[1/5] Setting subscription and resource group..." -ForegroundColor Yellow
az account set --subscription $SUBSCRIPTION
az group create --name $RG --location $LOCATION --output none

# -- 2. Create the VM (idempotent) ---------------------------------------------
Write-Host "[2/5] Creating VM '$VM_NAME'..." -ForegroundColor Yellow
$exists = az vm show --resource-group $RG --name $VM_NAME --query name -o tsv 2>$null
if ($exists) {
    Write-Host "  VM already exists - skipping create." -ForegroundColor Gray
} else {
    az vm create `
      --resource-group $RG `
      --name $VM_NAME `
      --image $OS_IMAGE `
      --size $VM_SIZE `
      --os-disk-size-gb $OS_DISK_GB `
      --admin-username $AdminUser `
      --admin-password $AdminPassword `
      --public-ip-sku Standard `
      --output none
    Write-Host "  VM created." -ForegroundColor Green
}

# Ensure the OS disk is at least the target size (covers pre-existing smaller VMs).
$osDisk = az vm show -g $RG -n $VM_NAME --query "storageProfile.osDisk.name" -o tsv
$curGb  = az disk show -g $RG -n $osDisk --query diskSizeGb -o tsv
if ([int]$curGb -lt $OS_DISK_GB) {
    Write-Host "  Resizing OS disk $curGb GB -> $OS_DISK_GB GB (requires deallocate)..." -ForegroundColor Yellow
    az vm deallocate -g $RG -n $VM_NAME
    az disk update -g $RG -n $osDisk --size-gb $OS_DISK_GB --output none
    az vm start -g $RG -n $VM_NAME
    Write-Host "  OS disk resized (root grows automatically on boot via cloud-init)." -ForegroundColor Green
}

# -- 3. OS prerequisites: libidn11 ---------------------------------------------
Write-Host "[3/5] Installing OS prerequisites (libidn11)..." -ForegroundColor Yellow
$libidnScript = Join-Path $env:TEMP "agentvm_libidn.sh"
@'
#!/bin/bash
set -e
if ldconfig -p | grep -q "libidn.so.11"; then
  echo "libidn.so.11 already present."
else
  cd /tmp
  wget -q http://archive.ubuntu.com/ubuntu/pool/main/libi/libidn/libidn11_1.33-2.2ubuntu2_amd64.deb -O libidn11.deb
  sudo dpkg -i libidn11.deb
  sudo ldconfig
fi
ldconfig -p | grep "libidn.so.11" && echo "OK: libidn.so.11 installed"
'@ | Set-Content -Path $libidnScript -Encoding utf8
az vm run-command invoke -g $RG -n $VM_NAME --command-id RunShellScript --scripts "@$libidnScript" --query "value[0].message" -o tsv

# -- 4. Install + register the Secure Agent (optional, needs token) -------------
Write-Host "[4/5] Secure Agent install/registration..." -ForegroundColor Yellow
if ($AgentInstallToken -ne "") {
    $installScript = Join-Path $env:TEMP "agentvm_install.sh"
    # NOTE: the installer URL is pod-specific. Grab it from the IDMC "Download
    # Secure Agent" page and set INSTALLER_URL below, or pre-stage the .bin.
    @"
#!/bin/bash
set -e
cd /home/$AdminUser
INSTALLER_URL="REPLACE_WITH_IDMC_LINUX_INSTALLER_URL"
if [ ! -d /home/$AdminUser/infaagent ]; then
  wget -q "`$INSTALLER_URL" -O agent64_install.bin
  chmod +x agent64_install.bin
  ./agent64_install.bin -i silent
fi
cd /home/$AdminUser/infaagent/apps/agentcore
# Register with IDMC using the one-time install token
./consoleAgentManager.sh configureToken '$AgentInstallToken'
echo "Agent registration attempted."
"@ | Set-Content -Path $installScript -Encoding utf8
    az vm run-command invoke -g $RG -n $VM_NAME --command-id RunShellScript --scripts "@$installScript" --query "value[0].message" -o tsv
} else {
    Write-Host "  No -AgentInstallToken given. Install/register the agent manually," -ForegroundColor Gray
    Write-Host "  then re-run this script's step 5 to enable auto-start." -ForegroundColor Gray
}

# -- 5. Auto-start service (survives reboots) ----------------------------------
Write-Host "[5/5] Configuring agent auto-start (systemd)..." -ForegroundColor Yellow
$autostartScript = Join-Path $env:TEMP "agentvm_autostart.sh"
@"
#!/bin/bash
cat <<'UNIT' | sudo tee /etc/systemd/system/infaagent.service >/dev/null
[Unit]
Description=Informatica Secure Agent
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
User=$AdminUser
WorkingDirectory=/home/$AdminUser/infaagent/apps/agentcore
ExecStart=/home/$AdminUser/infaagent/apps/agentcore/infaagent.sh startup
ExecStop=/home/$AdminUser/infaagent/apps/agentcore/infaagent.sh shutdown
TimeoutStartSec=600
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable infaagent.service
systemctl is-enabled infaagent.service
"@ | Set-Content -Path $autostartScript -Encoding utf8
az vm run-command invoke -g $RG -n $VM_NAME --command-id RunShellScript --scripts "@$autostartScript" --query "value[0].message" -o tsv

Write-Host "`n=== Secure Agent VM setup complete ===" -ForegroundColor Cyan
Write-Host "  VM        : $VM_NAME ($VM_SIZE, ${OS_DISK_GB}GB)" -ForegroundColor White
Write-Host "  Prereqs   : libidn11 installed, auto-start enabled" -ForegroundColor White
Write-Host ""
Write-Host "Verify the agent is online in IDMC:" -ForegroundColor White
Write-Host "  Administrator -> Runtime Environments -> $VM_NAME (should be Up)" -ForegroundColor Green
Write-Host ""
Write-Host "Read agent logs remotely without SSH:" -ForegroundColor White
Write-Host "  az vm run-command invoke -g $RG -n $VM_NAME --command-id RunShellScript --scripts 'tail -50 /home/$AdminUser/infaagent/apps/Data_Integration_Server/logs/tomcat/tomcat_*.log'" -ForegroundColor Green
