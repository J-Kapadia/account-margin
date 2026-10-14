"""Run `controls/CTL-*.sql`. Each file returns the violating rows of a month."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from .paths import ROOT
from .warehouse import month_date

CONTROLS_DIR = ROOT / "controls"


def run_controls(con: duckdb.DuckDBPyConnection, month: str, run_id: str) -> list[dict]:
    """Run every control for `month`, record the results, and return them in ID order.

    Each result: id, status, violating_rows, detail (a list of strings naming the
    violating `_row_hash`es and the number IDs they touch).
    """
    results = []
    for path in sorted(CONTROLS_DIR.glob("CTL-*.sql")):
        control_id = path.stem
        rows = con.execute(path.read_text(encoding="utf-8"), {"month": month_date(month)}).fetchall()
        detail = [
            f"{number_id}: {description}; rows {','.join(hashes)}"
            for number_id, hashes, description in rows
        ]
        status = "fail" if rows else "pass"
        results.append(
            {"id": control_id, "status": status, "violating_rows": len(rows), "detail": detail}
        )
        record(con, month, run_id, results[-1])
    return results


def record(con: duckdb.DuckDBPyConnection, month: str, run_id: str, result: dict) -> None:
    """Store one control result in `ctl.control_results`."""
    con.execute(
        "INSERT INTO ctl.control_results VALUES (?, ?, ?, ?, ?, ?)",
        [run_id, month_date(month), result["id"], result["status"], result["violating_rows"],
         json.dumps(result["detail"])],
    )
