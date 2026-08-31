$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$valuesPromStack = Join-Path $repoRoot "testbed/monitoring/values.yaml"
$valuesAdapter = Join-Path $repoRoot "testbed/monitoring/prometheus-adapter-values.yaml"
$valuesMetrics = Join-Path $repoRoot "testbed/monitoring/metrics-server-values.yaml"

foreach ($tool in @("kubectl", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Missing required tool: $tool. Install it and retry."
    }
}

Write-Host "==> Adding Helm repos"
& helm repo add prometheus-community https://prometheus-community.github.io/helm-charts | Out-Null
& helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ | Out-Null
& helm repo update | Out-Null

Write-Host "==> Creating namespace 'monitoring' (if absent)"
& cmd /c "kubectl get namespace monitoring 2>NUL 1>NUL"
if ($LASTEXITCODE -ne 0) {
    & kubectl create namespace monitoring | Out-Null
}

Write-Host "==> Installing kube-prometheus-stack"
& helm upgrade --install kube-prom-stack `
    prometheus-community/kube-prometheus-stack `
    --namespace monitoring `
    --values $valuesPromStack `
    --wait --timeout 10m
if ($LASTEXITCODE -ne 0) { throw "kube-prometheus-stack install failed" }

Write-Host "==> Installing Prometheus Adapter"
& helm upgrade --install prometheus-adapter `
    prometheus-community/prometheus-adapter `
    --namespace monitoring `
    --values $valuesAdapter `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { throw "prometheus-adapter install failed" }

Write-Host "==> Installing metrics-server (for CPU HPA)"
& helm upgrade --install metrics-server `
    metrics-server/metrics-server `
    --namespace monitoring `
    --values $valuesMetrics `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { throw "metrics-server install failed" }

Write-Host "==> Pods in 'monitoring'"
& kubectl -n monitoring get pods

Write-Host "==> Done."
Write-Host "    Prometheus UI:  kubectl -n monitoring port-forward svc/kube-prom-stack-prometheus 9090:9090"
Write-Host "    Grafana:        kubectl -n monitoring port-forward svc/kube-prom-stack-grafana 3000:80"
Write-Host "    Custom metrics: kubectl get --raw '/apis/custom.metrics.k8s.io/v1beta1' | ConvertFrom-Json"
