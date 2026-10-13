"""The dbt close layers on the committed `mini` fixture.

Every figure of the mart is compared with the Python reference (`truth_reference`), which shares
no code with the dbt macros, and with the committed expected mart and truth.
"""

from __future__ import annotations

import csv
import json
import shutil
from decimal import Decimal

import duckdb
import pytest

from conftest import (
    MINI_BOOKS, MINI_CONFIG, MINI_PRICES, MINI_TRUTH, FIXTURES, ROOT, build_mini_warehouse, margin,
)
from macro_sql import macro
from margin import truth_reference as ref
from margin.gen_config import load_config

RATES = ROOT / "data" / "reference" / "support_rates.csv"


@pytest.fixture(scope="module")
def con(mini_warehouse):
    c = duckdb.connect(str(mini_warehouse), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def reference():
    """Every month of `mini` recomputed from its CSV files by the independent implementation."""
    config = load_config(MINI_CONFIG)
    drivers = [(d.code, d.median_floor) for d in config.policy.drivers]
    return ref.recompute_from_books(
        MINI_BOOKS, MINI_PRICES, RATES, config.allocation.rules, config.policy.floor_pct, drivers
    )


def test_the_committed_fixture_verifies_against_its_manifest():
    result = margin(
        "gen", "--verify", "--fixture", "mini", "--config", str(MINI_CONFIG), "--prices", str(MINI_PRICES),
    )
    assert result.code == 0, result.stderr


def _rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _mart(con):
    return con.execute(
        "SELECT strftime(month, '%Y-%m') AS month, rule, account_id, revenue_cents, inference_cost_cents,"
        " cloud_direct_cents, cloud_shared_cents, support_cost_cents, contribution_profit_cents,"
        " coalesce(cast(margin_pct AS VARCHAR), 'n/a') AS margin_pct,"
        " coalesce(cast(trailing3_margin_pct AS VARCHAR), 'n/a') AS trailing3_margin_pct,"
        " lower(cast(below_floor AS VARCHAR)) AS below_floor"
        " FROM mart.fct_contribution_by_rule ORDER BY month, rule, account_id"
    ).fetchall()


# ------------------------------------------------------------------ the mart


def test_the_mart_equals_the_committed_expected_mart(con):
    expected = [tuple(r.values()) for r in _rows(FIXTURES / "mini_expected_mart.csv")]
    got = [tuple(str(v) for v in row) for row in _mart(con)]
    assert sorted(got) == sorted(tuple(str(v) for v in row) for row in expected)
    assert len(got) == 108  # 12 accounts x 3 months x 3 rules


def test_the_expected_mart_is_the_reference_output(reference):
    from margin.gen import expected_mart_rows

    config = load_config(MINI_CONFIG)
    rows = expected_mart_rows(reference, config.allocation.rules)
    committed = [list(r.values()) for r in _rows(FIXTURES / "mini_expected_mart.csv")]
    assert [[str(v) for v in r] for r in rows] == committed


def test_the_reference_and_contribution_cents_give_equal_profit_for_every_account_rule_and_month(con, reference):
    expr = macro("contribution_cents", "r", "i", "d", "s", "u")
    checked = 0
    for month, result in reference.items():
        for account_id, c in result.comps.items():
            for rule in c.shared:
                (macro_profit,) = con.execute(
                    f"SELECT {expr} FROM (SELECT CAST(? AS BIGINT) AS r, CAST(? AS BIGINT) AS i,"
                    " CAST(? AS BIGINT) AS d, CAST(? AS BIGINT) AS s, CAST(? AS BIGINT) AS u)",
                    [c.revenue, c.inference, c.direct, c.shared[rule], c.support],
                ).fetchone()
                (mart_profit,) = con.execute(
                    "SELECT contribution_profit_cents FROM mart.fct_contribution_by_rule"
                    " WHERE strftime(month, '%Y-%m') = ? AND account_id = ? AND rule = ?",
                    [month, account_id, rule],
                ).fetchone()
                assert macro_profit == mart_profit == c.profit[rule] == ref.contribution_cents(
                    c.revenue, c.inference, c.direct, c.shared[rule], c.support
                )
                checked += 1
    assert checked == 108


def test_the_signed_rule_view_is_the_recommended_rules_rows(con):
    rule = load_config(MINI_CONFIG).allocation.recommended
    view = con.execute("SELECT * FROM mart.fct_contribution_profit ORDER BY month, account_id").fetchall()
    cols = [d[0] for d in con.description]
    full = con.execute(
        f"SELECT {', '.join(cols)} FROM mart.fct_contribution_by_rule WHERE rule = '{rule}' ORDER BY month, account_id"
    ).fetchall()
    assert view == full and len(view) == 36
    assert "rule" not in cols and "below_floor" not in cols


def test_allocation_conserves_every_pool_under_every_rule(con, reference):
    for month, result in reference.items():
        for rule in ("by_tokens", "by_revenue", "equal_usage"):
            (shared,) = con.execute(
                "SELECT sum(cloud_shared_cents) FROM mart.fct_contribution_by_rule"
                " WHERE strftime(month, '%Y-%m') = ? AND rule = ?", [month, rule]).fetchone()
            assert shared == result.pool_cents
        (inference,) = con.execute(
            "SELECT sum(inference_cost_cents) FROM mart.fct_contribution_profit"
            " WHERE strftime(month, '%Y-%m') = ?", [month]).fetchone()
        assert inference == sum(result.provider_cents.values())


def test_rules_differ_so_the_choice_of_rule_is_a_finding(con):
    spread = con.execute(
        "SELECT account_id, count(DISTINCT cloud_shared_cents) FROM mart.fct_contribution_by_rule"
        " WHERE month = DATE '2026-03-01' GROUP BY account_id HAVING count(DISTINCT cloud_shared_cents) > 1"
    ).fetchall()
    assert spread


def test_the_fixture_carries_the_forced_content(con):
    zero, fee_only, negative = (con.execute(q).fetchall() for q in (
        "SELECT month FROM mart.fct_contribution_profit WHERE revenue_cents = 0 AND inference_cost_cents"
        " + cloud_direct_cents + cloud_shared_cents + support_cost_cents > 0",
        "SELECT p.month FROM mart.fct_contribution_profit p JOIN intermediate.int_account_facts f"
        " ON f.month = p.month AND f.account_id = p.account_id WHERE NOT f.has_usage AND p.revenue_cents > 0"
        " AND p.cloud_direct_cents + p.support_cost_cents > 0",
        "SELECT month FROM mart.fct_contribution_profit WHERE revenue_cents < 0",
    ))
    assert len(zero) == len(fee_only) == len(negative) == 3  # one account of each, every month
    (null_margin,) = con.execute(
        "SELECT count(*) FROM mart.fct_contribution_profit WHERE revenue_cents = 0 AND margin_pct IS NULL").fetchone()
    assert null_margin == 3  # margin is undefined, never zero


# ------------------------------------------------------------------ flags


def test_flags_match_the_answer_key(con):
    truth = {(r["month"], r["account_id"]): r for r in _rows(MINI_TRUTH / "rule_flags.csv")}
    got = con.execute("SELECT strftime(month, '%Y-%m'), account_id, flag, rules_below_floor, rules_total"
                      " FROM mart.fct_flags").fetchall()
    assert len(got) == len(truth) == 36
    for month, account_id, flag, below, total in got:
        row = truth[(month, account_id)]
        assert (flag, below, total) == (row["flag"], int(row["rules_below_floor"]), 3)


def test_firm_set_is_the_planted_set_with_its_reasons_and_soft_set_is_the_decoys(con):
    planted = json.loads((MINI_TRUTH / "planted_flags.json").read_text())
    for month in ("2026-02", "2026-03", "2026-04"):
        firm = con.execute("SELECT account_id, reason_code FROM mart.fct_flags WHERE flag = 'firm'"
                           " AND strftime(month, '%Y-%m') = ? ORDER BY account_id", [month]).fetchall()
        soft = con.execute("SELECT account_id FROM mart.fct_flags WHERE flag = 'soft'"
                           " AND strftime(month, '%Y-%m') = ? ORDER BY account_id", [month]).fetchall()
        assert firm == [(p["account_id"], p["reason_code"]) for p in planted[month]]
        assert [a for (a,) in soft] == planted["decoys"][month]


def test_one_account_enters_and_one_leaves_the_firm_set_between_march_and_april(con):
    def firm(month):
        return {a for (a,) in con.execute("SELECT account_id FROM mart.fct_flags WHERE flag = 'firm'"
                                          " AND strftime(month, '%Y-%m') = ?", [month]).fetchall()}
    assert len(firm("2026-04") - firm("2026-03")) == 1 and len(firm("2026-03") - firm("2026-04")) == 1


def test_every_flagged_account_has_the_references_reason_and_an_unflagged_one_has_none(con, reference):
    for month, account_id, flag, reason in con.execute(
        "SELECT strftime(month, '%Y-%m'), account_id, flag, reason_code FROM mart.fct_flags").fetchall():
        if flag == "none":
            assert reason is None
        else:
            assert reason == reference[month].reasons[account_id].code


def test_a_negative_and_a_zero_revenue_account_are_classified_by_cost(con):
    rows = dict(con.execute(
        "SELECT p.revenue_cents, f.reason_code FROM mart.fct_flags f JOIN mart.fct_contribution_profit p"
        " ON p.month = f.month AND p.account_id = f.account_id WHERE p.revenue_cents <= 0 AND f.month = DATE '2026-03-01'"
    ).fetchall())
    assert set(rows.values()) == {"DEDICATED_CLOUD", "MODEL_MIX"}
    assert all(r <= 0 for r in rows)


# ------------------------------------------------------------------ tie-out


def test_every_gl_code_ties_to_the_cent(con):
    rows = con.execute("SELECT strftime(month, '%Y-%m'), gl_code, gl_cents, accounts_cents, diff_cents"
                       " FROM mart.fct_pnl_tieout ORDER BY 1, 2").fetchall()
    assert {(m, c) for m, c, *_ in rows} == {
        (m, c) for m in ("2026-02", "2026-03", "2026-04") for c in ("4000", "4100", "4900", "5100", "5200", "5300")
    }
    assert all(diff == 0 and gl == accounts for _, _, gl, accounts, diff in rows)


def _tampered_warehouse(tmp_path, books, edit):
    edit(books)
    return build_mini_warehouse(books, tmp_path / "w.duckdb")


def test_a_one_cent_slip_in_gl_5300_shows_in_the_tie_out(tmp_path, mini_books_copy):
    def slip(books):
        gl = books / "gl.csv"
        lines = gl.read_text().splitlines()
        i = next(n for n, line in enumerate(lines) if line.split(",")[1:3] == ["2026-03", "5300"])
        parts = lines[i].split(",")
        parts[3] = str(int(parts[3]) + 1)
        lines[i] = ",".join(parts)
        gl.write_text("\n".join(lines) + "\n")

    wh = _tampered_warehouse(tmp_path, mini_books_copy, slip)
    with duckdb.connect(str(wh), read_only=True) as c:
        off = c.execute("SELECT strftime(month, '%Y-%m'), gl_code, diff_cents FROM mart.fct_pnl_tieout"
                        " WHERE diff_cents <> 0").fetchall()
    assert off == [("2026-03", "5300", -1)]  # accounts minus ledger


# ------------------------------------------------------------------ unweighted pools


def test_a_pool_with_cents_and_no_weight_is_reported_and_never_spread_by_another_rule(tmp_path, mini_books_copy, reference):
    def empty_usage_month(books):
        usage = books / "usage_monthly.csv"
        lines = usage.read_text().splitlines()
        usage.write_text("\n".join(l for i, l in enumerate(lines) if i == 0 or l.split(",")[1] != "2026-03") + "\n")

    # The reference refuses the same books: there is no fallback to another rule.
    probe = tmp_path / "probe"
    shutil.copytree(mini_books_copy, probe)
    empty_usage_month(probe)
    with pytest.raises(ValueError, match="no eligible weight|no pinned|no provider"):
        ref.recompute_from_books(probe, MINI_PRICES, RATES, ("by_tokens", "by_revenue", "equal_usage"), 30,
                                 [(c, Decimal("0.01")) for c in ("DEEP_DISCOUNT", "MODEL_MIX", "SUPPORT_LOAD", "DEDICATED_CLOUD")])

    wh = _tampered_warehouse(tmp_path, mini_books_copy, empty_usage_month)
    with duckdb.connect(str(wh), read_only=True) as c:
        violations = c.execute(
            "SELECT rule, pool, pool_cents > 0 FROM intermediate.int_pool_violations"
            " WHERE strftime(month, '%Y-%m') = '2026-03' ORDER BY rule NULLS LAST, pool").fetchall()
        other_months = c.execute(
            "SELECT count(*) FROM intermediate.int_pool_violations WHERE strftime(month, '%Y-%m') <> '2026-03'"
        ).fetchone()[0]
        shared = dict(c.execute(
            "SELECT rule, sum(cloud_shared_cents) FROM mart.fct_contribution_by_rule"
            " WHERE month = DATE '2026-03-01' GROUP BY rule").fetchall())
        pool_cents = c.execute("SELECT pool_cents FROM intermediate.int_cloud_shared_pools"
                               " WHERE month = DATE '2026-03-01' AND rule = 'by_tokens'").fetchone()[0]
        tie = c.execute("SELECT gl_code, diff_cents FROM mart.fct_pnl_tieout WHERE month = DATE '2026-03-01'"
                        " AND diff_cents <> 0 ORDER BY gl_code").fetchall()
    # shared cloud has no weight under by_tokens and equal_usage; by_revenue still has positive revenue
    assert [(r, p) for r, p, _ in violations if p == "shared cloud"] == [
        ("by_tokens", "shared cloud"), ("equal_usage", "shared cloud")]
    assert any(p.startswith("inference ") for _, p, _ in violations)  # every provider invoice, too
    assert other_months == 0
    # nothing was moved to another rule: the unweighted pools stay unallocated
    assert shared["by_tokens"] == shared["equal_usage"] == 0 and shared["by_revenue"] == pool_cents
    # and the ledger no longer ties, which is what the control will fail on
    assert {code for code, _ in tie} >= {"5100", "5200"}


# ------------------------------------------------------------------ the loader and the CLI


def test_absent_cost_books_and_prices_load_as_empty_tables(closed):
    with duckdb.connect(str(closed.warehouse), read_only=True) as c:
        counts = {t: c.execute(f"SELECT count(*) FROM raw.{t}").fetchone()[0]
                  for t in ("cloud_focus", "provider_invoices", "support_tickets", "ref_model_prices",
                            "ref_support_rates")}
        flags = c.execute("SELECT DISTINCT flag FROM mart.fct_flags").fetchall()
    assert counts == {"cloud_focus": 0, "provider_invoices": 0, "support_tickets": 0, "ref_model_prices": 0,
                      "ref_support_rates": 3}
    assert flags == [("none",)]  # the small fixture has no costs, so nothing is below the floor


def test_close_runs_the_new_layers_on_mini_with_the_fixture_config(tmp_path):
    result = margin("close", "--month", "2026-03", "--fixture", "mini", "--config", str(MINI_CONFIG),
                    "--warehouse", str(tmp_path / "w.duckdb"), "--json")
    assert result.code == 0, result.stderr
    assert result.json["controls"] == [{"id": "CTL-TIE-REV", "status": "pass", "violating_rows": 0}]
    with duckdb.connect(str(tmp_path / "w.duckdb"), read_only=True) as c:
        assert c.execute("SELECT count(*) FROM mart.fct_flags").fetchone()[0] == 24  # 2026-02 and 2026-03
        assert c.execute("SELECT status FROM state.close_state ORDER BY month").fetchall() == [("closed",)] * 2
