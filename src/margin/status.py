"""`status`: the months of `state.close_state` with their result hashes, in month order."""

from __future__ import annotations

import duckdb


def status(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """One entry per month with a `close_state` row; an `open` month has a null `result_hash`."""
    rows = con.execute(
        "SELECT strftime(month, '%Y-%m'), status, result_hash, restated_from "
        "FROM state.close_state ORDER BY month"
    ).fetchall()
    return [
        {"month": month, "status": state, "result_hash": result_hash, "restated_from": restated_from}
        for month, state, result_hash, restated_from in rows
    ]


def render(months: list[dict]) -> list[str]:
    """`2026-03 locked <hash>`; a restated month adds `(restated from <hash>)`."""
    lines = []
    for m in months:
        line = f"{m['month']} {m['status']} {m['result_hash'] or '-'}"
        if m["restated_from"]:
            line += f" (restated from {m['restated_from']})"
        lines.append(line)
    return lines
