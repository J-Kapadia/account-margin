"""`explain` on every ID family of the closed `mini` fixture.

Twenty IDs per month, drawn with `random.Random(20261013)`, each re-derive their digest; the
`--json` output validates against the envelope schema; an open month exits 4 and an ID outside the
grammar exits 2 with its nearest neighbours.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from conftest import connect
import jsonschema
import pytest
from click.testing import CliRunner

from conftest import margin
from margin import prov
from margin.cli import main

MONTHS = ("2026-02", "2026-03", "2026-04")
SEED = 20261013
VALIDATOR = jsonschema.Draft202012Validator(
    json.loads((Path(__file__).resolve().parents[1] / "src/margin/schemas/envelope.schema.json").read_text())
)


def run(warehouse, *args):
    """The real command in-process; `--json` envelopes are validated on the way."""
    result = CliRunner().invoke(main, ["explain", *args, "--fixture", "mini", "--warehouse", str(warehouse), "--json"])
    env = json.loads(result.stdout)
    VALIDATOR.validate(env)
    return result.exit_code, env


@pytest.fixture(scope="module")
def ids_by_month(mini_closed):
    with connect(str(mini_closed), read_only=True) as con:
        return {
            m: [r for (r,) in con.execute(
                "SELECT number_id FROM prov.records WHERE month = ? ORDER BY 1", [f"{m}-01"]).fetchall()]
            for m in MONTHS
        }


def walk(node):
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def assert_chain_ends_at_verified_rows(node):
    """Re-derive every digest in the printed chain, independently of `explain`."""
    for n in walk(node):
        if n.get("truncated"):
            continue
        assert n["digest_verified"] is True, n["number_id"]
        if "rows" in n:
            assert prov.digest_rows([r["_row_hash"] for r in n["rows"]]) == n["input_digest"], n["number_id"]
            assert len(n["rows"]) == n["input_row_count"]
        elif not n.get("repeated"):
            assert prov.digest_children([(c["number_id"], c["input_digest"]) for c in n["children"]]) == n["input_digest"]


def test_twenty_ids_per_month_rederive_their_digest(mini_closed, ids_by_month):
    rng = random.Random(SEED)
    for month in MONTHS:
        sample = rng.sample(ids_by_month[month], 20)
        for number_id in sample:
            code, env = run(mini_closed, number_id)
            assert code == 0, (number_id, env)
            assert env["number_id"] == number_id and env["month"] == month and env["digest_verified"] is True
            assert env["chain"]["number_id"] == number_id
            assert_chain_ends_at_verified_rows(env["chain"])


def test_every_id_shape_of_a_month_explains(mini_closed, ids_by_month):
    """One ID per family and per last path segment, so every component and metric is covered."""
    shapes = {}
    for number_id in ids_by_month["2026-04"]:
        shapes.setdefault((number_id.split("/", 1)[0], number_id.rsplit("/", 1)[-1] if number_id.count("/") > 2 else ""), number_id)
    assert {family for family, _ in shapes} == {"cp", "cpr", "cohort", "flag", "pnl", "rollup"}
    for number_id in shapes.values():
        code, env = run(mini_closed, number_id)
        assert code == 0 and env["digest_verified"], number_id


def test_a_leaf_lists_its_source_rows_and_a_composite_its_children(mini_closed):
    _, leaf = run(mini_closed, "cp/2026-04/acct_0001/revenue")
    assert leaf["chain"]["rows"] and "children" not in leaf["chain"]
    assert {r["source_table"] for r in leaf["chain"]["rows"]} == {"raw.invoices"}
    _, flag = run(mini_closed, "flag/2026-04/acct_0001")
    kinds = {c["number_id"].split("/", 1)[0] for c in flag["chain"]["children"]}
    assert kinds == {"cpr", "cohort", "cfg"}


def test_the_default_depth_reaches_the_leaves_and_depth_stops_the_walk(mini_closed):
    _, full = run(mini_closed, "cp/2026-04/acct_0001/margin_pct")
    leaves = [n for n in walk(full["chain"]) if "rows" in n]
    assert {n["number_id"].rsplit("/", 1)[-1] for n in leaves} >= {"revenue", "inference_cost", "support_cost"}
    _, shallow = run(mini_closed, "cp/2026-04/acct_0001/margin_pct", "--depth", "1")
    by_name = {c["number_id"].rsplit("/", 1)[-1]: c for c in shallow["chain"]["children"]}
    assert "rows" in by_name["revenue"]  # a leaf one level down is still shown
    assert by_name["contribution_profit"]["children"] and all(
        c.get("truncated") for c in by_name["contribution_profit"]["children"])
    _, zero = run(mini_closed, "cp/2026-04/acct_0001/margin_pct", "--depth", "0")
    assert all(c.get("truncated") and "children" not in c for c in zero["chain"]["children"])
    assert zero["chain"]["digest_verified"]  # the digest is re-derived from the children's stored digests


def test_a_number_shared_by_many_parents_is_expanded_once(mini_closed):
    _, env = run(mini_closed, "rollup/2026-04/accounts_firm")
    nodes = [n for n in walk(env["chain"]) if n["number_id"] == "cfg/policy"]
    assert len(nodes) > 1 and sum("rows" in n for n in nodes) == 1
    assert all(n.get("repeated") for n in nodes if "rows" not in n)


def test_month_less_ids_are_answered_from_their_record(mini_closed):
    with connect(str(mini_closed), read_only=True) as con:
        ref = con.execute("SELECT number_id FROM prov.records WHERE number_id LIKE 'ref/%' ORDER BY 1").fetchone()[0]
    for number_id in ("cfg/policy", "cfg/allocation", ref):
        code, env = run(mini_closed, number_id)
        assert code == 0 and "month" not in env and env["chain"]["month"] is None
        assert env["chain"]["rows"] and env["digest_verified"]


MISSING_MONTH_LESS = ["bvb/build_cost", "ref/model_prices/no-such-model", "cfg/missing"]


@pytest.mark.parametrize("number_id", MISSING_MONTH_LESS)
def test_an_unknown_month_less_id_in_a_closed_warehouse_exits_2(mini_closed, number_id):
    code, env = run(mini_closed, number_id)
    assert code == 2 and env["message"].startswith(f"unknown number ID {number_id}; nearest: ")


@pytest.mark.parametrize("number_id", MISSING_MONTH_LESS)
def test_a_month_less_id_in_a_warehouse_with_no_records_exits_4(mini_closed_copy, number_id):
    with connect(str(mini_closed_copy)) as con:
        con.execute("DELETE FROM prov.records")
    code, env = run(mini_closed_copy, number_id)
    assert code == 4 and env["message"] == f"no provenance record for {number_id}: run close"


def test_a_scenario_id_needs_its_base_month_closed_and_its_records(mini_closed_copy):
    number_id = "scenario/2026-04/full/Q1/acct_0001/inference_cost"
    code, env = run(mini_closed_copy, number_id)
    assert code == 4 and env["message"] == "scenario not run for month 2026-04"
    code, env = run(mini_closed_copy, "scenario/2026-04/summary/monthly_rate")
    assert code == 4 and env["message"] == "scenario not run for month 2026-04"
    child = "cp/2026-04/acct_0001/inference_cost"
    with connect(str(mini_closed_copy)) as con:
        (digest,) = con.execute("SELECT input_digest FROM prov.records WHERE number_id = ?", [child]).fetchone()
        con.execute(
            "INSERT INTO prov.records SELECT ?, month, 1, '1', unit, 'margin.scenario', git_sha, producer_kind, "
            "producer_id, ?, 1, ?, checks_passed, signer, signoff_ref, run_id FROM prov.records WHERE number_id = ?",
            [number_id, prov.digest_children([(child, digest)]), json.dumps([child]), child])
    code, env = run(mini_closed_copy, number_id)
    assert code == 0 and env["month"] == "2026-04" and env["chain"]["children"][0]["number_id"] == child
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = DATE '2026-04-01'")
    code, env = run(mini_closed_copy, number_id)
    assert code == 4 and env["message"] == "month 2026-04 not closed"


# ------------------------------------------------------------------ failures


def test_an_open_month_exits_4_for_every_month_family(mini_closed_copy):
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = DATE '2026-03-01'")
    for number_id in ("cp/2026-03/acct_0001/revenue", "pnl/2026-03/4000", "rollup/2026-03/revenue",
                      "flag/2026-03/acct_0001", "cpr/2026-03/acct_0001/by_revenue",
                      "cohort/2026-03/model_mix_median", "cp/2026-03/acct_9999/revenue"):
        code, env = run(mini_closed_copy, number_id)
        assert code == 4 and env["message"] == "month 2026-03 not closed", number_id
    assert run(mini_closed_copy, "cp/2026-04/acct_0001/revenue")[0] == 0  # other months still answer
    assert run(mini_closed_copy, "cfg/policy")[0] == 0  # a month-less ID has no month to be open


def test_an_id_outside_the_grammar_exits_2_with_three_nearest(mini_closed):
    code, env = run(mini_closed, "cp/2026-04/acct_0001/revenu")
    assert code == 2 and env["message"].startswith("unknown number ID cp/2026-04/acct_0001/revenu; nearest: ")
    assert len(env["message"].split("nearest: ")[1].split(", ")) == 3
    assert run(mini_closed, "nonsense")[0] == 2
    code, env = run(mini_closed, "cp/2026-04/acct_9999/revenue")  # in the grammar, no such account
    assert code == 2 and "nearest" in env["message"]


def test_a_tampered_digest_exits_6_naming_the_number(mini_closed_copy):
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = 'cp/2026-04/acct_0001/revenue'")
    code, env = run(mini_closed_copy, "cp/2026-04/acct_0001/revenue")
    assert code == 6 and env["message"].startswith("digest mismatch for cp/2026-04/acct_0001/revenue")
    code, env = run(mini_closed_copy, "cp/2026-04/acct_0001/contribution_profit")  # reached through its child
    assert code == 6
    assert run(mini_closed_copy, "cp/2026-04/acct_0001/contribution_profit", "--depth", "0")[0] == 6  # child digest differs from the parent's


def test_a_missing_child_record_exits_6(mini_closed_copy):
    with connect(str(mini_closed_copy)) as con:
        con.execute("DELETE FROM prov.records WHERE number_id = 'cp/2026-04/acct_0001/revenue'")
    code, env = run(mini_closed_copy, "cp/2026-04/acct_0001/contribution_profit")
    assert code == 6 and "no record for cp/2026-04/acct_0001/revenue" in env["message"]


def test_a_stored_query_that_is_not_a_select_exits_6(mini_closed_copy):
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE prov.records SET source_query = 'DROP TABLE raw.gl' WHERE number_id = 'cp/2026-04/acct_0001/revenue'")
    assert run(mini_closed_copy, "cp/2026-04/acct_0001/revenue")[0] == 6


# ------------------------------------------------------------------ the real command


def test_the_human_view_prints_the_chain_and_the_source_rows(mini_closed):
    result = margin("explain", "cp/2026-04/acct_0001/margin_pct", "--fixture", "mini", "--warehouse", str(mini_closed))
    assert result.code == 0, result.stderr
    assert result.stdout.startswith("cp/2026-04/acct_0001/margin_pct = ")
    assert "(verified)" in result.stdout and "source raw.invoices _row_hash=" in result.stdout
    assert "  cp/2026-04/acct_0001/contribution_profit = " in result.stdout


def test_a_subprocess_sample_matches_the_in_process_command(mini_closed, ids_by_month):
    rng = random.Random(SEED)
    for number_id in rng.sample(ids_by_month["2026-03"], 3):
        result = margin("explain", number_id, "--fixture", "mini", "--warehouse", str(mini_closed), "--json")
        assert result.code == 0, result.stderr
        VALIDATOR.validate(result.json)
        assert result.json == run(mini_closed, number_id)[1]
