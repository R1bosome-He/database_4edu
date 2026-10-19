from __future__ import annotations

from typing import Any

from .models import QueryResult


def format_result(result: QueryResult) -> str:
    if result.kind == "message":
        return result.message
    if result.kind == "explain":
        return f"{result.message}\n{result.plan}"
    if result.kind == "table":
        parts = []
        if result.plan:
            parts.append("Plan:\n" + result.plan)
        parts.append(format_table(result.columns, result.rows))
        if result.metrics:
            parts.append(format_metrics(result.metrics))
        return "\n\n".join(parts)
    if result.kind == "compare":
        off, on = result.comparisons
        rows = [
            {
                "mode": "optimizer_off",
                "elapsed_ms": off.metrics.get("elapsed_ms"),
                "disk_reads": off.metrics.get("disk_reads"),
                "cache_hits": off.metrics.get("cache_hits"),
                "pages_touched": off.metrics.get("pages_touched"),
                "rows": off.metrics.get("rows"),
            },
            {
                "mode": "optimizer_on",
                "elapsed_ms": on.metrics.get("elapsed_ms"),
                "disk_reads": on.metrics.get("disk_reads"),
                "cache_hits": on.metrics.get("cache_hits"),
                "pages_touched": on.metrics.get("pages_touched"),
                "rows": on.metrics.get("rows"),
            },
        ]
        return "\n\n".join(
            [
                "Optimizer OFF plan:\n" + off.plan,
                "Optimizer ON plan:\n" + on.plan,
                format_table(list(rows[0].keys()), rows),
            ]
        )
    return result.message or repr(result)


def format_table(columns: list[str], rows: list[dict[str, Any]], limit: int = 30) -> str:
    if not rows:
        return "(empty result)"
    columns = columns or list(rows[0].keys())
    visible_rows = rows[:limit]
    widths = {
        column: max(len(column), *(len(_cell(row.get(column))) for row in visible_rows))
        for column in columns
    }
    sep = "+-" + "-+-".join("-" * widths[column] for column in columns) + "-+"
    header = "| " + " | ".join(column.ljust(widths[column]) for column in columns) + " |"
    body = [
        "| " + " | ".join(_cell(row.get(column)).ljust(widths[column]) for column in columns) + " |"
        for row in visible_rows
    ]
    if len(rows) > limit:
        body.append(f"... {len(rows) - limit} more rows")
    return "\n".join([sep, header, sep, *body, sep])


def format_metrics(metrics: dict[str, Any]) -> str:
    keys = [
        "optimizer",
        "elapsed_ms",
        "rows",
        "disk_reads",
        "disk_writes",
        "cache_hits",
        "cache_misses",
        "pages_touched",
        "evictions",
    ]
    return "Metrics: " + ", ".join(f"{key}={metrics[key]}" for key in keys if key in metrics)


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value)

