"""The scenario model on a copy of the closed `mini` warehouse.

Python hands the scaled inference costs over in `scenario.inputs`; one dbt invocation turns them
into `scenario.results`, with profit, margin and the floor test from the same macros as the close.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

from conftest import MINI_CONFIG, ROOT, close_mini, connect
from margin import dbt_runner
from margin.gen_config import load_config

BASE_MONTH = "2026-02-01"
RECOVERING = "acct_0003"  # revenue, so its margin falls below the floor and comes back
NO_REVENUE = "acct_0010"  # zero revenue in the base month: margin_pct is NULL, it never recovers
CASES = ("full", "half", "flat")
# Inference cents per case for k = 1..5 and from k = 6 on: the recovering account crosses the
# floor at k = 6, so its first quarter-end (k = 3) is below and its second (k = 6) is not.
HIGH = {"full": 600_000, "half": 550_000, "flat": 500_000}
LOW = {"full": 300_000, "half": 250_000, "flat": 200_000}


def _inputs():
    rows = []
    for case in CASES:
        for k in range(1, 13):
            rows.append((BASE_MONTH, case, k, RECOVERING, HIGH[case] if k < 6 else LOW[case]))
            rows.append((BASE_MONTH, case, k, NO_REVENUE, 10_000 * k))
    return rows


def _scenario(warehouse):
    con = connect(str(warehouse))
    try:
        con.execute("DELETE FROM scenario.inputs")
        con.executemany("INSERT INTO scenario.inputs VALUES (?, ?, ?, ?, ?)", _inputs())
    finally:
        con.close()
    dbt_runner.build_scenario(warehouse, MINI_CONFIG)


def _results(warehouse):
    con = connect(str(warehouse), read_only=True)
    try:
        cols = [r[0] for r in con.execute("DESCRIBE scenario.results").fetchall()]
        rows = con.execute(
            'SELECT * FROM scenario.results ORDER BY month, "case", account_id, k'
        ).fetchall()
        base = {
            r[0]: r[1:]
            for r in con.execute(
                "SELECT account_id, revenue_cents, cloud_direct_cents, cloud_shared_cents,"
                " support_cost_cents FROM mart.fct_contribution_profit WHERE month = ?",
                [BASE_MONTH],
            ).fetchall()
        }
        return [dict(zip(cols, r)) for r in rows], base
    finally:
        con.close()


def _fingerprint(warehouse):
    con = connect(str(warehouse), read_only=True)
    try:
        return con.execute(
            "SELECT count(*), sum(hash(t)::hugeint) FROM scenario.results t"
        ).fetchone()
    finally:
        con.close()


def test_every_input_row_becomes_a_result_with_the_base_months_components(mini_closed_copy):
    _scenario(mini_closed_copy)
    results, base = _results(mini_closed_copy)
    inference = {(c, k, a): v for _, c, k, a, v in _inputs()}
    assert len(results) == 2 * 3 * 12
    for row in results:
        revenue, direct, shared, support = base[row["account_id"]]
        cost = inference[(row["case"], row["k"], row["account_id"])]
        assert (row["revenue_cents"], row["cloud_direct_cents"], row["cloud_shared_cents"],
                row["support_cost_cents"]) == (revenue, direct, shared, support)
        assert row["inference_cost_cents"] == cost
        assert row["contribution_profit_cents"] == revenue - cost - direct - shared - support


def test_margin_is_null_without_revenue_and_the_floor_test_is_integer(mini_closed_copy):
    _scenario(mini_closed_copy)
    results, _ = _results(mini_closed_copy)
    floor = Decimal(str(load_config(MINI_CONFIG).policy.floor_pct))
    for row in results:
        assert row["below_floor"] == (
            row["contribution_profit_cents"] * 100 < floor * row["revenue_cents"]
        )
        if row["account_id"] == NO_REVENUE:
            assert row["revenue_cents"] == 0 and row["margin_pct"] is None
        else:
            assert row["margin_pct"] is not None


def test_recovery_quarter_is_the_first_quarter_end_back_above_the_floor(mini_closed_copy):
    _scenario(mini_closed_copy)
    results, _ = _results(mini_closed_copy)
    for row in results:
        assert row["quarter"] == f"Q{(row['k'] - 1) // 3 + 1}"
        assert row["quarter_end"] == (row["k"] % 3 == 0)
        recovered = "Q2" if row["account_id"] == RECOVERING else "none"
        assert row["recovery_quarter"] == recovered
    recovering = [r for r in results if r["account_id"] == RECOVERING]
    assert {r["k"] for r in recovering if not r["below_floor"]} == set(range(6, 13))


def test_a_close_on_a_fresh_warehouse_runs_without_scenario_inputs(tmp_path):
    result = close_mini(tmp_path / "fresh.duckdb")
    assert result.code == 0, result.stderr


def test_a_later_close_leaves_the_scenario_results_alone(mini_closed_copy):
    _scenario(mini_closed_copy)
    before = _fingerprint(mini_closed_copy)
    assert before[0] == 72
    result = close_mini(mini_closed_copy)
    assert result.code == 0, result.stderr
    assert _fingerprint(mini_closed_copy) == before


def test_the_project_compiles_without_scenario_inputs(tmp_path):
    warehouse = tmp_path / "empty.duckdb"
    proc = subprocess.run(
        [sys.executable, "-c", dbt_runner._DBT_MAIN, "compile",
         "--project-dir", str(dbt_runner.DBT_DIR), "--profiles-dir", str(dbt_runner.DBT_DIR),
         "--target-path", str(tmp_path / "target"), "--log-path", str(tmp_path / "logs"),
         "--no-use-colors", "--quiet"],
        env=dbt_runner.env_for(warehouse), capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
