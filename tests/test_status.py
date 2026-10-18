"""`status` and `close --recompute`, on the committed `mini` books.

Each test that changes the warehouse works on a private copy of the closed one.
"""

from __future__ import annotations

import json

import jsonschema
import duckdb

from conftest import MINI_CONFIG, ROOT, close_mini, margin
from test_lock import hold_exclusive
from test_rollforward import rollforward

ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((ROOT / "src/margin/schemas/envelope.schema.json").read_text())
)


def status(warehouse, *extra):
    return margin("status", "--fixture", "mini", "--warehouse", str(warehouse), "--json", *extra)


def recompute(warehouse, month, *extra):
    return close_mini(warehouse, month, "--recompute", *extra)


def _hashes(warehouse):
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return dict(con.execute(
            "SELECT strftime(month, '%Y-%m'), result_hash FROM state.close_state"
        ).fetchall())
    finally:
        con.close()


def test_status_lists_the_three_closed_months_with_their_hashes(mini_closed):
    result = status(mini_closed)
    assert result.code == 0, result.stderr
    ENVELOPE.validate(result.json)
    months = result.json["months"]
    assert [(m["month"], m["status"]) for m in months] == [
        ("2026-02", "closed"), ("2026-03", "closed"), ("2026-04", "closed")]
    assert {m["month"]: m["result_hash"] for m in months} == _hashes(mini_closed)
    assert all(m["restated_from"] is None for m in months)
    human = margin("status", "--fixture", "mini", "--warehouse", str(mini_closed))
    assert f"2026-03 closed {_hashes(mini_closed)['2026-03']}" in human.stdout


def test_status_after_a_rollforward_shows_two_locked_and_one_open(tmp_path):
    fresh = tmp_path / "roll.duckdb"
    assert close_mini(fresh, "2026-03").code == 0
    assert rollforward(fresh, "2026-04").code == 0
    result = status(fresh)
    assert result.code == 0, result.stderr
    ENVELOPE.validate(result.json)
    months = result.json["months"]
    assert [(m["month"], m["status"]) for m in months] == [
        ("2026-02", "locked"), ("2026-03", "locked"), ("2026-04", "open")]
    assert months[2]["result_hash"] is None
    assert months[0]["result_hash"] == _hashes(fresh)["2026-02"]


def test_status_on_an_empty_warehouse_lists_no_months(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    con = duckdb.connect(str(warehouse))
    from margin.warehouse import init_schemas
    init_schemas(con)
    con.close()
    result = status(warehouse)
    assert result.code == 0, result.stderr
    assert result.json["months"] == []


def test_status_on_a_missing_warehouse_exits_4_and_writes_nothing(tmp_path):
    under_missing_dir = tmp_path / "nope" / "x.duckdb"
    result = status(under_missing_dir)
    assert result.code == 4, result.stderr
    assert not under_missing_dir.parent.exists()
    fresh = tmp_path / "fresh.duckdb"
    result = status(fresh)
    assert result.code == 4 and str(fresh) in result.json["message"]
    assert list(tmp_path.iterdir()) == []


def test_status_exits_70_while_another_process_holds_the_warehouse(mini_closed_copy):
    holder = hold_exclusive(str(mini_closed_copy) + ".lock")
    try:
        result = status(mini_closed_copy)
        assert result.code == 70
        assert result.json["message"] == "another margin process holds the warehouse"
    finally:
        holder.kill()
        holder.wait()


def test_recompute_on_a_closed_month_with_nothing_changed_restates_to_the_same_hash(mini_closed_copy):
    before = _hashes(mini_closed_copy)
    result = recompute(mini_closed_copy, "2026-03")
    assert result.code == 0, result.stderr
    ENVELOPE.validate(result.json)
    assert result.json["restated_from"] == before["2026-03"]
    assert result.json["result_hash"] == before["2026-03"]
    assert _hashes(mini_closed_copy) == before
    listed = {m["month"]: m for m in status(mini_closed_copy).json["months"]}
    assert listed["2026-03"]["restated_from"] == before["2026-03"]
    assert listed["2026-04"]["restated_from"] is None


def test_recompute_with_check_compares_the_new_result_with_the_answer_key(mini_closed_copy):
    result = recompute(mini_closed_copy, "2026-04", "--check")
    assert result.code == 0, result.stderr
    assert any(c["id"].startswith("CHK-") for c in result.json["controls"])


def test_recompute_on_a_locked_month_exits_4_and_changes_nothing(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert close_mini(warehouse, "2026-03").code == 0
    assert rollforward(warehouse, "2026-04").code == 0
    before = _hashes(warehouse)
    result = recompute(warehouse, "2026-03")
    assert result.code == 4
    assert result.json["message"] == "month 2026-03 is locked: --recompute applies to a closed month"
    assert _hashes(warehouse) == before
    assert {m["month"]: m["restated_from"] for m in status(warehouse).json["months"]} == {
        "2026-02": None, "2026-03": None, "2026-04": None}


def test_recompute_on_an_open_month_or_a_missing_row_exits_4(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert close_mini(warehouse, "2026-03").code == 0
    assert rollforward(warehouse, "2026-04").code == 0
    result = recompute(warehouse, "2026-04")  # open: rolled forward, not closed
    assert result.code == 4 and result.json["message"] == "month 2026-04 not closed"
    assert recompute(tmp_path / "none.duckdb", "2026-03").code == 4
    assert not (tmp_path / "none.duckdb").exists()
