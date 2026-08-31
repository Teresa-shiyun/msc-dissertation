param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("baseline", "hpa-cpu", "hpa-rps", "agent", "agent-predictive", "agent-predictive-guarded",
                 "d60", "d30", "g60", "g30", "f30",
                 "h50", "h100", "h115", "p30")]
    [string]$Arm,

    [int]$BaselineReplicas = 1
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$hpaDir = Join-Path $repoRoot "testbed/manifests/hpa"

function Remove-AllHpas {
    Write-Host "==> Removing any existing HPAs in 'workload' namespace"
    & cmd /c "kubectl -n workload delete hpa --all --ignore-not-found 2>NUL"
}

function Stop-AgentIfDeployed {
    $agentDeploy = & cmd /c "kubectl -n tools get deployment orchestration-agent -o name 2>NUL"
    if ($agentDeploy) {
        Write-Host "==> Scaling orchestration-agent to 0 (releasing scaling control)"
        & kubectl -n tools scale deployment/orchestration-agent --replicas=0 | Out-Null
    }
}

function Activate-AgentPolicy {
    param([string]$PolicyPath, [string]$Label)
    Remove-AllHpas
    $agentDeploy = & cmd /c "kubectl -n tools get deployment orchestration-agent -o name 2>NUL"
    if (-not $agentDeploy) {
        Write-Host "==> orchestration-agent not deployed yet."
        Write-Host "    Run ./experiments/scripts/deploy-agent.ps1 first, then re-run this script."
        return
    }
    Write-Host "==> Selecting policy $PolicyPath"
    & kubectl -n tools set env deployment/orchestration-agent AGENT_POLICY=$PolicyPath | Out-Null
    Write-Host "==> Scaling orchestration-agent to 1 (taking over scaling decisions)"
    & kubectl -n tools scale deployment/orchestration-agent --replicas=1
    & kubectl -n tools rollout status deployment/orchestration-agent --timeout=60s
    Write-Host "==> $Label active. Tail logs with:"
    Write-Host "    kubectl -n tools logs -f deploy/orchestration-agent"
}

function Activate-Hpa {
    param([string]$ManifestFile, [string]$Label)
    Remove-AllHpas
    Stop-AgentIfDeployed
    Write-Host "==> Applying $hpaDir/$ManifestFile"
    & kubectl apply -f (Join-Path $hpaDir $ManifestFile)
    Write-Host "==> $Label active"
}

switch ($Arm) {
    "baseline" {
        Remove-AllHpas
        Stop-AgentIfDeployed
        Write-Host "==> Setting workload-cloud replicas to $BaselineReplicas"
        & kubectl -n workload scale deployment/workload-cloud --replicas=$BaselineReplicas
        Write-Host "==> Arm A (baseline) active"
    }
    "hpa-cpu" {
        Remove-AllHpas
        Stop-AgentIfDeployed
        Write-Host "==> Applying $hpaDir/hpa-cpu.yaml"
        & kubectl apply -f (Join-Path $hpaDir "hpa-cpu.yaml")
        Write-Host "==> Arm B-cpu active"
    }
    "hpa-rps" {
        Remove-AllHpas
        Stop-AgentIfDeployed
        Write-Host "==> Applying $hpaDir/hpa-custom.yaml"
        & kubectl apply -f (Join-Path $hpaDir "hpa-custom.yaml")
        Write-Host "==> Arm B-rps active"
    }
    "agent" {
        Remove-AllHpas
        $agentDeploy = & cmd /c "kubectl -n tools get deployment orchestration-agent -o name 2>NUL"
        if (-not $agentDeploy) {
            Write-Host "==> orchestration-agent not deployed yet."
            Write-Host "    Run ./experiments/scripts/deploy-agent.ps1 first, then re-run this script."
            return
        }
        Write-Host "==> Selecting threshold policy"
        & kubectl -n tools set env deployment/orchestration-agent AGENT_POLICY=policies/thresholds_v1.yaml | Out-Null
        Write-Host "==> Scaling orchestration-agent to 1 (taking over scaling decisions)"
        & kubectl -n tools scale deployment/orchestration-agent --replicas=1
        & kubectl -n tools rollout status deployment/orchestration-agent --timeout=60s
        Write-Host "==> Arm C (custom agent) active. Tail logs with:"
        Write-Host "    kubectl -n tools logs -f deploy/orchestration-agent"
    }
    "agent-predictive" {
        Remove-AllHpas
        $agentDeploy = & cmd /c "kubectl -n tools get deployment orchestration-agent -o name 2>NUL"
        if (-not $agentDeploy) {
            Write-Host "==> orchestration-agent not deployed yet."
            Write-Host "    Run ./experiments/scripts/deploy-agent.ps1 first, then re-run this script."
            return
        }
        Write-Host "==> Selecting predictive policy"
        & kubectl -n tools set env deployment/orchestration-agent AGENT_POLICY=policies/predictive_v1.yaml | Out-Null
        Write-Host "==> Scaling orchestration-agent to 1 (taking over scaling decisions)"
        & kubectl -n tools scale deployment/orchestration-agent --replicas=1
        & kubectl -n tools rollout status deployment/orchestration-agent --timeout=60s
        Write-Host "==> Arm D (predictive agent) active. Tail logs with:"
        Write-Host "    kubectl -n tools logs -f deploy/orchestration-agent"
    }
    "agent-predictive-guarded" {
        Remove-AllHpas
        $agentDeploy = & cmd /c "kubectl -n tools get deployment orchestration-agent -o name 2>NUL"
        if (-not $agentDeploy) {
            Write-Host "==> orchestration-agent not deployed yet."
            Write-Host "    Run ./experiments/scripts/deploy-agent.ps1 first, then re-run this script."
            return
        }
        Write-Host "==> Selecting guarded predictive policy"
        & kubectl -n tools set env deployment/orchestration-agent AGENT_POLICY=policies/predictive_guarded_v1.yaml | Out-Null
        Write-Host "==> Scaling orchestration-agent to 1 (taking over scaling decisions)"
        & kubectl -n tools scale deployment/orchestration-agent --replicas=1
        & kubectl -n tools rollout status deployment/orchestration-agent --timeout=60s
        Write-Host "==> Arm E (guarded predictive agent) active. Tail logs with:"
        Write-Host "    kubectl -n tools logs -f deploy/orchestration-agent"
    }
    "d60" { Activate-AgentPolicy "policies/ablation_d60.yaml" "Arm D60 (pure predictive, cooldown 60s)" }
    "d30" { Activate-AgentPolicy "policies/ablation_d30.yaml" "Arm D30 (pure predictive, cooldown 30s)" }
    "g60" { Activate-AgentPolicy "policies/ablation_g60.yaml" "Arm G60 (guarded predictive, cooldown 60s)" }
    "g30" { Activate-AgentPolicy "policies/ablation_g30.yaml" "Arm G30 (guarded predictive, cooldown 30s)" }
    "f30" { Activate-AgentPolicy "policies/ablation_f30.yaml" "Arm F30 (guards only, prediction ablated, cooldown 30s)" }
    "p30" { Activate-AgentPolicy "policies/ablation_p30.yaml" "Arm P30 (guarded persistence forecast, cooldown 30s)" }
    "h50"  { Activate-Hpa "hpa-custom.yaml"     "Arm H50 (HPA-RPS, target 50 req/s/pod)" }
    "h100" { Activate-Hpa "hpa-custom-100.yaml" "Arm H100 (HPA-RPS, target 100 req/s/pod)" }
    "h115" { Activate-Hpa "hpa-custom-115.yaml" "Arm H115 (HPA-RPS, target 115 req/s/pod)" }
}

Write-Host ""
Write-Host "==> Current state"
& kubectl -n workload get deployment workload-cloud
Write-Host ""
& cmd /c "kubectl -n workload get hpa 2>NUL"
if ($LASTEXITCODE -ne 0) { Write-Host "(no HPA in 'workload' namespace)" }
