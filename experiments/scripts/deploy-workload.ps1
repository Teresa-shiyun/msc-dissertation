$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$workloadDir = Join-Path $repoRoot "workload"
$manifestsDir = Join-Path $repoRoot "testbed/manifests"
$image = "workload:0.1.0"
$clusterName = "cloud-edge"

Write-Host "==> Building image $image"
& docker build -t $image $workloadDir
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

Write-Host "==> Loading image into kind cluster '$clusterName'"
& kind load docker-image $image --name $clusterName
if ($LASTEXITCODE -ne 0) { throw "kind load docker-image failed" }

Write-Host "==> Applying manifests"
& kubectl apply -f $manifestsDir
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed" }

Write-Host "==> Waiting for rollout"
& kubectl -n workload rollout status deployment/workload-cloud --timeout=120s
& kubectl -n workload rollout status deployment/workload-edge --timeout=120s

Write-Host "==> Pods"
& kubectl -n workload get pods -o wide

Write-Host "==> Done."
Write-Host "    Test from inside the cluster:"
Write-Host "      kubectl -n workload run curl --rm -it --image=curlimages/curl -- /bin/sh"
Write-Host "      curl http://workload.workload.svc/work?n=1000"
