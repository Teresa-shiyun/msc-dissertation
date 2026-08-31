$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$kindConfig = Join-Path $repoRoot "testbed/kind-config.yaml"
$clusterName = "cloud-edge"

Write-Host "==> Checking required tooling"
foreach ($tool in @("docker", "kind", "kubectl")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Missing required tool: $tool. Please install it and retry."
    }
}

$existing = & cmd /c "kind get clusters 2>NUL"
if ($existing -contains $clusterName) {
    Write-Host "==> kind cluster '$clusterName' already exists, skipping create"
} else {
    Write-Host "==> Creating kind cluster '$clusterName'"
    & kind create cluster --name $clusterName --config $kindConfig
    if ($LASTEXITCODE -ne 0) { throw "kind create cluster failed" }
}

Write-Host "==> Setting kubectl context"
& kubectl config use-context "kind-$clusterName" | Out-Null

Write-Host "==> Cluster nodes"
& kubectl get nodes -o wide --show-labels=false

Write-Host "==> Done. Next: ./experiments/scripts/bootstrap-monitoring.ps1"
