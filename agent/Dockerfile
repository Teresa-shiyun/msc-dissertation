FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY policies ./policies

ENV PYTHONUNBUFFERED=1
ENV PROMETHEUS_URL=http://kube-prom-stack-prometheus.monitoring.svc:9090

ENTRYPOINT ["agent"]
CMD ["run", "--policy", "policies/thresholds_v1.yaml", "--interval", "10"]
