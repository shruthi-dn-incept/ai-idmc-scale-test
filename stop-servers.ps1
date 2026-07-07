# Kill all three governance servers by port
foreach ($port in @(9765, 9770, 9080)) {
    $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "Killed :$port (PID $($conn.OwningProcess))"
    } else {
        Write-Host ":$port not running"
    }
}
