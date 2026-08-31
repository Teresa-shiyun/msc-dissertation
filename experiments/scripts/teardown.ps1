$ErrorActionPreference = "Stop"
$clusterName = "cloud-edge"

$existing = & cmd /c "kind get clusters 2>NUL"
if ($existing -contains $clusterName) {
    Write-Host "==> Deleting kind cluster '$clusterName'"
    & kind delete cluster --name $clusterName
} else {
    Write-Host "==> No cluster named '$clusterName' to delete"
}
