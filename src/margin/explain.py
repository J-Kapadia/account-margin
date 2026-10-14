"""`explain`: print a number's provenance chain down to its source rows.

Digests are re-derived on the way: a leaf from the rows its `source_query` selects, a
composite from its children's stored digests. A mismatch exits 6. Every family of the closed
ID grammar is answered. A number with a month slot needs that month `closed` or `locked` (exit 4);
a scenario number's month is its base month and it also needs its records (`scenario not run`).
A month-less number (`ref/`, `cfg/`, `bvb/`) is answered from its record alone; with no record it exits 4
("run close") only while the warehouse holds no records at all, else it is an unknown ID (exit 2).
"""

from __future__ import annotations

import difflib
import json

import duckdb

from . import prov
from .errors import EXIT_PRECONDITION, EXIT_PROVENANCE, EXIT_USAGE, MarginError

def _record(con: duckdb.DuckDBPyConnection, number_id: str) -> dict | None:
    rec = prov.fetch_record(con, number_id)
    if rec is not None:
        rec["month"] = rec["month"].strftime("%Y-%m") if rec["month"] else None
    return rec


def require_closed(con: duckdb.DuckDBPyConnection, month: str) -> None:
    """Read commands accept a `closed` or `locked` month; anything else exits 4."""
    row = con.execute(
        "SELECT status FROM state.close_state WHERE month = ?", [f"{month}-01"]
    ).fetchone()
    if row is None or row[0] not in ("closed", "locked"):
        raise MarginError(EXIT_PRECONDITION, f"month {month} not closed", month=month)


class _Walk:
    """One explain call: each number is verified once; a repeat is shown as a stub."""

    def __init__(self, con: duckdb.DuckDBPyConnection, depth: int | None):
        self.con, self.depth = con, depth
        self.records: dict[str, dict | None] = {}
        self.expanded: set[str] = set()

    def record(self, number_id: str) -> dict | None:
        if number_id not in self.records:
            self.records[number_id] = _record(self.con, number_id)
        return self.records[number_id]

    def node(self, number_id: str, level: int) -> dict:
        rec = self.record(number_id)
        if rec is None:
            raise MarginError(EXIT_PROVENANCE, f"provenance incomplete: no record for {number_id}")
        node = {k: v for k, v in rec.items() if k not in ("source_query", "checks_passed")}
        node["checks_passed"] = list(rec["checks_passed"] or [])
        node["source_query"] = rec["source_query"]
        derived, inputs, _ = prov.rederive(self.con, rec, self.record)
        if derived != rec["input_digest"]:
            raise MarginError(
                EXIT_PROVENANCE,
                f"digest mismatch for {number_id}: stored {rec['input_digest']}, derived {derived}",
                month=rec["month"],
            )
        node["digest_verified"] = True
        if number_id in self.expanded:
            node["repeated"] = True  # its chain is printed where it first appears
            return node
        self.expanded.add(number_id)
        if prov.is_composite(rec["source_query"]):
            child_ids = [c["number_id"] for c in inputs]
            node["children"] = (
                [self.node(c, level + 1) for c in child_ids]
                if self.depth is None or level < self.depth
                else [self._stub(c) for c in child_ids]
            )
        else:
            node["rows"] = [
                {"source_table": t, "_row_hash": h, "row": json.loads(j)} for t, h, j in inputs
            ]
        return node

    def _stub(self, number_id: str) -> dict:
        rec = self.record(number_id)
        return {k: rec[k] for k in ("number_id", "value_cents", "value_text", "unit", "input_digest")} | {
            "truncated": True
        }


def _unknown(con: duckdb.DuckDBPyConnection, number_id: str) -> MarginError:
    known = [r[0] for r in con.execute("SELECT number_id FROM prov.records").fetchall()]
    near = difflib.get_close_matches(number_id, known, n=3, cutoff=0.0)
    hint = f"; nearest: {', '.join(near)}" if near else ""
    return MarginError(EXIT_USAGE, f"unknown number ID {number_id}{hint}")


def explain(con: duckdb.DuckDBPyConnection, number_id: str, depth: int | None = None) -> dict:
    family = prov.family_of(number_id)
    if family is None:
        raise _unknown(con, number_id)  # outside the closed grammar: exit 2, never an empty success
    month = prov.month_of_id(number_id)
    if month is not None:
        require_closed(con, month)  # an open month exits 4 even before its IDs exist
    if _record(con, number_id) is None:
        if family == "scenario":
            raise MarginError(EXIT_PRECONDITION, f"scenario not run for month {month}", month=month)
        if family in prov.MONTH_LESS_FAMILIES and not con.execute("SELECT count(*) FROM prov.records").fetchone()[0]:
            raise MarginError(EXIT_PRECONDITION, f"no provenance record for {number_id}: run close")
        raise _unknown(con, number_id)
    return _Walk(con, depth).node(number_id, 0)


def _value(node: dict) -> str:
    if node.get("value_cents") is not None:
        return f"{node['value_cents']} {node.get('unit', '')}".rstrip()
    if node.get("value_text") is not None:
        return node["value_text"] if node.get("unit") in (None, "ref") else f"{node['value_text']} {node['unit']}"
    return ""


def render(node: dict, indent: int = 0) -> list[str]:
    pad = "  " * indent
    head = f"{pad}{node['number_id']}"
    if _value(node):
        head += f" = {_value(node)}"
    lines = [head]
    if node.get("truncated"):
        return [head + "  (below --depth)"]
    if "step" in node:
        note = "verified" if not node.get("repeated") else "verified; chain shown above"
        lines.append(f"{pad}  step {node['step']}; digest {node['input_digest']} "
                     f"over {node['input_row_count']} inputs ({note})")
    for row in node.get("rows", []):
        lines.append(f"{pad}  source {row['source_table']} _row_hash={row['_row_hash']} "
                     f"{json.dumps(row['row'], sort_keys=True)}")
    for child in node.get("children", []):
        lines.extend(render(child, indent + 1))
    return lines
