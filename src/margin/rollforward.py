"""`rollforward`: lock the earlier closed months and load the next month's actuals.

The month before the target is closed first (the ordinary `close`, with its catch-up) when it is
not closed already; that is a separate, individually committed step. Then one transaction in this
process marks every earlier `closed` month `locked`, loads the target month's books unless they are
already loaded, and records the month as `open` unless it has a row. dbt never runs inside or
between those writes, and the month is not closed here. Every part is skipped when it is already
true, so a repeat changes nothing.

`MARGIN_PAUSE_BEFORE_COMMIT` is a hook for the interruption test only. When it names a path, the
transaction creates that file once the month is loaded and recorded but before it commits, then
waits (polling every 50 ms, at most 30 s) until the file is removed. A test that kills the process
while it waits gets a reproducible crash inside the transaction.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import duckdb

from . import close as close_mod
from . import load, signoff
from .books import check_in_window, months_between
from .errors import EXIT_PRECONDITION, MarginError
from .paths import Paths
from .warehouse import connect, init_schemas, month_date


def _is_closed(paths: Paths, month: str) -> bool:
    con = connect(paths.warehouse)
    try:
        init_schemas(con)
        row = con.execute(
            "SELECT status FROM state.close_state WHERE month = ?", [month_date(month)]
        ).fetchone()
    finally:
        con.close()
    return row is not None and row[0] in ("closed", "locked")


def _is_loaded(con: duckdb.DuckDBPyConnection, month: str) -> bool:
    table = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'raw' AND table_name = 'invoices'"
    ).fetchone()[0]
    if not table:
        return False
    return con.execute(
        "SELECT count(*) FROM raw.invoices WHERE _month = ?", [month_date(month)]
    ).fetchone()[0] > 0


PAUSE_ENV = "MARGIN_PAUSE_BEFORE_COMMIT"
PAUSE_POLL_SECONDS = 0.05
PAUSE_LIMIT_SECONDS = 30.0


def _pause_before_commit() -> None:
    """Test hook: create the marker file named by `MARGIN_PAUSE_BEFORE_COMMIT` and wait for its removal."""
    target = os.environ.get(PAUSE_ENV)
    if not target:
        return
    marker = Path(target)
    marker.touch()
    deadline = time.monotonic() + PAUSE_LIMIT_SECONDS
    while marker.exists() and time.monotonic() < deadline:
        time.sleep(PAUSE_POLL_SECONDS)


def rollforward_month(paths: Paths, month: str) -> dict:
    """Roll the warehouse forward to `month`; return the envelope fields.

    `locked` lists the months this call newly locked and `loaded` says whether it loaded the
    month's books (False when they were already there).
    """
    first, _ = check_in_window(paths.books, month)
    signoff.verify(paths.config, month=month)  # exit 4 before anything is read or written
    if not load.books_have_rows(paths.books, month):
        # Checked before the catch-up close, so a month without books leaves the warehouse alone.
        raise MarginError(EXIT_PRECONDITION, f"no books for month {month}", month=month)

    earlier = months_between(first, month)[:-1]
    if earlier and not _is_closed(paths, earlier[-1]):
        close_mod.close_month(paths, earlier[-1])

    run_id = uuid.uuid4().hex
    con = connect(paths.warehouse)
    try:
        init_schemas(con)
        load.check_books_identity(con, paths.books)
        con.execute("BEGIN")
        try:
            rows = con.execute(
                "UPDATE state.close_state SET status = 'locked' "
                "WHERE status = 'closed' AND month < ? RETURNING month",
                [month_date(month)],
            ).fetchall()
            loaded = not _is_loaded(con, month)
            load.load_months(con, paths.books, month, month, run_id)
            con.execute(
                "INSERT INTO state.close_state (month, status) VALUES (?, 'open') "
                "ON CONFLICT DO NOTHING",
                [month_date(month)],
            )
            _pause_before_commit()
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    locked = sorted(str(r[0])[:7] for r in rows)
    return {"month": month, "locked": locked, "loaded": loaded}
