"""The list-price series file: its shape, its citations, and the blended change it implies."""

from __future__ import annotations

import csv
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from itertools import groupby
from pathlib import Path

import pytest

SERIES = Path(__file__).resolve().parents[1] / "data" / "reference" / "list_price_series.csv"
COLUMNS = [
    "tier", "model", "period", "input_usd_per_mtok", "output_usd_per_mtok",
    "source_url", "retrieved_on", "notes",
]
TIERS = ["large", "medium", "small"]
PERIODS = [f"2026-{m:02d}" for m in range(3, 10)]
VENDOR_PAGE = {
    "large": "https://platform.claude.com/docs/en/about-claude/pricing",
    "medium": "https://developers.openai.com/api/docs/pricing",
    "small": "https://platform.claude.com/docs/en/about-claude/pricing",
}

# Worked by hand from the rows. A tier's price is (3 × input + output) / 4:
#   large  10.00 in every month        (5.00 in, 25.00 out)
#   small   2.00 in every month        (1.00 in,  5.00 out)
#   medium  4.8125 in 2026-03 (gpt-5.2: 1.75 in, 14.00 out) and
#           4.50   in 2026-09 (gpt-5.6-terra: 2.00 in, 12.00 out)
# The blend is the mean of the three: (10 + 4.8125 + 2) / 3 = 5.604166..., rounded half-even to six
# places 5.604167, and (10 + 4.5 + 2) / 3 = 5.5 exactly. The change is 5.5 / 5.604167 − 1
# = −0.104167 / 5.604167 = −0.018587..., carried to 28 digits below.
BLEND_FIRST = Decimal("5.604167")
BLEND_LAST = Decimal("5.5")
CHANGE = Decimal("-0.0185874189687780539016770914")


@pytest.fixture(scope="module")
def rows():
    with SERIES.open(newline="") as f:
        return list(csv.DictReader(f))


def _price(text: str) -> Decimal:
    value = Decimal(text)
    assert value.as_tuple().exponent == -2, text
    assert value * 1000 == (value * 1000).to_integral_value(), text
    return value


def _blend(rows, period: str) -> Decimal:
    by_tier = {r["tier"]: r for r in rows if r["period"] == period}
    prices = [(3 * _price(by_tier[t]["input_usd_per_mtok"]) + _price(by_tier[t]["output_usd_per_mtok"])) / 4 for t in TIERS]
    return (sum(prices) / 3).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def test_the_file_has_exactly_these_columns():
    with SERIES.open(newline="") as f:
        assert next(csv.reader(f)) == COLUMNS


def test_every_tier_and_period_appears_once(rows):
    assert len(rows) == 21
    assert sorted((r["tier"], r["period"]) for r in rows) == sorted((t, p) for t in TIERS for p in PERIODS)


def test_prices_have_two_places_and_whole_nano_usd(rows):
    for r in rows:
        _price(r["input_usd_per_mtok"])
        _price(r["output_usd_per_mtok"])


def test_every_row_cites_an_archived_capture_of_the_vendor_page(rows):
    for r in rows:
        assert r["source_url"].startswith("https://web.archive.org/web/"), r["source_url"]
        assert VENDOR_PAGE[r["tier"]] in r["source_url"], r["source_url"]


def test_retrieved_on_is_an_iso_date(rows):
    for r in rows:
        assert date.fromisoformat(r["retrieved_on"]).isoformat() == r["retrieved_on"]


def test_a_tier_never_returns_to_an_earlier_model(rows):
    for tier, group in groupby(sorted(rows, key=lambda r: (r["tier"], r["period"])), key=lambda r: r["tier"]):
        models = [r["model"] for r in group]
        sequence = [m for i, m in enumerate(models) if i == 0 or m != models[i - 1]]
        assert len(sequence) == len(set(sequence)), (tier, models)


def test_the_blended_change_equals_the_hand_worked_value(rows):
    getcontext().prec = 28
    getcontext().rounding = ROUND_HALF_EVEN
    first, last = _blend(rows, PERIODS[0]), _blend(rows, PERIODS[-1])
    assert (first, last) == (BLEND_FIRST, BLEND_LAST)
    assert last / first - 1 == CHANGE
