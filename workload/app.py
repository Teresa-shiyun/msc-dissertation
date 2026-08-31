from __future__ import annotations

import hashlib
import os
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

NODE_NAME = os.getenv("NODE_NAME", "unknown")
POD_NAME = os.getenv("POD_NAME", "unknown")
TIER = os.getenv("TIER", "unknown")

app = FastAPI()

REQUESTS = Counter(
    "workload_requests_total",
    "Total HTTP requests handled by the workload",
    ["route", "tier"],
)
LATENCY = Histogram(
    "workload_request_duration_seconds",
    "Wall-clock latency of HTTP requests, measured around the whole handler.",
    ["route", "tier"],
    buckets=(
        0.001, 0.002, 0.005, 0.010, 0.020, 0.050, 0.075,
        0.100, 0.150, 0.200, 0.300, 0.500, 0.750,
        1.0, 2.0, 5.0,
    ),
)
INFLIGHT = Gauge(
    "workload_inflight_requests",
    "In-flight requests",
    ["tier"],
)


@app.middleware("http")
async def observe_latency(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    route = request.url.path
    REQUESTS.labels(route=route, tier=TIER).inc()
    INFLIGHT.labels(tier=TIER).inc()
    start = time.perf_counter()
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed = time.perf_counter() - start
        LATENCY.labels(route=route, tier=TIER).observe(elapsed)
        INFLIGHT.labels(tier=TIER).dec()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "pod": POD_NAME, "node": NODE_NAME, "tier": TIER}


@app.get("/work")
def work(n: int = 5000) -> dict:
    digest = b"\x00" * 32
    for i in range(max(1, n)):
        digest = hashlib.sha256(digest + i.to_bytes(4, "little")).digest()
    return {
        "ok": True,
        "n": n,
        "digest": digest.hex()[:16],
        "pod": POD_NAME,
        "tier": TIER,
    }


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
