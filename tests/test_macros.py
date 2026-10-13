"""The dbt macros against the Python reference.

`apportion()` and the weight functions are run in a bare DuckDB through the rendered macro and
compared with `truth_reference`, the independent implementation: once per weight function. The margin and predicate macros get their edge cases and a property test.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pytest
from hypothesis import given, settings, strategies as st

from macro_sql import macro
from margin import truth_reference as ref

SETTINGS = settings(max_examples=60, deadline=None)
RULES = ("by_tokens", "by_revenue", "equal_usage")


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    yield c
    c.close()


def _apportioned(con, keys: list[str]) -> dict[tuple, int]:
    rows = con.execute(macro("apportion", "pools", "weights", keys) + " ORDER BY ALL").fetchall()
    return {tuple(r[:-1]): r[-1] for r in rows}


# ------------------------------------------------------------------ apportion(): shared cloud

token = st.integers(min_value=0, max_value=10**11)  # up to the token counts of a real month
account_rows = st.lists(
    st.tuples(
        token, token,
        st.integers(min_value=-10**7, max_value=10**9),  # revenue, negative allowed
        st.booleans(),  # has a usage row (it may then carry zero tokens)
    ),
    min_size=1, max_size=9,
)


@pytest.mark.parametrize("rule", RULES)
@SETTINGS
@given(rows=account_rows, pool=st.integers(min_value=0, max_value=10**12))
def test_apportion_agrees_with_the_reference_for_each_shared_rule(con, rule, rows, pool):
    accts = {}
    for n, (tokens_in, tokens_out, revenue, has_usage) in enumerate(rows, start=1):
        a = ref.AcctMonth(f"acct_{n:04d}", credit_cents=revenue)
        if has_usage:
            a.tokens["m"] = (tokens_in, tokens_out)
        accts[a.account_id] = a
    con.execute("CREATE OR REPLACE TEMP TABLE facts (month DATE, account_id VARCHAR, revenue_cents BIGINT,"
                " total_tokens HUGEINT, has_usage BOOLEAN)")
    con.executemany(
        "INSERT INTO facts VALUES (DATE '2026-03-01', ?, ?, ?, ?)",
        [(k, a.revenue_cents, a.total_tokens, a.has_usage) for k, a in accts.items()],
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE weights AS SELECT month, '{rule}' AS rule, account_id, "
                f"{macro('shared_weight', rule)} AS weight FROM facts")
    con.execute(f"CREATE OR REPLACE TEMP TABLE pools AS SELECT DATE '2026-03-01' AS month, '{rule}' AS rule, "
                f"CAST({pool} AS BIGINT) AS pool_cents")

    got = _apportioned(con, ["month", "rule"])
    weights = ref.shared_weights(rule, accts)
    if pool > 0 and not any(w > 0 for w in weights.values()):
        # no eligible weight. The reference refuses; the macro allocates nothing.
        with pytest.raises(ValueError):
            ref.apportion(pool, weights)
        assert got == {}
        return
    want = ref.apportion(pool, weights)
    by_account = {k[2]: v for k, v in got.items()}
    assert {k: v for k, v in want.items() if v} == {k: v for k, v in by_account.items() if v}
    if pool > 0:
        assert sum(by_account.values()) == pool


# ------------------------------------------------------------------ apportion(): inference

usage_rows = st.lists(
    st.tuples(st.integers(0, 1), token, token),  # model index, input and output tokens
    min_size=1, max_size=12,
)


@SETTINGS
@given(
    rows=usage_rows,
    pools=st.tuples(st.integers(0, 10**9), st.integers(0, 10**9)),
    nano=st.tuples(*[st.integers(1, 10**6)] * 4),  # nano-USD per token, input and output per model
)
def test_apportion_agrees_with_the_reference_for_inference(con, rows, pools, nano):
    models = ("m1", "m2")
    prices = {"m1": (nano[0], nano[1]), "m2": (nano[2], nano[3])}
    tokens: dict[tuple[str, str], tuple[int, int]] = {}
    for n, (mi, tin, tout) in enumerate(rows, start=1):
        key = (f"acct_{n:04d}", models[mi])
        tokens[key] = (tin, tout)
    con.execute("CREATE OR REPLACE TEMP TABLE usage (month DATE, provider VARCHAR, model VARCHAR,"
                " account_id VARCHAR, input_tokens BIGINT, output_tokens BIGINT, nano_in BIGINT, nano_out BIGINT)")
    con.executemany(
        "INSERT INTO usage VALUES (DATE '2026-03-01', 'p', ?, ?, ?, ?, ?, ?)",
        [(m, a, i, o, *prices[m]) for (a, m), (i, o) in tokens.items()],
    )
    con.execute("CREATE OR REPLACE TEMP TABLE weights AS SELECT month, provider, model, account_id, "
                + macro("inference_weight", "input_tokens", "output_tokens", "nano_in", "nano_out")
                + " AS weight FROM usage")
    con.execute("CREATE OR REPLACE TEMP TABLE pools (month DATE, provider VARCHAR, model VARCHAR, pool_cents BIGINT)")
    con.executemany("INSERT INTO pools VALUES (DATE '2026-03-01', 'p', ?, ?)", list(zip(models, pools)))

    got = {(k[2], k[3]): v for k, v in _apportioned(con, ["month", "provider", "model"]).items()}
    want: dict[tuple[str, str], int] = {}
    for model, pool in zip(models, pools):
        weights = {a: i * prices[model][0] + o * prices[model][1]
                   for (a, m), (i, o) in tokens.items() if m == model}
        if pool > 0 and not any(w > 0 for w in weights.values()):
            assert not any(m == model for m, _ in got)
            continue
        for account, cents in ref.apportion(pool, weights).items():
            if cents:
                want[(model, account)] = cents
    assert {k: v for k, v in got.items() if v} == want


def test_ties_go_to_the_lowest_account_id_and_a_pool_is_never_over_allocated(con):
    con.execute("CREATE OR REPLACE TEMP TABLE weights AS SELECT * FROM (VALUES "
                "('k', 'acct_0003', 1), ('k', 'acct_0001', 1), ('k', 'acct_0002', 1)) t(k, account_id, weight)")
    con.execute("CREATE OR REPLACE TEMP TABLE pools AS SELECT 'k' AS k, CAST(2 AS BIGINT) AS pool_cents")
    got = {k[1]: v for k, v in _apportioned(con, ["k"]).items()}
    assert got == {"acct_0001": 1, "acct_0002": 1, "acct_0003": 0}


def test_one_eligible_account_takes_the_whole_pool_and_negative_revenue_takes_none(con):
    con.execute("CREATE OR REPLACE TEMP TABLE facts AS SELECT * FROM (VALUES "
                "(DATE '2026-03-01', 'acct_0001', 500, 0, false), "
                "(DATE '2026-03-01', 'acct_0002', -900, 0, false)) t(month, account_id, revenue_cents, total_tokens, has_usage)")
    con.execute(f"CREATE OR REPLACE TEMP TABLE weights AS SELECT month, 'r' AS rule, account_id, "
                f"{macro('shared_weight', 'by_revenue')} AS weight FROM facts")
    con.execute("CREATE OR REPLACE TEMP TABLE pools AS SELECT DATE '2026-03-01' AS month, 'r' AS rule, CAST(77 AS BIGINT) AS pool_cents")
    assert {k[2]: v for k, v in _apportioned(con, ["month", "rule"]).items()} == {"acct_0001": 77}


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError, match="unknown allocation rule"):
        macro("shared_weight", "by_vibes")


# ------------------------------------------------------------------ contribution_cents()


def test_contribution_cents_is_the_reference_formula(con):
    expr = macro("contribution_cents", "r", "i", "d", "s", "u")
    (got,) = con.execute(f"SELECT {expr} FROM (SELECT 1000 AS r, 100 AS i, 20 AS d, 3 AS s, 7 AS u)").fetchone()
    assert got == ref.contribution_cents(1000, 100, 20, 3, 7) == 870


# ------------------------------------------------------------------ margin_pct and is_below


def _margin(con, profit: int, revenue: int):
    expr = macro("margin_pct_4dp", "p", "r")
    return con.execute(f"SELECT cast({expr} AS DECIMAL(9, 4)) FROM (SELECT CAST({profit} AS BIGINT) AS p, "
                       f"CAST({revenue} AS BIGINT) AS r)").fetchone()[0]


@pytest.mark.parametrize(
    "profit,revenue,expected",
    [
        (1, 3200, "0.0312"),    # 0.03125 exactly: half-even goes down to the even digit
        (3, 3200, "0.0938"),    # 0.09375 exactly: half-even goes up to the even digit
        (-1, 3200, "-0.0312"),  # the same on the negative side
        (-5, -3200, "0.1562"),  # a negative revenue flips the sign of the ratio
        (1, 3, "33.3333"),
        (2, 3, "66.6667"),
        (0, 500, "0.0000"),
    ],
)
def test_margin_pct_rounds_half_even_where_round_would_not(con, profit, revenue, expected):
    assert _margin(con, profit, revenue) == Decimal(expected)
    assert ref.margin_pct(profit, revenue) == Decimal(expected)


def test_margin_pct_is_null_at_zero_revenue(con):
    assert _margin(con, 40, 0) is None and ref.margin_pct(40, 0) is None


# Revenue magnitude of at least 10**7 cents keeps |margin| under 10**4 percent, inside DECIMAL(9, 4).
@SETTINGS
@given(profit=st.integers(-10**9, 10**9), revenue=st.integers(10**7, 10**11) | st.integers(-10**11, -10**7))
def test_margin_pct_matches_the_reference(con, profit, revenue):
    assert _margin(con, profit, revenue) == ref.margin_pct(profit, revenue)


def _below(con, profit: int, revenue: int, floor: str = "30") -> bool:
    expr = macro("is_below", "p", "r", floor)
    return con.execute(f"SELECT {expr} FROM (SELECT CAST({profit} AS BIGINT) AS p, CAST({revenue} AS BIGINT) AS r)"
                       ).fetchone()[0]


@pytest.mark.parametrize(
    "profit,revenue,floor,below",
    [
        (299, 1000, "30", True),    # one cent under the floor
        (300, 1000, "30", False),   # exactly on it: the test is strict
        (301, 1000, "30", False),
        (-5, 0, "30", True),        # cost with no revenue is below by arithmetic
        (0, 0, "30", False),
        (-100, -50, "30", True),    # negative revenue
        (3049, 10000, "30.5", True),
        (3050, 10000, "30.5", False),
    ],
)
def test_is_below_truth_table(con, profit, revenue, floor, below):
    assert _below(con, profit, revenue, floor) is below
    assert ref.below(profit, revenue, Decimal(floor)) is below


@SETTINGS
@given(profit=st.integers(-10**9, 10**9), revenue=st.integers(-10**9, 10**9))
def test_is_below_matches_the_reference(con, profit, revenue):
    assert _below(con, profit, revenue) is ref.below(profit, revenue, 30)


def test_a_rounded_percentage_would_flip_a_flag_that_the_predicate_keeps(con):
    # 29.99996% rounds to 30.0000 at four places but is below the floor by the integer predicate.
    profit, revenue = 2_999_996, 10_000_000
    assert _margin(con, profit, revenue) == Decimal("30.0000")
    assert _below(con, profit, revenue) is True
