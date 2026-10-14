"""`close`: gate, load, build, control, record provenance, mark the month closed.

The sign-off gate is the first step and runs before anything is read or written. Every earlier
month of the books' window that is not yet closed is closed first, in order (catch-up); a month
that fails a control stays without a `closed` row. The state is derived: dbt rebuilds every mart on
each close, and a month already closed is recomputed and left as it is when its result hash
is unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import duckdb

from . import check as check_mod
from . import controls, dbt_runner, load, prov, signoff
from .books import check_in_window, months_between
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, MarginError
from .gen_config import load_config
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


def close_month(paths: Paths, month: str, *, check: bool = False) -> dict:
    """Close `month` (and any earlier month of the window); return the envelope fields.

    With `check`, the target month is compared with the answer key before it is marked closed;
    a mismatch leaves it unclosed. On a month already closed the comparison is a read.
    """
    if check:
        check_mod.require_truth(paths.truth, month=month)
    first, _ = check_in_window(paths.books, month)
    sign = signoff.verify(paths.config, month=month)  # exit 4 before anything is read or written
    months = months_between(first, month)
    for m in months:  # every month the close will touch has books, checked before any write
        if not load.books_have_rows(paths.books, m):
            raise MarginError(EXIT_PRECONDITION, f"no books for month {m}", month=m)
    config = load_config(paths.config)
    run_id = uuid.uuid4().hex

    con = connect(paths.warehouse)
    try:
        init_schemas(con)
        load.check_books_identity(con, paths.books)
        load.load_months(con, paths.books, first, month, run_id)
        load.load_reference(con, paths.prices)
        load.load_config(con, paths.config)
        stored = con.execute(
            "SELECT status FROM state.close_state WHERE month = ?", [month_date(month)]
        ).fetchone()
        read_only_check = check and stored is not None and stored[0] in ("closed", "locked")
    finally:
        con.close()  # dbt needs the file: margin holds no connection while it runs

    dbt_runner.build(paths.warehouse, paths.config)

    con = connect(paths.warehouse)
    try:
        known: dict[str, dict] = {}
        for m in months:
            result = _close_one(
                con, m, run_id, sign, config, known, catch_up=m != month,
                truth=paths.truth if check and m == month else None,
            )
        # The month-less records describe the files this run loaded; they sit outside result_hash.
        # A `--check` of a month already closed changes no state, so it leaves them as they are.
        if not read_only_check:
            _write(con, prov.reference_records(con, run_id=run_id, signoff=sign))
        return result
    finally:
        con.close()


def _write(con: duckdb.DuckDBPyConnection, records: list[dict]) -> None:
    con.execute("BEGIN")
    try:
        prov.write_records(con, records)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _close_one(
    con: duckdb.DuckDBPyConnection, month: str, run_id: str, sign: signoff.Signoff, config,
    known: dict[str, dict], *, catch_up: bool, truth: Path | None = None,
) -> dict:
    gate = {"id": signoff.GATE_ID, "status": "pass", "violating_rows": 0, "detail": []}
    controls.record(con, month, run_id, gate)
    results = [gate] + controls.run_controls(con, month, run_id)
    if any(c["status"] == "fail" for c in results):
        raise _fail_controls(month, results, prefix="catch-up close stopped: " if catch_up else "")

    records = prov.build_records(
        con, month, checks=[c["id"] for c in results], run_id=run_id, signoff=sign, config=config,
        known=known,
    )
    known.update({r["number_id"]: r for r in records})
    new_hash = prov.result_hash(records)
    stored = con.execute(
        "SELECT status, result_hash FROM state.close_state WHERE month = ?", [month_date(month)]
    ).fetchone()
    if stored is not None and stored[0] in ("closed", "locked"):
        if stored[1] != new_hash:
            # A closed month whose recomputed result differs is a failure, not a rewrite.
            lock = {
                "id": "CTL-LOCK", "violating_rows": 1,
                "detail": [f"{month}: stored result_hash {stored[1]}, recomputed {new_hash}"],
            }
            raise MarginError(
                EXIT_CONTROL, f"closed month {month} changed: result hash differs",
                month=month, controls=_public(results) + [{"id": "CTL-LOCK", "status": "fail",
                                                          "violating_rows": 1}],
                failed_controls=[lock],
            )
        return _checked(con, month, truth, config, records, results)
    # No row, or an `open` one: step 7 runs before the month is marked closed, so a mismatch
    # leaves it open. Then it goes open -> closed with its records, in one transaction.
    outcome = _checked(con, month, truth, config, records, results)
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM prov.records WHERE month = ?", [month_date(month)])
        prov.write_records(con, records)
        con.execute(
            "INSERT OR REPLACE INTO state.close_state VALUES (?, 'closed', ?, ?, ?, NULL)",
            [month_date(month), prov.month_input_digest(records), new_hash, run_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return outcome


def _checked(
    con: duckdb.DuckDBPyConnection, month: str, truth: Path | None, config, records: list[dict],
    results: list[dict],
) -> dict:
    """The close outcome; with `truth`, the answer-key comparison runs first and may exit 5 or 6."""
    controls_out = _public(results)
    if truth is not None:
        checks = check_mod.evaluate(con, month, truth, config, records)
        check_mod.raise_for(month, checks, controls_out)
        controls_out = controls_out + _public(checks)
    return {"month": month, "controls": controls_out, "result_hash": prov.result_hash(records)}
