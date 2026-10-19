"""CTL-LOCK: a closed or locked month whose books or results changed fails the close with exit 3,
names the table and month, and leaves `state.close_state` as it was. A re-close of an unchanged
locked month passes the control. Each test works on its own copy of a shared warehouse on `mini`.
"""

from __future__ import annotations

import csv
import shutil

from conftest import MINI_BOOKS, MINI_CONFIG, close_mini, connect, margin


def _states(warehouse):
    with connect(str(warehouse), read_only=True) as con:
        return con.execute(
            "SELECT strftime(month, '%Y-%m'), status, input_digest, result_hash, closed_run_id "
            "FROM state.close_state ORDER BY month"
        ).fetchall()


def _books_copy(tmp_path):
    """A private copy of the `mini` books and its manifest, to tamper with."""
    books = tmp_path / "b" / "books"
    shutil.copytree(MINI_BOOKS, books)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", books.parent / "books.manifest.sha256")
    return books


def _bump_gl_row(books, period, code):
    path = books / "gl.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    k = next(i for i, r in enumerate(rows) if r["gl_period"] == period and r["gl_code"] == code)
    rows[k] = {**rows[k], "amount_cents": str(int(rows[k]["amount_cents"]) + 1)}
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _close_books(books, warehouse, month, *extra):
    return margin("close", "--fixture", "mini", "--books", str(books), "--warehouse", str(warehouse),
                  "--config", str(MINI_CONFIG), "--month", month, "--json", *extra)


def _lock_rows(warehouse):
    with connect(str(warehouse), read_only=True) as con:
        return con.execute(
            "SELECT strftime(month, '%Y-%m'), status FROM ctl.control_results "
            "WHERE control_id = 'CTL-LOCK' ORDER BY month, status"
        ).fetchall()


# `mini_rolled_forward_copy` is March closed, then rolled forward to April: February and March are
# locked, April is open.


def test_a_locked_month_whose_books_changed_fails_ctl_lock_and_keeps_its_state(mini_rolled_forward_copy, tmp_path):
    warehouse = mini_rolled_forward_copy
    assert {m: s for m, s, *_ in _states(warehouse)} == {
        "2026-02": "locked", "2026-03": "locked", "2026-04": "open"}
    before = _states(warehouse)
    books = _books_copy(tmp_path)
    _bump_gl_row(books, "2026-03", "4000")

    result = _close_books(books, warehouse, "2026-04")

    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CTL-LOCK"
    assert "raw.gl" in result.json["failed_controls"][0]["detail"][0]
    assert "2026-03" in result.json["failed_controls"][0]["detail"][0]
    assert "locked month 2026-03 changed" in result.json["message"]
    assert "changed: gl;" in result.json["message"] and "entry in the open month" in result.json["message"]
    assert result.json["month"] == "2026-03"
    assert _states(warehouse) == before


def test_a_closed_month_whose_books_changed_fails_ctl_lock_and_keeps_its_state(mini_closed_copy, tmp_path):
    warehouse = mini_closed_copy  # every month of `mini` closed, as a close of April leaves it
    before = _states(warehouse)
    books = _books_copy(tmp_path)
    _bump_gl_row(books, "2026-04", "4000")

    result = _close_books(books, warehouse, "2026-04")

    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CTL-LOCK"
    assert "closed month 2026-04 changed" in result.json["message"]
    assert _states(warehouse) == before


def test_a_re_close_of_a_locked_month_is_a_no_op_that_passes_ctl_lock(mini_rolled_forward_copy):
    warehouse = mini_rolled_forward_copy
    before = _states(warehouse)

    result = close_mini(warehouse, "2026-03")

    assert result.code == 0, result.stderr
    assert result.json["result_hash"] == {m: h for m, _, _, h, _ in before}["2026-03"]
    assert {"id": "CTL-LOCK", "status": "pass", "violating_rows": 0} in result.json["controls"]
    assert _states(warehouse) == before
    assert ("2026-03", "pass") in _lock_rows(warehouse)


def test_a_failed_ctl_lock_is_recorded_in_the_control_results(mini_rolled_forward_copy, tmp_path):
    warehouse = mini_rolled_forward_copy
    books = _books_copy(tmp_path)
    _bump_gl_row(books, "2026-03", "4000")
    assert _close_books(books, warehouse, "2026-04").code == 3
    assert ("2026-03", "fail") in _lock_rows(warehouse)


def test_a_changed_input_digest_of_a_locked_month_fails_ctl_lock(mini_rolled_forward_copy):
    warehouse = mini_rolled_forward_copy
    with connect(str(warehouse)) as con:
        con.execute("UPDATE state.close_state SET input_digest = 'tampered' WHERE month = DATE '2026-03-01'")
    before = _states(warehouse)

    result = close_mini(warehouse, "2026-03")

    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CTL-LOCK"
    assert "locked month 2026-03 changed" in result.json["message"]
    assert _states(warehouse) == before


def test_check_on_a_locked_month_exits_0_and_changes_no_state(mini_rolled_forward_copy):
    warehouse = mini_rolled_forward_copy
    before = _states(warehouse)

    result = close_mini(warehouse, "2026-03", "--check")

    assert result.code == 0, result.stderr
    assert _states(warehouse) == before
