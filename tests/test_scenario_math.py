"""The token-price scenario's arithmetic: blend, change, rate, factor and cent scaling."""

from __future__ import annotations

import ast
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path

import pytest

from margin import scenario
from margin.scenario import (
    CASES,
    PERIODS,
    QUARTER_END_K,
    TIERS,
    blend,
    computed_change,
    factor,
    monthly_rate,
    quarter_of,
    read_series,
    scaled_cents,
    tier_price,
    validate_series,
)

ROOT = Path(__file__).resolve().parents[1]
SERIES = ROOT / "data" / "reference" / "list_price_series.csv"
CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
COMMITTED_CHANGE = Decimal("-0.0185874189687780539016770914")


def _row(tier, period, inp, out, source="https://example.test/p", retrieved="2026-10-18"):
    return {
        "tier": tier, "model": "m", "period": period,
        "input_usd_per_mtok": inp, "output_usd_per_mtok": out,
        "source_url": source, "retrieved_on": retrieved, "notes": "",
    }


def _fixture_rows():
    """Three tiers, two periods, worked by hand; the other periods repeat the second.

    A tier's price is (3 × input + output) / 4.
      2026-03: large (4.00, 8.00) -> 20 / 4 = 5.00; medium (2.00, 6.00) -> 12 / 4 = 3.00;
               small (1.00, 3.00) -> 6 / 4 = 1.50. Mean 9.50 / 3 = 3.1666666..., six places 3.166667.
      2026-09: large (3.00, 7.00) -> 16 / 4 = 4.00; medium (2.00, 6.00) -> 3.00;
               small (0.50, 2.50) -> 4 / 4 = 1.00. Mean 8.00 / 3 = 2.6666666..., six places 2.666667.
    """
    first = {"large": ("4.00", "8.00"), "medium": ("2.00", "6.00"), "small": ("1.00", "3.00")}
    last = {"large": ("3.00", "7.00"), "medium": ("2.00", "6.00"), "small": ("0.50", "2.50")}
    return [
        _row(t, p, *(first if p == PERIODS[0] else last)[t])
        for t in TIERS
        for p in PERIODS
    ]


@pytest.fixture(scope="module")
def committed():
    return read_series(SERIES)


def test_a_tier_price_is_three_parts_input_to_one_part_output():
    assert tier_price(_row("large", "2026-03", "4.00", "8.00")) == Decimal("5")
    assert tier_price(_row("medium", "2026-03", "1.75", "14.00")) == Decimal("4.8125")


def test_the_blend_of_the_hand_worked_fixture():
    rows = _fixture_rows()
    assert blend(rows, PERIODS[0]) == Decimal("3.166667")
    assert blend(rows, PERIODS[-1]) == Decimal("2.666667")


def test_the_blend_names_a_period_with_a_missing_tier():
    rows = [r for r in _fixture_rows() if not (r["tier"] == "small" and r["period"] == "2026-05")]
    with pytest.raises(ValueError, match="2026-05.*small"):
        blend(rows, "2026-05")


def test_the_fixture_change_is_last_blend_over_first_minus_one():
    change = computed_change(_fixture_rows())
    assert abs((change + 1) * Decimal("3.166667") - Decimal("2.666667")) < Decimal("1e-20")
    assert change == CTX.subtract(CTX.divide(Decimal("2.666667"), Decimal("3.166667")), Decimal(1))


def test_the_committed_file_reproduces_the_hand_worked_blends_and_change(committed):
    assert blend(committed, "2026-03") == Decimal("5.604167")
    assert blend(committed, "2026-09") == Decimal("5.5")
    assert computed_change(committed) == COMMITTED_CHANGE


def test_the_committed_file_is_valid(committed):
    assert validate_series(committed) == []


def test_the_monthly_rate_compounds_to_the_computed_change(committed):
    r = monthly_rate(committed)
    assert r < 0
    assert abs((1 + r) ** 6 - 1 - computed_change(committed)) < Decimal("1e-20")


def test_the_rate_is_held_at_twenty_eight_digits(committed):
    r = monthly_rate(committed)
    assert len(r.as_tuple().digits) <= 28


def test_a_half_persistence_halves_the_monthly_rate_not_the_cumulative_decline(committed):
    r = monthly_rate(committed)
    half = factor(Decimal("0.5"), r, 6)
    halved_cumulative = 1 + Decimal("0.5") * ((1 + r) ** 6 - 1)
    assert half != halved_cumulative
    assert abs(half - (1 + r / 2) ** 6) < Decimal("1e-20")


def test_the_factor_at_full_persistence_is_the_compounded_rate(committed):
    r = monthly_rate(committed)
    assert abs(factor(CASES["full"], r, 6) - (1 + computed_change(committed))) < Decimal("1e-20")


def test_no_persistence_leaves_cost_unchanged(committed):
    r = monthly_rate(committed)
    assert factor(CASES["flat"], r, 12) == Decimal(1)


def test_the_cases_are_full_half_and_flat():
    assert CASES == {"full": Decimal(1), "half": Decimal("0.5"), "flat": Decimal(0)}
    assert all(isinstance(v, Decimal) for v in CASES.values())


def test_scaled_cents_rounds_exact_halves_to_even():
    assert scaled_cents(1000, Decimal("1.0005")) == 1000  # 1000.5 -> 1000
    assert scaled_cents(1000, Decimal("1.0015")) == 1002  # 1001.5 -> 1002
    assert scaled_cents(1000, Decimal("1.0004")) == 1000  # 1000.4, not a half
    assert scaled_cents(1000, Decimal("1.0006")) == 1001  # 1000.6, not a half
    assert scaled_cents(0, Decimal("0.9")) == 0


def test_scaled_cents_returns_an_int():
    assert type(scaled_cents(1000, Decimal("0.99"))) is int


def test_quarters_are_consecutive_three_month_blocks():
    assert [quarter_of(k) for k in range(1, 13)] == [
        "Q1", "Q1", "Q1", "Q2", "Q2", "Q2", "Q3", "Q3", "Q3", "Q4", "Q4", "Q4",
    ]
    assert QUARTER_END_K == (3, 6, 9, 12)
    assert [quarter_of(k) for k in QUARTER_END_K] == ["Q1", "Q2", "Q3", "Q4"]


@pytest.mark.parametrize("k", [0, 13, -1])
def test_a_month_outside_the_horizon_has_no_quarter(k):
    with pytest.raises(ValueError):
        quarter_of(k)


def test_periods_and_tiers():
    assert PERIODS == ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09")
    assert TIERS == ("large", "medium", "small")


def test_validate_series_names_a_missing_period():
    rows = [r for r in _fixture_rows() if not (r["tier"] == "medium" and r["period"] == "2026-06")]
    problems = validate_series(rows)
    assert problems == ["medium 2026-06: missing row"]


def test_validate_series_names_a_missing_source():
    rows = _fixture_rows()
    rows[0]["source_url"] = ""
    rows[1]["retrieved_on"] = " "
    problems = validate_series(rows)
    assert problems == [
        f"{rows[0]['tier']} {rows[0]['period']}: empty source_url",
        f"{rows[1]['tier']} {rows[1]['period']}: empty retrieved_on",
    ]


def test_validate_series_names_a_three_decimal_price():
    rows = _fixture_rows()
    rows[2]["output_usd_per_mtok"] = "7.125"
    problems = validate_series(rows)
    assert len(problems) == 1
    assert "output_usd_per_mtok" in problems[0] and "7.125" in problems[0]
    assert problems[0].startswith(f"{rows[2]['tier']} {rows[2]['period']}:")


def test_validate_series_rejects_a_price_that_is_not_a_number():
    rows = _fixture_rows()
    rows[0]["input_usd_per_mtok"] = "cheap"
    assert len(validate_series(rows)) == 1


def test_validate_series_names_a_duplicate():
    rows = _fixture_rows()
    rows.append(dict(rows[0]))
    assert validate_series(rows) == [f"{rows[0]['tier']} {rows[0]['period']}: duplicate row (2 rows)"]


def test_the_module_reaches_nothing_else_in_the_package():
    tree = ast.parse(Path(scenario.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("margin"), ast.dump(node)
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("margin") for a in node.names)
