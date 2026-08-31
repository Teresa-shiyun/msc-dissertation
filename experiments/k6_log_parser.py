from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


_CHECKS_RE = re.compile(r"checks\.+:\s+([\d.]+)%\s+(\d+)\s+out of\s+(\d+)")
_HTTP_REQS_RE = re.compile(r"http_reqs\.+:\s+(\d+)\s+([\d.]+)/s")
_HTTP_REQ_FAILED_RE = re.compile(r"http_req_failed\.+:\s+([\d.]+)%\s+(\d+)\s+out of\s+(\d+)")
_HTTP_REQ_DURATION_RE = re.compile(
    r"http_req_duration\.+:\s+avg=([\d.]+)(µs|ms|s)\s+min=[\d.]+(?:µs|ms|s)\s+"
    r"med=([\d.]+)(µs|ms|s)\s+max=[\d.]+(?:µs|ms|s)\s+"
    r"p\(90\)=([\d.]+)(µs|ms|s)\s+p\(95\)=([\d.]+)(µs|ms|s)"
)


def _to_ms(value: str, unit: str) -> float:
    v = float(value)
    if unit == "µs":
        return v / 1000.0
    if unit == "ms":
        return v
    if unit == "s":
        return v * 1000.0
    raise ValueError(f"unknown k6 duration unit: {unit}")


def parse_k6_stdout_summary(log_text: str) -> dict:
    result: dict[str, Optional[float]] = {
        "checks_pass_rate": None,
        "checks_passed": None,
        "checks_total": None,
        "http_reqs_count": None,
        "http_reqs_rate": None,
        "http_req_failed_rate": None,
        "http_req_failed_count": None,
        "lat_avg_ms": None,
        "lat_med_ms": None,
        "lat_p90_ms": None,
        "lat_p95_ms": None,
    }

    m = _CHECKS_RE.search(log_text)
    if m:
        result["checks_pass_rate"] = float(m.group(1)) / 100.0
        result["checks_passed"] = int(m.group(2))
        result["checks_total"] = int(m.group(3))

    m = _HTTP_REQS_RE.search(log_text)
    if m:
        result["http_reqs_count"] = int(m.group(1))
        result["http_reqs_rate"] = float(m.group(2))

    m = _HTTP_REQ_FAILED_RE.search(log_text)
    if m:
        result["http_req_failed_rate"] = float(m.group(1)) / 100.0
        result["http_req_failed_count"] = int(m.group(2))

    m = _HTTP_REQ_DURATION_RE.search(log_text)
    if m:
        result["lat_avg_ms"] = round(_to_ms(m.group(1), m.group(2)), 3)
        result["lat_med_ms"] = round(_to_ms(m.group(3), m.group(4)), 3)
        result["lat_p90_ms"] = round(_to_ms(m.group(5), m.group(6)), 3)
        result["lat_p95_ms"] = round(_to_ms(m.group(7), m.group(8)), 3)

    return result


def parse_k6_stdout_file(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_k6_stdout_summary(text)
