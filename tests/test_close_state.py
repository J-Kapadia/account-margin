"""The month state machine, one test per row of the transition table: `close M` with catch-up, a
no-op on a closed and on a locked month, `open -> closed`, a failing earlier month, a changed
closed month, `--check` failing on a closed month, `rollforward` on a month a close already loaded,
`close --recompute`, and the read commands on a closed, a locked and an open month.
"""

from __future__ import annotations

import csv
import shutil

from conftest import connect
import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_TRUTH, close_mini, margin

MONTHS = ["2026-02", "2026-03", "2026-04"]


def _states(warehouse):
    with connect(str(warehouse), read_only=True) as con:
        return con.execute(
            "SELECT strftime(month, '%Y-%m'), status, result_hash, closed_run_id FROM state.close_state ORDER BY month"
        ).fetchall()


def _books_copy(tmp_path):
    """A private copy of the `mini` books and its manifest, to tamper with."""
    books = tmp_path / "b" / "books"
    shutil.copytree(MINI_BOOKS, books)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", books.parent / "books.manifest.sha256")
    return books


def _rewrite(path, keep):
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(keep(rows))


def _close_books(books, warehouse, month):
    return margin("close", "--fixture", "mini", "--books", str(books), "--warehouse", str(warehouse),
                  "--config", str(MINI_CONFIG), "--month", month, "--json")


def _first_gl_row(rows, period, code):
    return next(i for i, r in enumerate(rows) if r["gl_period"] == period and r["gl_code"] == code)


def test_close_on_a_fresh_warehouse_closes_every_earlier_month_in_order(mini_closed):
    assert [(m, s) for m, s, *_ in _states(mini_closed)] == [(m, "closed") for m in MONTHS]
    with connect(str(mini_closed), read_only=True) as con:
        kinds = {k for (k,) in con.execute("SELECT DISTINCT producer_kind FROM prov.records").fetchall()}
        months = [m for (m,) in con.execute(
            "SELECT strftime(month, '%Y-%m') FROM prov.records WHERE month IS NOT NULL GROUP BY month ORDER BY month"
        ).fetchall()]
    assert kinds == {"workflow"}
    assert months == MONTHS


@pytest.mark.slow
def test_close_on_a_closed_month_is_a_no_op_with_the_same_hash(mini_closed_copy):
    before = _states(mini_closed_copy)
    result = close_mini(mini_closed_copy, "2026-03")
    assert result.code == 0, result.stderr
    assert result.json["result_hash"] == dict((m, h) for m, _, h, _ in before)["2026-03"]
    assert _states(mini_closed_copy) == before  # no row rewritten, the closing run is the same


def test_an_open_month_closes_and_gets_the_same_result(mini_closed_copy):
    before = _states(mini_closed_copy)
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open', result_hash = NULL WHERE month = DATE '2026-03-01'")
    result = close_mini(mini_closed_copy, "2026-03")
    assert result.code == 0, result.stderr
    after = _states(mini_closed_copy)
    assert [(m, s, h) for m, s, h, _ in after] == [(m, s, h) for m, s, h, _ in before]
    assert dict((m, r) for m, _, _, r in after)["2026-03"] != dict((m, r) for m, _, _, r in before)["2026-03"]


@pytest.mark.slow
def test_a_failing_earlier_month_stops_the_catch_up_and_names_it(tmp_path):
    books = _books_copy(tmp_path)
    def slip(rows):
        k = _first_gl_row(rows, "2026-02", "4000")
        rows[k] = {**rows[k], "amount_cents": str(int(rows[k]["amount_cents"]) + 1)}
        return rows

    _rewrite(books / "gl.csv", slip)
    warehouse = tmp_path / "w.duckdb"
    result = _close_books(books, warehouse, "2026-04")
    assert result.code == 3
    assert "2026-02" in result.json["message"] and result.json["month"] == "2026-02"
    assert result.json["failed_controls"][0]["id"] == "CTL-TIE-REV"
    assert _states(warehouse) == []  # nothing closed, so the later months never ran


def test_a_closed_month_whose_books_changed_exits_3_and_keeps_its_state(mini_closed_copy, tmp_path):
    books = _books_copy(tmp_path)
    _rewrite(books / "gl.csv", lambda rows: [r for i, r in enumerate(rows) if i != _first_gl_row(rows, "2026-02", "4000")])
    before = _states(mini_closed_copy)
    result = _close_books(books, mini_closed_copy, "2026-03")
    assert result.code == 3 and "closed month 2026-02 changed" in result.json["message"]
    assert result.json["failed_controls"][0]["id"] == "CTL-LOCK"
    assert _states(mini_closed_copy) == before


def test_a_month_without_books_exits_4_and_writes_nothing(tmp_path):
    books = _books_copy(tmp_path)
    _rewrite(books / "invoices.csv", lambda rows: [r for r in rows if r["service_period"] != "2026-04"])
    warehouse = tmp_path / "w.duckdb"
    result = _close_books(books, warehouse, "2026-04")
    assert result.code == 4 and result.json["message"] == "no books for month 2026-04"
    assert not warehouse.exists()


def test_a_gap_in_the_catch_up_names_the_first_month_without_books(tmp_path):
    books = _books_copy(tmp_path)
    _rewrite(books / "invoices.csv", lambda rows: [r for r in rows if r["service_period"] != "2026-03"])
    warehouse = tmp_path / "w.duckdb"
    result = _close_books(books, warehouse, "2026-04")
    assert result.code == 4 and result.json["message"] == "no books for month 2026-03"
    assert not warehouse.exists()


def test_the_read_commands_accept_a_closed_month_and_refuse_an_open_one(mini_closed_copy, tmp_path):
    flags = ["--fixture", "mini", "--warehouse", str(mini_closed_copy), "--json"]
    assert margin("explain", "rollup/2026-04/revenue", *flags).code == 0
    assert margin("memo", "--month", "2026-04", "--out", str(tmp_path / "s"), *flags).code == 0
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = DATE '2026-04-01'")
    for args in (("explain", "rollup/2026-04/revenue"), ("memo", "--month", "2026-04", "--out", str(tmp_path / "t"))):
        result = margin(*args, *flags)
        assert result.code == 4 and "not closed" in result.json["message"]
    assert _states(mini_closed_copy)[-1][1] == "open"  # a read changes no state
    with connect(str(mini_closed_copy)) as con:
        con.execute("DELETE FROM state.close_state WHERE month = DATE '2026-04-01'")
    assert margin("explain", "rollup/2026-04/revenue", *flags).code == 4  # no row is unclosed too


def _lock_all(warehouse):
    with connect(str(warehouse)) as con:
        con.execute("UPDATE state.close_state SET status = 'locked' WHERE month < DATE '2026-04-01'")


def test_close_on_a_locked_month_is_a_no_op_with_the_same_hash(mini_closed_copy):
    _lock_all(mini_closed_copy)
    before = _states(mini_closed_copy)
    result = close_mini(mini_closed_copy, "2026-03")
    assert result.code == 0, result.stderr
    assert result.json["result_hash"] == dict((m, h) for m, _, h, _ in before)["2026-03"]
    assert _states(mini_closed_copy) == before  # still locked, same hash, same closing run


def test_rollforward_on_a_month_loaded_by_catch_up_adds_no_rows(mini_closed_copy):
    def counts():
        with connect(str(mini_closed_copy), read_only=True) as con:
            return [con.execute(f"SELECT count(*) FROM raw.{t}").fetchone()[0]
                    for t in ("invoices", "usage_monthly", "gl")]

    before = counts()
    result = margin("rollforward", "--fixture", "mini", "--month", "2026-04", "--json",
                    "--config", str(MINI_CONFIG), "--warehouse", str(mini_closed_copy))
    assert result.code == 0, result.stderr
    assert result.json["loaded"] is False
    assert counts() == before
    assert [(m, s) for m, s, *_ in _states(mini_closed_copy)] == [
        ("2026-02", "locked"), ("2026-03", "locked"), ("2026-04", "closed")]


def test_check_failing_on_a_closed_month_leaves_the_state_unchanged(mini_closed_copy, tmp_path):
    truth = tmp_path / "truth"
    shutil.copytree(MINI_TRUTH, truth)
    _rewrite(truth / "cost_components.csv", lambda rows: [{**rows[0], "revenue_cents": "1"}, *rows[1:]])
    before = _states(mini_closed_copy)
    result = close_mini(mini_closed_copy, rows_month(truth), "--check", "--truth", str(truth))
    assert result.code == 5, result.stderr
    assert _states(mini_closed_copy) == before


def rows_month(truth):
    with (truth / "cost_components.csv").open(encoding="utf-8", newline="") as fh:
        return next(csv.DictReader(fh))["month"]


def test_recompute_row_closed_restates_and_locked_or_open_exits_4(mini_closed_copy):
    before = _states(mini_closed_copy)
    result = close_mini(mini_closed_copy, "2026-03", "--recompute")
    assert result.code == 0, result.stderr
    assert result.json["restated_from"] == result.json["result_hash"] == dict((m, h) for m, _, h, _ in before)["2026-03"]
    _lock_all(mini_closed_copy)
    assert close_mini(mini_closed_copy, "2026-03", "--recompute").code == 4
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = DATE '2026-04-01'")
    assert close_mini(mini_closed_copy, "2026-04", "--recompute").code == 4


def test_the_read_commands_accept_a_locked_month_and_explain_and_memo_refuse_an_open_one(mini_closed_copy, tmp_path):
    flags = ["--fixture", "mini", "--warehouse", str(mini_closed_copy), "--json"]
    _lock_all(mini_closed_copy)
    before = _states(mini_closed_copy)
    assert margin("explain", "rollup/2026-03/revenue", *flags).code == 0
    assert margin("memo", "--month", "2026-03", "--out", str(tmp_path / "s"), *flags).code == 0
    assert margin("status", *flags).code == 0
    assert _states(mini_closed_copy) == before
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open', result_hash = NULL WHERE month = DATE '2026-04-01'")
    for args in (("explain", "rollup/2026-04/revenue"), ("memo", "--month", "2026-04", "--out", str(tmp_path / "t"))):
        result = margin(*args, *flags)
        assert result.code == 4 and result.json["message"] == "month 2026-04 not closed"
    assert margin("status", *flags).code == 0  # status lists the open month rather than refusing it
