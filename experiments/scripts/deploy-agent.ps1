$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$agentDir = Join-Path $repoRoot "agent"
$manifestsDir = Join-Path $agentDir "manifests"
$image = "orchestration-agent:0.1.0"
$clusterName = "cloud-edge"

Write-Host "==> Building image $image"
& docker build -t $image $agentDir
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

Write-Host "==> Loading image into kind cluster '$clusterName'"
& kind load docker-image $image --name $clusterName
if ($LASTEXITCODE -ne 0) { throw "kind load docker-image failed" }

Write-Host "==> Applying agent manifests"
& kubectl apply -f $manifestsDir
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed" }

Write-Host "==> Waiting for rollout"
& kubectl -n tools rollout status deployment/orchestration-agent --timeout=120s

Write-Host "==> Pod"
& kubectl -n tools get pods -l app=orchestration-agent -o wide

Write-Host ""
Write-Host "Tail agent logs:  kubectl -n tools logs -f deploy/orchestration-agent"
Write-Host "Activate arm C:   ./experiments/scripts/apply-arm.ps1 -Arm agent"
