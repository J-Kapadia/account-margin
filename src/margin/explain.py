"""`explain` (plan §6.1): print a number's provenance chain down to its source rows.

Digests are re-derived on the way: a leaf from the rows its `source_query`
selects, a composite from its children's stored digests. A mismatch exits 6.
"""

from __future__ import annotations

import difflib
import json

import duckdb

from . import prov
from .errors import EXIT_PRECONDITION, EXIT_PROVENANCE, EXIT_USAGE, MarginError

RECORD_COLS = [
    "number_id", "month", "value_cents", "value_text", "unit", "step", "git_sha",
    "producer_kind", "producer_id", "input_digest", "input_row_count", "source_query",
    "checks_passed", "run_id",
]


def _record(con: duckdb.DuckDBPyConnection, number_id: str) -> dict | None:
    row = con.execute(
        f"SELECT {', '.join(RECORD_COLS)} FROM prov.records WHERE number_id = ?", [number_id]
    ).fetchone()
    if row is None:
        return None
    rec = dict(zip(RECORD_COLS, row))
    rec["month"] = rec["month"].strftime("%Y-%m") if rec["month"] else None
    return rec


def require_closed(con: duckdb.DuckDBPyConnection, month: str) -> None:
    """Read commands accept a `closed` or `locked` month; anything else exits 4 (plan §6.5)."""
    row = con.execute(
        "SELECT status FROM state.close_state WHERE month = ?", [f"{month}-01"]
    ).fetchone()
    if row is None or row[0] not in ("closed", "locked"):
        raise MarginError(EXIT_PRECONDITION, f"month {month} not closed", month=month)


def _node(con: duckdb.DuckDBPyConnection, number_id: str, depth: int | None, level: int) -> dict:
    rec = _record(con, number_id)
    if rec is None:
        raise MarginError(EXIT_PROVENANCE, f"provenance incomplete: no record for {number_id}")
    node = {k: v for k, v in rec.items() if k != "source_query"}
    node["source_query"] = rec["source_query"]
    if prov.is_composite(rec["source_query"]):
        child_ids = json.loads(rec["source_query"])
        children = [_record(con, c) for c in child_ids]
        missing = [c for c, r in zip(child_ids, children) if r is None]
        if missing:
            raise MarginError(EXIT_PROVENANCE, f"provenance incomplete: no record for {missing[0]}")
        derived = prov.digest_children([(r["number_id"], r["input_digest"]) for r in children])
        node["children"] = (
            [_node(con, c, depth, level + 1) for c in child_ids]
            if depth is None or level < depth
            else [{"number_id": c} for c in child_ids]
        )
    else:
        rows = con.execute(rec["source_query"]).fetchall()
        derived = prov.digest_rows([r[1] for r in rows])
        node["rows"] = [
            {"source_table": t, "_row_hash": h, "row": json.loads(j)} for t, h, j in rows
        ]
    if derived != rec["input_digest"]:
        raise MarginError(
            EXIT_PROVENANCE,
            f"digest mismatch for {number_id}: stored {rec['input_digest']}, derived {derived}",
            month=rec["month"],
        )
    node["digest_verified"] = True
    return node


def _unknown(con: duckdb.DuckDBPyConnection, number_id: str) -> MarginError:
    known = [r[0] for r in con.execute("SELECT number_id FROM prov.records").fetchall()]
    near = difflib.get_close_matches(number_id, known, n=3, cutoff=0.0)
    hint = f"; nearest: {', '.join(near)}" if near else ""
    return MarginError(EXIT_USAGE, f"unknown number ID {number_id}{hint}")


def explain(con: duckdb.DuckDBPyConnection, number_id: str, depth: int | None = None) -> dict:
    month = prov.month_of_id(number_id)
    if month is None:
        raise _unknown(con, number_id)
    require_closed(con, month)  # an open month exits 4 even before its IDs exist
    if _record(con, number_id) is None:
        raise _unknown(con, number_id)
    return _node(con, number_id, depth, 0)


def render(node: dict, indent: int = 0) -> list[str]:
    pad = "  " * indent
    value = node.get("value_cents")
    head = f"{pad}{node['number_id']}"
    if value is not None:
        head += f" = {value} {node.get('unit', '')}".rstrip()
    lines = [head]
    if "step" in node:
        lines.append(f"{pad}  step {node['step']}; digest {node['input_digest']} "
                     f"over {node['input_row_count']} inputs (verified)")
    for row in node.get("rows", []):
        lines.append(f"{pad}  source {row['source_table']} _row_hash={row['_row_hash']} "
                     f"{json.dumps(row['row'], sort_keys=True)}")
    for child in node.get("children", []):
        lines.extend(render(child, indent + 1))
    return lines
