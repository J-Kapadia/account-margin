"""CTL-PROV and digest re-derivation over the stored records of the closed `mini` fixture.

A record's digest re-derives from its source rows (a leaf) or its children (a composite); a
tampered digest, value or query, a missing record and an ID outside the closed grammar each fail
the control, and a failing close exits 6 and leaves the month as it was.
"""

from __future__ import annotations

import csv
import json
import shutil

from conftest import connect
import pytest

from conftest import FIXTURE_SIGNOFF, MINI_CONFIG, MINI_TRUTH, close_mini
from margin import close as close_mod, prov
from margin.errors import MarginError
from margin.gen_config import load_config
from margin.paths import resolve
from margin.warehouse import init_schemas

MONTHS = ("2026-02", "2026-03", "2026-04")
LEAF = "cp/2026-04/acct_0001/revenue"
COMPOSITE = "rollup/2026-04/revenue"


@pytest.fixture(scope="module")
def con(mini_closed):
    c = connect(str(mini_closed), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def config():
    return load_config(MINI_CONFIG)


def sql(warehouse, statement, *args):
    with connect(str(warehouse)) as c:
        c.execute(statement, list(args))


def snapshot(warehouse):
    with connect(str(warehouse), read_only=True) as c:
        return (
            c.execute("SELECT * FROM state.close_state ORDER BY month").fetchall(),
            c.execute("SELECT number_id, value_cents, value_text, input_digest FROM prov.records "
                      "ORDER BY number_id").fetchall(),
        )


def failed(result) -> dict[str, list[str]]:
    return {f["id"]: f["detail"] for f in result.json["failed_controls"]}


# ------------------------------------------------------------------ re-derivation


def test_every_stored_record_rederives(con):
    ids = [r for (r,) in con.execute("SELECT number_id FROM prov.records").fetchall()]
    assert len(ids) > 500
    for number_id in ids:
        rec = prov.fetch_record(con, number_id)
        digest, inputs, count = prov.rederive(con, rec, lambda i: prov.fetch_record(con, i))
        assert digest == rec["input_digest"], number_id
        assert count == rec["input_row_count"] == len(inputs), number_id


def test_a_leaf_rederives_from_rows_and_a_composite_from_its_children(con):
    leaf = prov.fetch_record(con, LEAF)
    assert not prov.is_composite(leaf["source_query"])
    _, rows, _ = prov.rederive(con, leaf, lambda i: None)
    assert rows and all(len(row) == 3 for row in rows)
    composite = prov.fetch_record(con, COMPOSITE)
    assert prov.is_composite(composite["source_query"])
    _, children, _ = prov.rederive(con, composite, lambda i: prov.fetch_record(con, i))
    assert [c["number_id"] for c in children] == json.loads(composite["source_query"])


def test_a_composite_with_a_missing_child_is_a_provenance_error(con):
    rec = {**prov.fetch_record(con, COMPOSITE), "source_query": json.dumps(["cp/2026-04/nobody/revenue"])}
    with pytest.raises(MarginError) as err:
        prov.rederive(con, rec, lambda i: None)
    assert err.value.exit_code == 6 and "cp/2026-04/nobody/revenue" in err.value.message


def test_a_source_query_must_be_one_select(con):
    for query in ("SELECT 1; SELECT 2", "DROP TABLE raw.gl", "SELEC nonsense"):
        with pytest.raises(MarginError) as err:
            prov.run_source_query(con, query)
        assert err.value.exit_code == 6


def test_the_control_passes_on_every_closed_month(con, config):
    for month in MONTHS:
        (stored,) = con.execute(
            "SELECT result_hash FROM state.close_state WHERE month = ?", [f"{month}-01"]).fetchone()
        verdict = prov.control(con, month, config, expected_hash=stored)
        assert verdict == {"id": "CTL-PROV", "status": "pass", "violating_rows": 0, "detail": []}


def test_every_close_records_the_control(con):
    rows = con.execute(
        "SELECT strftime(month, '%Y-%m'), status FROM ctl.control_results WHERE control_id = 'CTL-PROV' "
        "ORDER BY month").fetchall()
    assert rows == [(m, "pass") for m in MONTHS]


def test_the_control_is_in_the_check_output_before_the_checks_and_not_in_a_plain_close(mini_closed_copy):
    plain = close_mini(mini_closed_copy)
    assert plain.code == 0 and "CTL-PROV" not in [c["id"] for c in plain.json["controls"]]
    checked = close_mini(mini_closed_copy, "2026-04", "--check")
    ids = [c["id"] for c in checked.json["controls"]]
    assert checked.code == 0 and ids[ids.index("CTL-PROV") + 1] == "CHK-COMPONENTS"
    assert checked.json["result_hash"] == plain.json["result_hash"]


# ------------------------------------------------------------------ the closed grammar


VALID = [
    "cp/2026-04/acct_0001/revenue", "cp/2026-04/acct_0001/trailing3_margin", "pnl/2026-04/5100",
    "rollup/2026-04/accounts_status_change_by_tokens", "rollup/2026-04/margin_pct_p75",
    "flag/2026-04/acct_0001", "cpr/2026-04/acct_0001/by_tokens", "cohort/2026-04/model_mix_median",
    "scenario/2026-03/full/Q1/acct_0001/above_floor", "scenario/2026-03/half/summary/acct_0001/recovery_quarter",
    "scenario/2026-03/summary/monthly_rate", "scenario/2026-03/flat/summary/accounts_recovered_Q3",
    "scenario/2026-03/full/summary/persistence_pct", "ref/model_prices/some-model", "cfg/policy",
    "bvb/build_cost", "bvb/vendor_annual_cost/acme",
]
INVALID = [
    "", "cp/2026-04/acct_0001/bogus", "cp/2026-13/acct_0001/revenue", "cp/2026-04/revenue",
    "pnl/2026-04/9999", "rollup/2026-04/accounts_secret", "flag/2026-04", "cohort/2026-04/model_mix",
    "scenario/2026-03/quarter/Q1/acct_0001/above_floor", "scenario/2026-03/full/Q5/acct_0001/margin_pct",
    "scenario/2026-03/full/Q1/acct_0001/revenue", "scenario/2026-03/summary/persistence_pct",
    "ref/model_prices", "cfg/", "bvb/build_cost/extra", "bvb/vendor_annual_cost", "other/2026-04/x",
]


@pytest.mark.parametrize("number_id", VALID)
def test_an_id_in_the_grammar_is_registered(number_id):
    assert prov.is_registered(number_id)


@pytest.mark.parametrize("number_id", INVALID)
def test_an_id_outside_the_grammar_is_not(number_id):
    assert not prov.is_registered(number_id) and prov.month_of_id(number_id) is None


def test_the_month_slot_of_each_family():
    assert prov.month_of_id("scenario/2026-03/full/Q1/acct_0001/margin_pct") == "2026-03"
    assert prov.month_of_id("cp/2026-04/acct_0001/revenue") == "2026-04"
    assert prov.month_of_id("ref/model_prices/x") is None and prov.month_of_id("cfg/policy") is None
    assert prov.month_of_id("bvb/build_cost") is None


def test_every_stored_id_is_registered_and_its_month_matches(con):
    for number_id, month in con.execute("SELECT number_id, month FROM prov.records").fetchall():
        assert prov.is_registered(number_id), number_id
        assert (month is None) == (prov.family_of(number_id) in prov.MONTH_LESS_FAMILIES), number_id


def test_the_writer_refuses_an_unregistered_id(tmp_path):
    c = connect(str(tmp_path / "w.duckdb"))
    init_schemas(c)
    with pytest.raises(MarginError) as err:
        prov.write_records(c, [{"number_id": "cp/2026-04/acct_0001/bogus"}])
    assert err.value.exit_code == 6 and "unregistered" in err.value.message
    assert c.execute("SELECT count(*) FROM prov.records").fetchone()[0] == 0


# ------------------------------------------------------------------ tampering exits 6


@pytest.mark.slow
def test_a_tampered_leaf_digest_exits_6_and_names_it(mini_closed_copy):
    before = snapshot(mini_closed_copy)
    sql(mini_closed_copy, "UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = ?", LEAF)
    tampered = snapshot(mini_closed_copy)
    result = close_mini(mini_closed_copy)
    assert result.code == 6, result.stderr
    assert result.json["status"] == "fail" and result.json["month"] == "2026-04"
    detail = failed(result)["CTL-PROV"]
    assert any(d.startswith(f"{LEAF}: digest does not re-derive") for d in detail)
    assert any(d.startswith("cp/2026-04/acct_0001/contribution_profit: digest does not re-derive") for d in detail)
    assert snapshot(mini_closed_copy) == tampered != before  # a failing close writes nothing


@pytest.mark.slow
def test_a_tampered_digest_exits_6_under_check_too(mini_closed_copy):
    sql(mini_closed_copy, "UPDATE prov.records SET input_digest = repeat('f', 64) WHERE number_id = ?", COMPOSITE)
    result = close_mini(mini_closed_copy, "2026-04", "--check")
    assert result.code == 6 and list(failed(result)) == ["CTL-PROV"]
    assert any(d.startswith(f"{COMPOSITE}: digest does not re-derive") for d in failed(result)["CTL-PROV"])


@pytest.mark.slow
def test_a_tampered_value_exits_6_because_the_result_hash_moves(mini_closed_copy):
    sql(mini_closed_copy, "UPDATE prov.records SET value_cents = value_cents + 1 WHERE number_id = ?", COMPOSITE)
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    assert any(d.startswith("result_hash/2026-04:") for d in failed(result)["CTL-PROV"])


@pytest.mark.slow
def test_a_deleted_record_exits_6(mini_closed_copy):
    sql(mini_closed_copy, "DELETE FROM prov.records WHERE number_id = ?", LEAF)
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    assert f"{LEAF}: no complete provenance record" in failed(result)["CTL-PROV"]


@pytest.mark.slow
def test_a_record_with_no_digest_exits_6(mini_closed_copy):
    sql(mini_closed_copy, "UPDATE prov.records SET input_digest = '' WHERE number_id = 'pnl/2026-04/5300'")
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    assert "pnl/2026-04/5300: no complete provenance record" in failed(result)["CTL-PROV"]


@pytest.mark.slow
def test_an_unregistered_id_exits_6(mini_closed_copy):
    sql(mini_closed_copy,
        "INSERT INTO prov.records SELECT 'cp/2026-04/acct_0001/bogus', month, value_cents, value_text, unit, "
        "step, git_sha, producer_kind, producer_id, input_digest, input_row_count, source_query, "
        "checks_passed, signer, signoff_ref, run_id FROM prov.records WHERE number_id = ?", LEAF)
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    assert "cp/2026-04/acct_0001/bogus: not in the number registry" in failed(result)["CTL-PROV"]


@pytest.mark.slow
def test_a_stored_query_that_is_not_a_select_fails_and_runs_nothing(mini_closed_copy):
    sql(mini_closed_copy, "UPDATE prov.records SET source_query = 'SELECT 1; DROP TABLE raw.gl' WHERE number_id = ?", LEAF)
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    assert any("not a single SELECT" in d for d in failed(result)["CTL-PROV"])
    with connect(str(mini_closed_copy), read_only=True) as c:
        assert c.execute("SELECT count(*) FROM raw.gl").fetchone()[0] > 0


@pytest.mark.slow
def test_a_tampered_config_record_is_found_through_the_flags_it_hangs_on(mini_closed_copy):
    sql(mini_closed_copy, "UPDATE prov.records SET input_digest = repeat('1', 64) WHERE number_id = 'cfg/policy'")
    result = close_mini(mini_closed_copy)
    assert result.code == 6
    detail = failed(result)["CTL-PROV"]
    assert any(d.startswith("cfg/policy:") for d in detail) and any(d.startswith("flag/2026-02/") for d in detail)  # the first month fails


def test_a_key_mismatch_outranks_a_provenance_failure(mini_closed_copy, tmp_path):
    truth = tmp_path / "truth"
    shutil.copytree(MINI_TRUTH, truth)
    cost = truth / "cost_components.csv"
    with cost.open(newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]["revenue_cents"] = str(int(rows[0]["revenue_cents"]) + 1)
    with cost.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    month, account = rows[0]["month"], rows[0]["account_id"]
    sql(mini_closed_copy, "UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = ?",
        f"cp/{month}/{account}/revenue")
    assert close_mini(mini_closed_copy, month).code == 6  # without the key, the provenance failure
    assert close_mini(mini_closed_copy, month, "--check", "--truth", str(truth)).code == 5


# ------------------------------------------------------------------ a failure leaves the month open


@pytest.mark.slow
def test_a_failing_control_on_a_fresh_month_rolls_back_the_records_and_leaves_it_open(tmp_path, monkeypatch):
    warehouse = tmp_path / "w.duckdb"
    monkeypatch.setenv("MARGIN_SIGNOFF_DIR", str(FIXTURE_SIGNOFF))
    paths = resolve(fixture="mini", warehouse=str(warehouse), config=str(MINI_CONFIG))
    fail = {"id": "CTL-PROV", "status": "fail", "violating_rows": 1, "detail": ["cp/2026-02/acct_0001/revenue: x"]}
    monkeypatch.setattr(prov, "control", lambda *a, **k: fail)
    with pytest.raises(MarginError) as err:
        close_mod.close_month(paths, "2026-02")
    assert err.value.exit_code == 6 and err.value.failed_controls[0]["id"] == "CTL-PROV"
    assert err.value.controls[-1] == {"id": "CTL-PROV", "status": "fail", "violating_rows": 1}
    with connect(str(warehouse), read_only=True) as c:
        assert c.execute("SELECT count(*) FROM state.close_state").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM prov.records WHERE month IS NOT NULL").fetchone()[0] == 0
        assert c.execute("SELECT status FROM ctl.control_results WHERE control_id = 'CTL-PROV'").fetchall() == [("fail",)]
