"""Output renderers for the CLI: text, json, jsonl."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable


def emit_json(obj: Any, fh=None) -> None:
    fh = fh or sys.stdout
    fh.write(json.dumps(obj, indent=2, default=str))
    fh.write("\n")
    fh.flush()


def emit_jsonl(records: Iterable[dict], fh=None) -> None:
    fh = fh or sys.stdout
    for rec in records:
        fh.write(json.dumps(rec, default=str))
        fh.write("\n")
    fh.flush()


def emit_table(rows: list[dict], columns: list[str], fh=None) -> None:
    """Format ``rows`` as an aligned text table.

    Pure stdlib - we don't pull in tabulate. If there are no rows, prints a
    single ``(no rows)`` placeholder.
    """
    fh = fh or sys.stdout
    if not rows:
        fh.write("(no rows)\n")
        return
    widths = {c: len(c) for c in columns}
    out_rows: list[list[str]] = []
    for r in rows:
        row = [str(r.get(c, "")) for c in columns]
        out_rows.append(row)
        for c, val in zip(columns, row):
            widths[c] = max(widths[c], len(val))
    # header
    fh.write("  ".join(c.ljust(widths[c]) for c in columns))
    fh.write("\n")
    fh.write("  ".join("-" * widths[c] for c in columns))
    fh.write("\n")
    for row in out_rows:
        fh.write("  ".join(val.ljust(widths[c]) for c, val in zip(columns, row)))
        fh.write("\n")
    fh.flush()


def emit(data: Any, fmt: str, *, columns: list[str] | None = None) -> None:
    """Render ``data`` according to ``fmt``: text|json|jsonl."""
    if fmt == "json":
        emit_json(data)
        return
    if fmt == "jsonl":
        if isinstance(data, list):
            emit_jsonl(data)
        elif isinstance(data, dict):
            emit_jsonl([data])
        else:
            emit_jsonl([{"value": data}])
        return
    # text fallback
    if isinstance(data, list) and data and isinstance(data[0], dict):
        cols = columns or sorted(data[0].keys())
        emit_table(data, cols)
        return
    emit_json(data)
