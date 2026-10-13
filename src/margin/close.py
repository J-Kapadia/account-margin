"""`close` (plan §6.2, §6.5): load, build, control, record provenance, mark the month closed.

Phase 0 allocates nothing, so no sign-off gate runs. Earlier months of the books'
window are closed first, in order (catch-up); a month that fails a control stays
without a `close_state` row. The state is derived: dbt rebuilds every mart on each close.
"""

from __future__ import annotations

import uuid

import duckdb

from . import controls, dbt_runner, load, prov
from .books import check_in_window, months_between
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, MarginError
from .paths import Paths
from .warehouse import connect, init_schemas, month_date


def _public(results: list[dict]) -> list[dict]:
    return [{k: c[k] for k in ("id", "status", "violating_rows")} for c in results]


def _fail_controls(month: str, results: list[dict], *, prefix: str = "") -> MarginError:
    failed = [c for c in results if c["status"] == "fail"]
    names = ", ".join(c["id"] for c in failed)
    return MarginError(
        EXIT_CONTROL,
        f"{prefix}control failed for {month}: {names}",
        month=month,
        controls=_public(results),
        failed_controls=[
            {"id": c["id"], "violating_rows": c["violating_rows"], "detail": c["detail"]}
            for c in failed
        ],
    )


def close_month(paths: Paths, month: str) -> dict:
    """Close `month` (and any earlier month of the window); return the envelope fields."""
    first, _ = check_in_window(paths.books, month)
    if not load.books_have_rows(paths.books, month):
        raise MarginError(EXIT_PRECONDITION, f"no books for month {month}", month=month)
    months = months_between(first, month)
    run_id = uuid.uuid4().hex

    con = connect(paths.warehouse)
    try:
        init_schemas(con)
        load.check_books_identity(con, paths.books)
        load.load_months(con, paths.books, first, month, run_id)
    finally:
        con.close()  # dbt needs the file: margin holds no connection while it runs

    dbt_runner.build(paths.warehouse)

    con = connect(paths.warehouse)
    try:
        for m in months:
            result = _close_one(con, m, run_id, catch_up=m != month)
        return result
    finally:
        con.close()


def _close_one(con: duckdb.DuckDBPyConnection, month: str, run_id: str, *, catch_up: bool) -> dict:
    results = controls.run_controls(con, month, run_id)
    if any(c["status"] == "fail" for c in results):
        raise _fail_controls(month, results, prefix="catch-up close stopped: " if catch_up else "")

    records = prov.build_records(
        con, month, checks=[c["id"] for c in results], run_id=run_id
    )
    new_hash = prov.result_hash(records)
    stored = con.execute(
        "SELECT result_hash FROM state.close_state WHERE month = ?", [month_date(month)]
    ).fetchone()
    if stored is not None and stored[0] != new_hash:
        # EC-05: a closed month whose recomputed result differs is a failure, not a rewrite.
        lock = {
            "id": "CTL-LOCK", "violating_rows": 1,
            "detail": [f"{month}: stored result_hash {stored[0]}, recomputed {new_hash}"],
        }
        raise MarginError(
            EXIT_CONTROL, f"closed month {month} changed: result hash differs",
            month=month, controls=_public(results) + [{"id": "CTL-LOCK", "status": "fail",
                                                      "violating_rows": 1}],
            failed_controls=[lock],
        )
    if stored is None:
        con.execute("BEGIN")
        try:
            prov.write_records(con, records)
            con.execute(
                "INSERT INTO state.close_state VALUES (?, 'closed', ?, ?, ?, NULL)",
                [month_date(month), prov.month_input_digest(records), new_hash, run_id],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return {"month": month, "controls": _public(results), "result_hash": new_hash}
