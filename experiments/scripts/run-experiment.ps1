$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venvPython = Join-Path $repoRoot "agent\.venv\Scripts\python.exe"
$runner     = Join-Path $repoRoot "experiments\run_experiment.py"

if (-not (Test-Path $venvPython)) {
    Write-Error "Agent venv not found at $venvPython. Create it first by running pytest in agent/ once."
    exit 1
}
if (-not (Test-Path $runner)) {
    Write-Error "Experiment runner not found at $runner."
    exit 1
}

$promUrl = if ($env:PROMETHEUS_URL) { $env:PROMETHEUS_URL } else { "http://localhost:9090" }
try {
    $r = Invoke-WebRequest -Uri "$promUrl/-/ready" -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -ne 200) { throw "Prometheus replied $($r.StatusCode)" }
} catch {
    Write-Warning "Prometheus not reachable at $promUrl. Start the port-forward in another window:"
    Write-Warning "  kubectl -n monitoring port-forward svc/kube-prom-stack-prometheus 9090:9090"
    Write-Warning "Continuing anyway — pass --dry-run if you only want to plan."
}

& $venvPython $runner @args
exit $LASTEXITCODE
