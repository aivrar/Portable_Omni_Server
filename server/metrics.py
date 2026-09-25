"""Lightweight Prometheus exporter for the gateway.

We don't pull in the official ``prometheus_client`` dep — for a single
process serving a handful of routes, hand-rolling exposition is easier
than learning that library's nuances. The exporter renders a small set
of counters and gauges in the standard text format:

    omni_requests_total{path="...",method="...",status="..."} 12
    omni_request_seconds_sum{path="...",method="..."} 3.4
    omni_request_seconds_count{path="...",method="..."} 12
    omni_jobs{kind="...",status="..."} 5
    omni_workers{model="...",status="..."} 1
    omni_comfy_instances{status="..."} 2

The middleware in ``omni_comfy_server.py`` calls ``record_request(...)``
on every response. Worker / comfy / job gauges are sampled at scrape
time so they stay fresh without extra bookkeeping.

Path label cardinality is bounded: ``{worker_id}``, ``{instance_id}``,
``{filename}``, ``{job_id}``, ``{relpath:path}`` and any UUID-shaped
path segment are folded into a literal placeholder. This keeps the
metric set small even when callers hammer per-resource routes.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict


_PATH_TEMPLATE_PLACEHOLDERS = (
    # Match UUIDs (with or without dashes) and 16+ hex/alnum tokens that look
    # like ids; replace each with a literal `:id` segment so cardinality
    # stays bounded.
    (re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "/:id"),
    (re.compile(r"/[A-Za-z0-9_.-]{16,}"), "/:id"),
)


def normalise_path(raw_path: str) -> str:
    """Fold dynamic segments into stable placeholders.

    Doesn't pull from FastAPI's route table because the middleware fires
    before the router has matched. Heuristics on the raw path are fine —
    the worst case is a few extra metric series, not a leak.
    """
    out = raw_path.split("?", 1)[0]
    for regex, repl in _PATH_TEMPLATE_PLACEHOLDERS:
        out = regex.sub(repl, out)
    return out


class MetricsCollector:
    """Thread-safe in-memory counter / histogram store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._req_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._req_seconds_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._req_seconds_count: dict[tuple[str, str], int] = defaultdict(int)
        self._errors: dict[tuple[str, str], int] = defaultdict(int)

    def record_request(self, *, path: str, method: str,
                       status: int, elapsed: float) -> None:
        norm = normalise_path(path)
        with self._lock:
            self._req_total[(norm, method, str(status))] += 1
            self._req_seconds_sum[(norm, method)] += elapsed
            self._req_seconds_count[(norm, method)] += 1
            if status >= 500:
                self._errors[(norm, method)] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "req_total": dict(self._req_total),
                "req_seconds_sum": dict(self._req_seconds_sum),
                "req_seconds_count": dict(self._req_seconds_count),
                "errors": dict(self._errors),
            }


metrics = MetricsCollector()


def _label_str(**kw) -> str:
    parts: list[str] = []
    for k, v in kw.items():
        # Escape backslash and quote per Prometheus exposition format
        s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{k}="{s}"')
    return "{" + ",".join(parts) + "}"


def render_prometheus(*, jobs_store=None,
                      worker_registry=None,
                      comfy_registry=None) -> str:
    """Render the current metrics in Prometheus text exposition format."""
    snap = metrics.snapshot()
    lines: list[str] = []

    lines.append("# HELP omni_requests_total Total HTTP requests by path/method/status")
    lines.append("# TYPE omni_requests_total counter")
    for (path, method, status), n in sorted(snap["req_total"].items()):
        lines.append(f'omni_requests_total{_label_str(path=path, method=method, status=status)} {n}')

    lines.append("# HELP omni_request_seconds Request latency by path+method")
    lines.append("# TYPE omni_request_seconds summary")
    for (path, method), total in sorted(snap["req_seconds_sum"].items()):
        count = snap["req_seconds_count"].get((path, method), 0)
        lines.append(f'omni_request_seconds_sum{_label_str(path=path, method=method)} {total:.6f}')
        lines.append(f'omni_request_seconds_count{_label_str(path=path, method=method)} {count}')

    lines.append("# HELP omni_request_errors_total HTTP requests with status >= 500")
    lines.append("# TYPE omni_request_errors_total counter")
    for (path, method), n in sorted(snap["errors"].items()):
        lines.append(f'omni_request_errors_total{_label_str(path=path, method=method)} {n}')

    if jobs_store is not None:
        kinds_status: dict[tuple[str, str], int] = defaultdict(int)
        for j in jobs_store.list():
            kinds_status[(j.kind, j.status)] += 1
        lines.append("# HELP omni_jobs Active and historical jobs by kind/status")
        lines.append("# TYPE omni_jobs gauge")
        for (kind, status), n in sorted(kinds_status.items()):
            lines.append(f'omni_jobs{_label_str(kind=kind, status=status)} {n}')

    if worker_registry is not None:
        worker_summary: dict[tuple[str, str], int] = defaultdict(int)
        worker_vram: dict[str, int] = defaultdict(int)
        for w in worker_registry.all_workers():
            worker_summary[(w.model, w.status)] += 1
            worker_vram[w.model] += getattr(w, "vram_used_mb", 0) or 0
        lines.append("# HELP omni_workers Worker count by model/status")
        lines.append("# TYPE omni_workers gauge")
        for (model, status), n in sorted(worker_summary.items()):
            lines.append(f'omni_workers{_label_str(model=model, status=status)} {n}')
        lines.append("# HELP omni_worker_vram_used_mb VRAM used by workers (MiB) by model")
        lines.append("# TYPE omni_worker_vram_used_mb gauge")
        for model, mb in sorted(worker_vram.items()):
            lines.append(f'omni_worker_vram_used_mb{_label_str(model=model)} {mb}')

    if comfy_registry is not None:
        comfy_summary: dict[str, int] = defaultdict(int)
        for inst in comfy_registry.all_instances():
            comfy_summary[inst.status] += 1
        lines.append("# HELP omni_comfy_instances ComfyUI instance count by status")
        lines.append("# TYPE omni_comfy_instances gauge")
        for status, n in sorted(comfy_summary.items()):
            lines.append(f'omni_comfy_instances{_label_str(status=status)} {n}')

    lines.append(f"omni_metrics_scraped_unixtime {time.time():.0f}")
    return "\n".join(lines) + "\n"
