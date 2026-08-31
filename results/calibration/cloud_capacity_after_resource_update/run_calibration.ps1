$ErrorActionPreference = "Stop"
$here    = $PSScriptRoot
$ns      = "workload"
$svc     = "workload-cloud"
$lport   = 18080
$workN   = 3000
$dur     = "45s"
$durSec  = 45
$gap     = 15            # seconds between levels for CPU to settle
$levels  = @(25, 50, 75, 100, 125, 150, 175, 200)

$k6 = (Get-Command k6 -ErrorAction SilentlyContinue).Source
if (-not $k6) { $k6 = "k6" }
$cloudPod = (kubectl get pods -n $ns -l "app=workload,tier=cloud" -o jsonpath="{.items[0].metadata.name}")
Write-Host "Cloud pod: $cloudPod"

$cpuCsv = Join-Path $here "cpu_samples.csv"
"level_rps,iso_time,milli_cpu" | Out-File -FilePath $cpuCsv -Encoding utf8

Write-Host "Starting port-forward svc/$svc $lport:80 ..."
$pf = Start-Process -FilePath "kubectl" `
    -ArgumentList @("port-forward","-n",$ns,"svc/$svc","$lport`:80") `
    -PassThru -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$lport/healthz" -TimeoutSec 2 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    Write-Host "ERROR: port-forward/health not ready"; if ($pf) { $pf.Kill() }; exit 1
}
Write-Host "Tunnel ready."

function Sample-Cpu($level) {
    $line = (kubectl top pod -n $ns $cloudPod --no-headers 2>$null)
    if ($line) {
        $cols = ($line -split "\s+") | Where-Object { $_ -ne "" }
        $cpuRaw = $cols[1]
        if ($cpuRaw -match "m$") { $milli = [int]($cpuRaw -replace "m","") }
        else { $milli = [int]([double]$cpuRaw * 1000) }
        $ts = (Get-Date).ToString("o")
        "$level,$ts,$milli" | Out-File -FilePath $cpuCsv -Append -Encoding utf8
        return $milli
    }
    return $null
}

foreach ($rps in $levels) {
    Write-Host "=== Level $rps rps ==="
    $summary = Join-Path $here ("level_{0}.json" -f $rps)
    $k6env = @{
        TARGET_URL   = "http://127.0.0.1:$lport"
        WORK_N       = "$workN"
        RATE         = "$rps"
        DURATION     = $dur
        PREALLOC_VUS = "200"
    }
    foreach ($k in $k6env.Keys) { Set-Item -Path "Env:$k" -Value $k6env[$k] }

    $script = Join-Path $here "k6-capacity.js"
    $k6proc = Start-Process -FilePath $k6 `
        -ArgumentList @("run","--quiet","--summary-export",$summary,$script) `
        -PassThru -WindowStyle Hidden

    Start-Sleep -Seconds 8
    while (-not $k6proc.HasExited) {
        [void](Sample-Cpu $rps)
        Start-Sleep -Seconds 7
    }
    $k6proc.WaitForExit()
    Write-Host "  level $rps done -> $summary"
    Start-Sleep -Seconds $gap
}

if ($pf) { $pf.Kill(); Write-Host "port-forward stopped." }
Write-Host "Calibration sweep complete."
