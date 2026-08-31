$ErrorActionPreference = "Stop"
$here    = $PSScriptRoot
$ns      = "workload"
$svc     = "workload-edge"
$lport   = 18081
$workN   = 3000
$dur     = "45s"
$gap     = 15
$levels  = @(25, 50, 75, 100, 125, 150)

$k6 = (Get-Command k6 -ErrorAction SilentlyContinue).Source
if (-not $k6) { $k6 = "k6" }
$edgePod = (kubectl get pods -n $ns -l "app=workload,tier=edge" -o jsonpath="{.items[0].metadata.name}")
Write-Host "Edge pod: $edgePod"

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
if (-not $ready) { Write-Host "ERROR: tunnel not ready"; if ($pf) { try { $pf.Kill() } catch {} }; exit 1 }
Write-Host "Tunnel ready."

function Sample-Cpu($level) {
    $line = (kubectl top pod -n $ns $edgePod --no-headers 2>$null)
    if ($line) {
        $cols = ($line -split "\s+") | Where-Object { $_ -ne "" }
        $cpuRaw = $cols[1]
        if ($cpuRaw -match "m$") { $milli = [int]($cpuRaw -replace "m","") }
        else { $milli = [int]([double]$cpuRaw * 1000) }
        $ts = (Get-Date).ToString("o")
        "$level,$ts,$milli" | Out-File -FilePath $cpuCsv -Append -Encoding utf8
    }
}

foreach ($rps in $levels) {
    Write-Host "=== Level $rps rps ==="
    $summary = Join-Path $here ("level_{0}.json" -f $rps)
    $env:TARGET_URL   = "http://127.0.0.1:$lport"
    $env:WORK_N       = "$workN"
    $env:RATE         = "$rps"
    $env:DURATION     = $dur
    $env:PREALLOC_VUS = "200"
    $script = Join-Path $here "k6-capacity.js"
    $k6proc = Start-Process -FilePath $k6 `
        -ArgumentList @("run","--quiet","--summary-export",$summary,$script) `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 8
    while (-not $k6proc.HasExited) { Sample-Cpu $rps; Start-Sleep -Seconds 7 }
    $k6proc.WaitForExit()
    Write-Host "  level $rps done"
    Start-Sleep -Seconds $gap
}

if ($pf) { try { $pf.Kill() } catch {}; Write-Host "port-forward stopped." }
Write-Host "Edge calibration sweep complete."
