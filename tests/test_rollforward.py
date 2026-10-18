"""`rollforward`: lock the earlier closed months and load the next month, on the committed `mini` books.

Each test works in its own temporary warehouse.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import duckdb
import jsonschema

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_TRUTH, ROOT, close_mini, margin

ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((ROOT / "src/margin/schemas/envelope.schema.json").read_text())
)
RAW_TABLES = ("accounts", "invoices", "usage_monthly", "provider_invoices", "cloud_focus",
              "support_tickets", "gl")


def rollforward(warehouse: Path, month: str, *extra: str, env: dict | None = None):
    return margin("rollforward", "--fixture", "mini", "--month", month, "--json",
                  "--config", str(MINI_CONFIG), "--warehouse", str(warehouse), *extra, env=env)


def _state(warehouse: Path) -> dict[str, tuple]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        rows = con.execute(
            "SELECT strftime(month, '%Y-%m'), status, input_digest, result_hash, closed_run_id "
            "FROM state.close_state ORDER BY month"
        ).fetchall()
    finally:
        con.close()
    return {r[0]: r[1:] for r in rows}


def _statuses(warehouse: Path) -> dict[str, str]:
    return {month: row[0] for month, row in _state(warehouse).items()}


def _raw_counts(warehouse: Path) -> dict[str, list[tuple]]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return {
            t: con.execute(
                f"SELECT strftime(_month, '%Y-%m'), count(*) FROM raw.{t} GROUP BY 1 ORDER BY 1 NULLS FIRST"
            ).fetchall()
            for t in RAW_TABLES
        }
    finally:
        con.close()


def _firm(warehouse: Path, month: str) -> dict[str, str]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return dict(con.execute(
            "SELECT account_id, reason_code FROM mart.fct_flags WHERE flag = 'firm' AND month = ?",
            [f"{month}-01"],
        ).fetchall())
    finally:
        con.close()


def _planted(month: str) -> dict[str, str]:
    planted = json.loads((MINI_TRUTH / "planted_flags.json").read_text())
    return {p["account_id"]: p["reason_code"] for p in planted[month]}


def test_rollforward_locks_march_and_april_closes_against_the_answer_key(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert close_mini(warehouse, "2026-03").code == 0
    before = _state(warehouse)

    result = rollforward(warehouse, "2026-04")
    assert result.code == 0, result.stderr
    assert result.json["month"] == "2026-04"
    assert result.json["locked"] == ["2026-02", "2026-03"]
    assert result.json["loaded"] is True
    assert "rolled forward to 2026-04: locked 2026-02, 2026-03" in result.stderr
    after = _state(warehouse)
    assert {m: s[0] for m, s in after.items()} == {
        "2026-02": "locked", "2026-03": "locked", "2026-04": "open"}
    assert {m: after[m][1:] for m in ("2026-02", "2026-03")} == {
        m: before[m][1:] for m in ("2026-02", "2026-03")}  # locking changes only the status

    checked = close_mini(warehouse, "2026-04", "--check")
    assert checked.code == 0, checked.stderr
    assert {m: s[0] for m, s in _state(warehouse).items()} == {
        "2026-02": "locked", "2026-03": "locked", "2026-04": "closed"}
    assert _firm(warehouse, "2026-04") != _firm(warehouse, "2026-03")
    assert _firm(warehouse, "2026-03") == _planted("2026-03")
    assert _firm(warehouse, "2026-04") == _planted("2026-04")


def test_a_second_rollforward_changes_nothing(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert close_mini(warehouse, "2026-03").code == 0
    assert rollforward(warehouse, "2026-04").code == 0
    state, counts = _state(warehouse), _raw_counts(warehouse)

    again = rollforward(warehouse, "2026-04")
    assert again.code == 0, again.stderr
    assert again.json["locked"] == [] and again.json["loaded"] is False
    assert _state(warehouse) == state
    assert _raw_counts(warehouse) == counts


def test_rollforward_on_a_fresh_warehouse_closes_the_earlier_months_itself(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    result = rollforward(warehouse, "2026-04")
    assert result.code == 0, result.stderr
    assert result.json["locked"] == ["2026-02", "2026-03"]
    assert _statuses(warehouse) == {"2026-02": "locked", "2026-03": "locked", "2026-04": "open"}


def test_a_month_loaded_by_an_earlier_close_is_recorded_without_a_reload(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert close_mini(warehouse, "2026-04").code == 0
    counts = _raw_counts(warehouse)
    # April is closed already: nothing is locked below it that is not closed, and nothing reloads.
    result = rollforward(warehouse, "2026-04")
    assert result.code == 0, result.stderr
    assert result.json["locked"] == ["2026-02", "2026-03"] and result.json["loaded"] is False
    assert _statuses(warehouse) == {"2026-02": "locked", "2026-03": "locked", "2026-04": "closed"}
    assert _raw_counts(warehouse) == counts


def test_a_month_outside_the_window_exits_2_naming_the_range(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    result = rollforward(warehouse, "2026-05")
    assert result.code == 2
    assert "2026-02..2026-04" in result.json["message"]
    assert not warehouse.exists()


def test_a_month_without_books_exits_4_and_writes_nothing(tmp_path, mini_books_copy):
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", tmp_path / "books.manifest.sha256")
    invoices = mini_books_copy / "invoices.csv"
    with invoices.open(newline="") as f:
        rows = list(csv.reader(f))
    keep = rows[0].index("service_period")
    with invoices.open("w", newline="") as f:
        csv.writer(f).writerows(r for i, r in enumerate(rows) if i == 0 or r[keep] != "2026-04")

    warehouse = tmp_path / "w.duckdb"
    result = rollforward(warehouse, "2026-04", "--books", str(mini_books_copy))
    assert result.code == 4
    assert result.json["message"] == "no books for month 2026-04"
    assert not warehouse.exists()  # not even the catch-up close ran

    assert close_mini(warehouse, "2026-03").code == 0
    state, counts = _state(warehouse), _raw_counts(warehouse)
    again = rollforward(warehouse, "2026-04", "--books", str(mini_books_copy))
    assert again.code == 4 and again.json["message"] == "no books for month 2026-04"
    assert _state(warehouse) == state and _raw_counts(warehouse) == counts


def test_without_a_sign_off_it_exits_4_with_the_gate_control_and_writes_nothing(tmp_path):
    empty, warehouse = tmp_path / "empty", tmp_path / "w.duckdb"
    empty.mkdir()
    result = rollforward(warehouse, "2026-04", env={"MARGIN_SIGNOFF_DIR": str(empty)})
    assert result.code == 4
    assert [c["id"] for c in result.json["failed_controls"]] == ["CTL-SIGNOFF"]
    assert not warehouse.exists()


def test_json_output_validates_against_the_envelope_schema(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    ok = rollforward(warehouse, "2026-04")
    assert ok.code == 0
    ENVELOPE.validate(ok.json)
    ENVELOPE.validate(rollforward(warehouse, "2026-05").json)
    ENVELOPE.validate(rollforward(warehouse, "2026-04", env={"MARGIN_SIGNOFF_DIR": str(tmp_path)}).json)
