$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$dashboardDir = Join-Path $repoRoot "testbed/monitoring/dashboards"

if (-not (Test-Path $dashboardDir)) {
    throw "Dashboard directory not found: $dashboardDir"
}

$jsonFiles = Get-ChildItem -Path $dashboardDir -Filter *.json -File
if ($jsonFiles.Count -eq 0) {
    Write-Host "==> No dashboard JSONs found in $dashboardDir, nothing to do."
    return
}

foreach ($f in $jsonFiles) {
    $cmName = "dashboard-$($f.BaseName)"
    Write-Host "==> Applying ConfigMap '$cmName' from $($f.Name)"

    $manifest = & kubectl create configmap $cmName `
        --namespace monitoring `
        --from-file ("{0}={1}" -f $f.Name, $f.FullName) `
        --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "kubectl create configmap dry-run failed for $($f.Name)" }

    $manifest | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed for $cmName" }

    & kubectl label configmap $cmName --namespace monitoring grafana_dashboard=1 --overwrite | Out-Null
}

Write-Host ""
Write-Host "==> ConfigMaps with grafana_dashboard label:"
& kubectl -n monitoring get configmap -l grafana_dashboard=1

Write-Host ""
Write-Host "Grafana sidecar will pick these up within ~30 seconds."
Write-Host "Open Grafana:  kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80"
Write-Host "Dashboard:     http://localhost:3000/dashboards  ->  'Workload — cloud/edge orchestration'"
