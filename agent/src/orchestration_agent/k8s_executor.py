from __future__ import annotations

import logging
from typing import Optional

from kubernetes import client, config


log = logging.getLogger(__name__)


class K8sExecutor:
    def __init__(
        self,
        namespace: str,
        deployment: str,
        in_cluster: Optional[bool] = None,
        dry_run: bool = False,
    ) -> None:
        if in_cluster is None:
            in_cluster = self._detect_in_cluster()
        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        self.apps = client.AppsV1Api()
        self.namespace = namespace
        self.deployment = deployment
        self.dry_run = dry_run

    @staticmethod
    def _detect_in_cluster() -> bool:
        import os
        return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

    def get_replicas(self) -> int:
        scale = self.apps.read_namespaced_deployment_scale(self.deployment, self.namespace)
        return int(scale.spec.replicas or 0)

    def set_replicas(self, n: int) -> None:
        if self.dry_run:
            log.info("DRY-RUN would patch %s/%s scale to %d", self.namespace, self.deployment, n)
            return
        body = {"spec": {"replicas": int(n)}}
        self.apps.patch_namespaced_deployment_scale(
            name=self.deployment,
            namespace=self.namespace,
            body=body,
        )
