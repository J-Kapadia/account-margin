"""`close`: gate, load, build, control, record provenance, mark the month closed.

The sign-off gate is the first step and runs before anything is read or written. Every earlier
month of the books' window that is not yet closed is closed first, in order (catch-up); a month
that fails a control stays without a `closed` row. The state is derived: dbt rebuilds every mart on
each close, and a month already closed is recomputed and left as it is when its result hash
is unchanged.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import duckdb

from . import check as check_mod
from . import controls, dbt_runner, load, prov, signoff
from .books import check_in_window, months_between
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, EXIT_PROVENANCE, MarginError
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


_CORRECT_IN_OPEN_MONTH = "correct it with an entry in the open month"
_CHANGED_MONTH = re.compile(r"^raw\.(?P<table>\w+): month (?P<month>\d{4}-\d{2}) changed since it was loaded$")


def _lock_failure(
    con: duckdb.DuckDBPyConnection, month: str, run_id: str, message: str, detail: list[str],
    results: list[dict] | None = None,
) -> MarginError:
    """Record a failed CTL-LOCK for `month` and return the exit 3 error that reports it."""
    verdict = {"id": "CTL-LOCK", "status": "fail", "violating_rows": 1, "detail": detail}
    controls.record(con, month, run_id, verdict)
    return MarginError(
        EXIT_CONTROL, message, month=month, controls=_public(results or []) + _public([verdict]),
        failed_controls=[{k: verdict[k] for k in ("id", "violating_rows", "detail")}],
    )


def _changed_month_error(
    con: duckdb.DuckDBPyConnection, run_id: str, err: MarginError
) -> MarginError | None:
    """The CTL-LOCK failure for a load that found a closed or locked month's books changed;
    None when the error is anything else (an open month, or the books as a whole)."""
    found = _CHANGED_MONTH.match(err.message) if err.exit_code == EXIT_CONTROL else None
    if found is None:
        return None
    month = found["month"]
    stored = con.execute(
        "SELECT status FROM state.close_state WHERE month = ?", [month_date(month)]
    ).fetchone()
    if stored is None or stored[0] not in ("closed", "locked"):
        return None
    return _lock_failure(
        con, month, run_id, f"{stored[0]} month {month} changed: {found['table']}; "
        f"{_CORRECT_IN_OPEN_MONTH}", [err.message],
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
        try:
            load.load_months(con, paths.books, first, month, run_id)
        except MarginError as err:
            lock = _changed_month_error(con, run_id, err)
            if lock is None:
                raise
            raise lock from err
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
        "SELECT status, result_hash, input_digest FROM state.close_state WHERE month = ?",
        [month_date(month)],
    ).fetchone()
    if stored is not None and stored[0] in ("closed", "locked"):
        # A closed or locked month whose recomputed inputs or result differ is a failure, not a
        # rewrite. A locked month is never restated: the fix is an entry in the open month.
        status = stored[0]
        hint = f"; {_CORRECT_IN_OPEN_MONTH}" if status == "locked" else ""
        if status == "locked" and stored[2] != prov.month_input_digest(records):
            raise _lock_failure(
                con, month, run_id, f"locked month {month} changed: input digest differs{hint}",
                [f"{month}: stored input_digest {stored[2]}, recomputed "
                 f"{prov.month_input_digest(records)}"], results,
            )
        if stored[1] != new_hash:
            raise _lock_failure(
                con, month, run_id, f"{status} month {month} changed: result hash differs{hint}",
                [f"{month}: stored result_hash {stored[1]}, recomputed {new_hash}"], results,
            )
        lock = {"id": "CTL-LOCK", "status": "pass", "violating_rows": 0, "detail": []}
        controls.record(con, month, run_id, lock)
        results = results + [lock]
        # The month is as it was closed: CTL-PROV re-checks the records that are stored.
        verdict = prov.control(con, month, config, expected_hash=stored[1])
        controls.record(con, month, run_id, verdict)
        outcome = _checked(con, month, truth, config, records, results, verdict)  # exit 5 first
        if verdict["status"] == "fail":
            raise _fail_provenance(month, results, verdict)
        return outcome
    # No row, or an `open` one: the records are written first and checked as stored (CTL-PROV),
    # then the close controls run; a failure of either rolls the records back and leaves the month
    # open. Then it goes open -> closed with its records, in one transaction.
    verdict = None
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM prov.records WHERE month = ?", [month_date(month)])
        prov.write_records(con, records)
        prov.write_config_records(con, run_id=run_id, signoff=sign)
        verdict = prov.control(con, month, config, expected_hash=new_hash)
        outcome = _checked(con, month, truth, config, records, results, verdict)  # exit 5 first
        if verdict["status"] == "fail":
            raise _fail_provenance(month, results, verdict)
        controls.record(con, month, run_id, verdict)
        con.execute(
            "INSERT OR REPLACE INTO state.close_state VALUES (?, 'closed', ?, ?, ?, NULL)",
            [month_date(month), prov.month_input_digest(records), new_hash, run_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        if verdict is not None and verdict["status"] == "fail":
            controls.record(con, month, run_id, verdict)
        raise
    return outcome


def _fail_provenance(month: str, results: list[dict], verdict: dict) -> MarginError:
    return MarginError(
        EXIT_PROVENANCE, f"provenance check failed for {month}: {verdict['id']}",
        month=month, controls=_public(results) + _public([verdict]),
        failed_controls=[{k: verdict[k] for k in ("id", "violating_rows", "detail")}],
    )


def _checked(
    con: duckdb.DuckDBPyConnection, month: str, truth: Path | None, config, records: list[dict],
    results: list[dict], verdict: dict,
) -> dict:
    """The close outcome; with `truth`, CTL-PROV is reported and the answer-key comparison runs
    (it may exit 5 or 6). Without it, CTL-PROV is recorded in `ctl.control_results` only."""
    controls_out = _public(results)
    if truth is not None:
        checks = check_mod.evaluate(con, month, truth, config, records)
        check_mod.raise_for(month, checks, controls_out + _public([verdict]))
        controls_out = controls_out + _public([verdict]) + _public(checks)
    return {"month": month, "controls": controls_out, "result_hash": prov.result_hash(records)}
