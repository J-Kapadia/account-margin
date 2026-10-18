"""`margin scenario --month M` on copies of the closed `mini` warehouse.

The run writes `scenario.inputs`, `scenario.results` and the `scenario/` records, every one a
composite over the base month's `cp/` records and the list-price series; a close afterwards finds
the month as it was.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import MINI_CONFIG, connect, margin
from margin import prov

MONTH = "2026-03"
CASES = ("full", "half", "flat")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
LARGEST = "acct_0006"  # the most inference cost of the population: its cost moves by whole cents
ZERO_REVENUE = "acct_0010"


def run_scenario(warehouse, month=MONTH, *extra):
    return margin("scenario", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                  "--warehouse", str(warehouse), *extra)


def close_month(warehouse, month=MONTH):
    return margin("close", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                  "--warehouse", str(warehouse))


def records(warehouse, like="scenario/%"):
    with connect(str(warehouse), read_only=True) as con:
        return {
            r[0]: r[1:] for r in con.execute(
                "SELECT number_id, value_cents, value_text, unit, input_digest FROM prov.records "
                "WHERE number_id LIKE ? ORDER BY 1", [like]).fetchall()
        }


def population(warehouse):
    with connect(str(warehouse), read_only=True) as con:
        return [a for (a,) in con.execute(
            "SELECT account_id FROM mart.fct_flags WHERE month = ? AND flag IN ('firm', 'soft') ORDER BY 1",
            [f"{MONTH}-01"]).fetchall()]


@pytest.fixture(scope="module")
def ran(mini_closed, tmp_path_factory):
    """(warehouse, envelope) after one scenario run over a private copy; read-only for the tests."""
    warehouse = tmp_path_factory.mktemp("scenario") / "w.duckdb"
    shutil.copy(mini_closed, warehouse)
    result = run_scenario(warehouse)
    assert result.code == 0, result.stderr
    return warehouse, result.json


def test_a_run_writes_every_record_of_the_grammar(ran):
    warehouse, env = ran
    accounts = population(warehouse)
    expected = len(accounts) * 3 * (4 * 4 + 1) + 2 + 3 * 5
    found = records(warehouse)
    assert len(found) == expected
    assert (env["month"], env["accounts"], env["cases"], env["records"]) == (MONTH, len(accounts), list(CASES), expected)
    assert all(prov.family_of(number_id) == "scenario" for number_id in found)
    assert env["monthly_rate"] == found[f"scenario/{MONTH}/summary/monthly_rate"][1]
    assert env["computed_change"] == found[f"scenario/{MONTH}/summary/computed_change"][1]


def test_explain_reaches_the_series_and_the_base_month(ran):
    warehouse, _ = ran
    number_id = f"scenario/{MONTH}/full/Q4/{LARGEST}/contribution_profit"
    result = margin("explain", number_id, "--fixture", "mini", "--warehouse", str(warehouse), "--json")
    assert result.code == 0, result.stderr
    ids = []

    def walk(node):
        ids.append(node["number_id"])
        assert node["digest_verified"] is True
        for child in node.get("children", []):
            walk(child)

    walk(result.json["chain"])
    assert "ref/list_price_series/large" in ids
    for component in prov.LEAF_COMPONENTS:
        assert f"cp/{MONTH}/{LARGEST}/{component}" in ids


def test_flat_holds_the_base_cost_and_half_lies_between_flat_and_full(ran):
    warehouse, _ = ran
    found = records(warehouse)
    cp = records(warehouse, f"cp/{MONTH}/%/inference_cost")
    for account in population(warehouse):
        for quarter in QUARTERS:
            flat = found[f"scenario/{MONTH}/flat/{quarter}/{account}/inference_cost"][0]
            assert flat == cp[f"cp/{MONTH}/{account}/inference_cost"][0]
    cost = {c: found[f"scenario/{MONTH}/{c}/Q4/{LARGEST}/inference_cost"][0] for c in CASES}
    assert min(cost["flat"], cost["full"]) < cost["half"] < max(cost["flat"], cost["full"])


def test_recovered_counts_do_not_exceed_the_population_and_never_fall(ran):
    warehouse, _ = ran
    found = records(warehouse)
    total = len(population(warehouse))
    for case in CASES:
        counts = [found[f"scenario/{MONTH}/{case}/summary/accounts_recovered_{q}"][0] for q in QUARTERS]
        assert counts == sorted(counts) and counts[-1] <= total
        assert found[f"scenario/{MONTH}/{case}/summary/persistence_pct"][1] == {"full": "100", "half": "50", "flat": "0"}[case]


def test_a_zero_revenue_account_has_no_margin(ran):
    warehouse, _ = ran
    found = records(warehouse)
    for case in CASES:
        for quarter in QUARTERS:
            assert found[f"scenario/{MONTH}/{case}/{quarter}/{ZERO_REVENUE}/margin_pct"][1] == "n/a"
        assert found[f"scenario/{MONTH}/{case}/summary/{ZERO_REVENUE}/recovery_quarter"][1] == "none"


def test_a_close_afterwards_finds_the_month_unchanged(mini_closed_copy):
    warehouse = mini_closed_copy
    before = close_month(warehouse)
    assert before.code == 0, before.stderr
    assert run_scenario(warehouse).code == 0
    with connect(str(warehouse), read_only=True) as con:
        results = con.execute("SELECT count(*) FROM scenario.results").fetchone()[0]
    after = close_month(warehouse)
    assert after.code == 0, after.stderr
    assert after.json["result_hash"] == before.json["result_hash"]
    with connect(str(warehouse), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM scenario.results").fetchone()[0] == results > 0
    assert len(records(warehouse)) > 0


def test_an_open_month_exits_4(mini_closed_copy):
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = '2026-04-01'")
    result = run_scenario(mini_closed_copy, "2026-04")
    assert result.code == 4
    assert "month 2026-04 not closed" in result.json["message"]
    assert records(mini_closed_copy) == {}


@pytest.mark.parametrize("tampered", [
    f"scenario/{MONTH}/full/Q4/{LARGEST}/contribution_profit",
    f"cp/{MONTH}/{LARGEST}/inference_cost",
    "ref/list_price_series/large",
])
def test_a_tampered_record_makes_explain_exit_6(ran, tmp_path, tampered):
    """A scenario number fails when its own digest or any child's digest no longer re-derives."""
    copy = tmp_path / "w.duckdb"
    shutil.copy(ran[0], copy)
    with connect(str(copy)) as con:
        con.execute("UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = ?", [tampered])
    result = margin("explain", f"scenario/{MONTH}/full/Q4/{LARGEST}/contribution_profit", "--fixture", "mini",
                    "--warehouse", str(copy), "--json")
    assert result.code == 6


def test_a_rerun_leaves_the_same_records(ran, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copy(ran[0], copy)
    first = records(copy)
    result = run_scenario(copy)
    assert result.code == 0, result.stderr
    assert records(copy) == first
