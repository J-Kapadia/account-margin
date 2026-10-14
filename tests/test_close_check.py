"""`close --check`: the answer-key comparison as the last step before a month is closed.

Mismatches exit 5 (provenance gaps 6) and leave a month being closed `open`; on a month already
closed the comparison is a read and changes no state. `truth/` absent exits 4.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

import duckdb
import pytest

from conftest import MINI_CONFIG, MINI_TRUTH, close_mini, margin
from margin import check as check_mod
from margin.errors import MarginError
from margin.gen_config import load_config

ID = re.compile(r"^(cp|cpr|flag|rollup|pnl)/\d{4}-\d{2}/\S+: (differs from the answer key|no complete provenance record)$")


@pytest.fixture
def truth(tmp_path) -> Path:
    """A private copy of the `mini` answer key, to tamper with."""
    target = tmp_path / "truth"
    shutil.copytree(MINI_TRUTH, target)
    return target


def edit_csv(path: Path, change) -> None:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(change(rows) or rows)


def row_of(rows, month="2026-03", account="acct_0001"):
    return next(i for i, r in enumerate(rows) if r["month"] == month and r["account_id"] == account)


def state(warehouse: Path):
    with duckdb.connect(str(warehouse), read_only=True) as con:
        states = con.execute(
            "SELECT strftime(month, '%Y-%m'), status, result_hash, closed_run_id, input_digest "
            "FROM state.close_state ORDER BY month").fetchall()
        records = con.execute(
            "SELECT count(*), md5(string_agg(number_id || coalesce(cast(value_cents AS VARCHAR), value_text) "
            "|| input_digest || run_id, ',' ORDER BY number_id)) FROM prov.records").fetchone()
    return states, records


def check(warehouse: Path, truth_dir: Path, month: str = "2026-03"):
    return close_mini(warehouse, month, "--check", "--truth", str(truth_dir))


def failures(result) -> dict[str, list[str]]:
    return {f["id"]: f["detail"] for f in result.json["failed_controls"]}


# ------------------------------------------------------------------ the happy path


def test_check_passes_on_the_clean_fixture_and_the_month_closes(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    result = check(warehouse, MINI_TRUTH)
    assert result.code == 0, result.stderr
    ids = [c["id"] for c in result.json["controls"]]
    assert ids[0] == "CTL-SIGNOFF" and ids[-5:] == list(check_mod.ORDER)
    assert all(c["status"] == "pass" for c in result.json["controls"])
    assert [(m, s) for m, s, *_ in state(warehouse)[0]] == [("2026-02", "closed"), ("2026-03", "closed")]


def test_running_check_twice_gives_byte_identical_results(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    first, second = check(warehouse, MINI_TRUTH), check(warehouse, MINI_TRUTH)
    assert first.code == second.code == 0
    assert first.json["result_hash"] == second.json["result_hash"]


def test_check_does_not_change_the_result_hash(tmp_path):
    plain = close_mini(tmp_path / "a.duckdb", "2026-03")
    checked = check(tmp_path / "b.duckdb", MINI_TRUTH)
    assert plain.json["result_hash"] == checked.json["result_hash"]


# ------------------------------------------------------------------ truth/ absent


def test_check_without_truth_exits_4_and_plain_close_does_not_need_it(tmp_path):
    missing = tmp_path / "no-truth"
    warehouse = tmp_path / "w.duckdb"
    result = check(warehouse, missing)
    assert result.code == 4
    assert result.json["message"] == "truth/ not found: --check needs the answer key"
    assert not warehouse.exists()  # nothing was read or written
    plain = close_mini(warehouse, "2026-03", "--truth", str(missing))
    assert plain.code == 0, plain.stderr


def test_an_incomplete_answer_key_exits_4(tmp_path, truth):
    (truth / "rule_flags.csv").unlink()
    result = check(tmp_path / "w.duckdb", truth)
    assert result.code == 4 and "missing rule_flags.csv" in result.json["message"]


# ------------------------------------------------------------------ mismatches leave the month open


def test_a_component_off_by_one_cent_exits_5_and_the_month_stays_open(tmp_path, truth):
    edit_csv(truth / "cost_components.csv",
             lambda rows: rows[row_of(rows)].update(support_cost_cents=str(int(rows[row_of(rows)]["support_cost_cents"]) + 1)))
    warehouse = tmp_path / "w.duckdb"
    result = check(warehouse, truth)
    assert result.code == 5
    assert failures(result)["CHK-COMPONENTS"] == [
        "cp/2026-03/acct_0001/support_cost: differs from the answer key"]
    assert result.json["month"] == "2026-03"
    # only the catch-up month closed; the target month has no closed row
    assert [(m, s) for m, s, *_ in state(warehouse)[0]] == [("2026-02", "closed")]
    with duckdb.connect(str(warehouse), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM prov.records WHERE month = DATE '2026-03-01'").fetchone()[0] == 0


def test_the_open_month_closes_once_the_key_agrees(tmp_path, truth):
    edit_csv(truth / "cost_components.csv",
             lambda rows: rows[row_of(rows)].update(revenue_cents="1"))
    warehouse = tmp_path / "w.duckdb"
    assert check(warehouse, truth).code == 5
    assert check(warehouse, MINI_TRUTH).code == 0
    assert [(m, s) for m, s, *_ in state(warehouse)[0]] == [("2026-02", "closed"), ("2026-03", "closed")]


def test_a_failing_check_on_a_closed_month_leaves_its_state_unchanged(mini_closed_copy, truth):
    before = state(mini_closed_copy)
    edit_csv(truth / "cost_components.csv",
             lambda rows: rows[row_of(rows)].update(revenue_cents="1"))
    result = check(mini_closed_copy, truth)
    assert result.code == 5 and "CHK-COMPONENTS" in failures(result)
    assert state(mini_closed_copy) == before  # rows, hashes, digests and provenance records alike
    assert check(mini_closed_copy, MINI_TRUTH).code == 0  # and a passing re-run is a no-op too
    assert state(mini_closed_copy) == before


def test_a_failing_check_on_a_locked_month_changes_no_state(mini_closed_copy, truth):
    with duckdb.connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'locked' WHERE month = DATE '2026-03-01'")
    before = state(mini_closed_copy)
    edit_csv(truth / "cost_components.csv", lambda rows: rows[row_of(rows)].update(revenue_cents="1"))
    assert check(mini_closed_copy, truth).code == 5
    assert state(mini_closed_copy) == before
    assert check(mini_closed_copy, MINI_TRUTH).code == 0
    assert state(mini_closed_copy) == before


def test_only_the_target_month_is_checked_not_the_catch_up_months(tmp_path, truth):
    edit_csv(truth / "cost_components.csv",
             lambda rows: rows[row_of(rows, "2026-02")].update(revenue_cents="1"))
    result = check(tmp_path / "w.duckdb", truth)
    assert result.code == 0, result.stderr


def test_a_missing_planted_account_and_a_wrong_reason_are_named(tmp_path, truth):
    path = truth / "planted_flags.json"
    planted = json.loads(path.read_text())
    planted["2026-03"][0]["reason_code"] = "SUPPORT_LOAD" if planted["2026-03"][0]["reason_code"] != "SUPPORT_LOAD" else "MODEL_MIX"
    wrong_reason = planted["2026-03"][0]["account_id"]
    dropped = planted["2026-03"].pop()["account_id"]
    path.write_text(json.dumps(planted))
    result = check(tmp_path / "w.duckdb", truth)
    assert result.code == 5
    found = failures(result)
    assert found["CHK-FLAGS"] == [f"flag/2026-03/{dropped}: differs from the answer key"]
    assert found["CHK-REASONS"] == [f"flag/2026-03/{wrong_reason}: differs from the answer key"]


def test_a_soft_flag_that_is_not_a_decoy_is_named(tmp_path, truth):
    path = truth / "planted_flags.json"
    planted = json.loads(path.read_text())
    planted["decoys"]["2026-03"] = []
    path.write_text(json.dumps(planted))
    found = failures(check(tmp_path / "w.duckdb", truth))
    assert found["CHK-FLAGS"] == ["flag/2026-03/acct_0009: differs from the answer key"]


def test_a_wrong_per_rule_result_and_status_change_count_are_named(tmp_path, truth):
    config = load_config(MINI_CONFIG)
    other = next(r for r in config.allocation.rules if r != config.allocation.recommended)
    edit_csv(truth / "rule_flags.csv", lambda rows: rows[row_of(rows, account="acct_0002")].update(
        {f"below_{other}": "true"}))
    result = check(tmp_path / "w.duckdb", truth)
    assert result.code == 5
    found = failures(result)["CHK-RULE-FLAGS"]
    assert f"cpr/2026-03/acct_0002/{other}: differs from the answer key" in found
    assert f"rollup/2026-03/accounts_status_change_{other}: differs from the answer key" in found


def test_a_failure_prints_identifiers_only(tmp_path, truth):
    edit_csv(truth / "cost_components.csv",
             lambda rows: rows[row_of(rows)].update(revenue_cents="123456789", inference_cost_cents="987654321"))
    result = check(tmp_path / "w.duckdb", truth)
    assert result.code == 5
    assert all(ID.match(d) for ds in failures(result).values() for d in ds)
    assert "123456789" not in result.stdout + result.stderr and "987654321" not in result.stdout + result.stderr


# ------------------------------------------------------------------ provenance (exit 6)


@pytest.fixture
def records_and_con(mini_closed):
    con = duckdb.connect(str(mini_closed), read_only=True)
    cursor = con.execute("SELECT * FROM prov.records WHERE month = DATE '2026-03-01'")
    cols = [d[0] for d in cursor.description]
    records = [dict(zip(cols, r)) for r in cursor.fetchall()]
    yield con, records
    con.close()


def test_a_complete_record_set_has_no_gaps(records_and_con):
    con, records = records_and_con
    assert check_mod.provenance_gaps(con, "2026-03", records, load_config(MINI_CONFIG)) == []


def test_a_missing_record_and_a_record_without_a_digest_are_gaps(records_and_con):
    con, records = records_and_con
    records = [r for r in records if r["number_id"] != "cp/2026-03/acct_0001/revenue"]
    records = [{**r, "input_digest": ""} if r["number_id"] == "pnl/2026-03/5300" else r for r in records]
    records = [{**r, "source_query": None} if r["number_id"] == "flag/2026-03/acct_0006" else r for r in records]
    assert check_mod.provenance_gaps(con, "2026-03", records, load_config(MINI_CONFIG)) == [
        "cp/2026-03/acct_0001/revenue", "flag/2026-03/acct_0006", "pnl/2026-03/5300"]


def test_a_provenance_gap_alone_exits_6_and_a_key_mismatch_takes_precedence():
    gap = {"id": check_mod.CHK_PROVENANCE, "status": "fail", "violating_rows": 1,
           "detail": ["cp/2026-03/acct_0001/revenue: no complete provenance record"]}
    wrong = {"id": check_mod.CHK_COMPONENTS, "status": "fail", "violating_rows": 1,
             "detail": ["cp/2026-03/acct_0001/revenue: differs from the answer key"]}
    ok = {"id": check_mod.CHK_FLAGS, "status": "pass", "violating_rows": 0, "detail": []}
    with pytest.raises(MarginError) as err:
        check_mod.raise_for("2026-03", [ok, gap], [])
    assert err.value.exit_code == 6 and err.value.failed_controls[0]["id"] == "CHK-PROVENANCE"
    with pytest.raises(MarginError) as err:
        check_mod.raise_for("2026-03", [wrong, gap], [])
    assert err.value.exit_code == 5 and [f["id"] for f in err.value.failed_controls] == ["CHK-COMPONENTS", "CHK-PROVENANCE"]
    check_mod.raise_for("2026-03", [ok], [])  # nothing to raise


def test_the_dbt_project_defaults_match_the_fixture_config():
    """The definition of done runs plain `dbt test`, which reads these defaults, not --vars."""
    import yaml

    from conftest import ROOT
    from margin import dbt_runner

    defaults = yaml.safe_load((ROOT / "dbt" / "dbt_project.yml").read_text())["vars"]
    assert defaults == dbt_runner.dbt_vars(MINI_CONFIG)
