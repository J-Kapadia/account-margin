"""Token-price scenario arithmetic: the list-price series, its blended trend, and the scaling of cost.

All arithmetic is exact `Decimal` under one local context (28 digits, round half to even); nothing
here touches a float. Only `read_series` does I/O.
"""

from __future__ import annotations

import csv
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path

TIERS = ("large", "medium", "small")
PERIODS = tuple(f"2026-{m:02d}" for m in range(3, 10))
CASES = {"full": Decimal(1), "half": Decimal("0.5"), "flat": Decimal(0)}
QUARTER_END_K = (3, 6, 9, 12)

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
_SIX_PLACES = Decimal("0.000001")
_ONE = Decimal(1)
_MONTHS_BETWEEN_ENDS = len(PERIODS) - 1
_PRICE_COLUMNS = ("input_usd_per_mtok", "output_usd_per_mtok")


def read_series(path: Path | str) -> list[dict]:
    """The rows of the list-price series file, one dict per row, values as text."""
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _has_two_decimals(text: str) -> bool:
    try:
        value = Decimal(text)
    except ArithmeticError:
        return False
    return value.is_finite() and value.as_tuple().exponent == -2


def validate_series(rows: list[dict]) -> list[str]:
    """One problem string per missing tier/period, duplicate, empty citation or malformed price."""
    problems: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.get("tier", ""), row.get("period", ""))
        seen[key] = seen.get(key, 0) + 1
        where = f"{key[0]} {key[1]}"
        for column in ("source_url", "retrieved_on"):
            if not (row.get(column) or "").strip():
                problems.append(f"{where}: empty {column}")
        for column in _PRICE_COLUMNS:
            text = row.get(column) or ""
            if not _has_two_decimals(text):
                problems.append(f"{where}: {column} {text!r} does not have exactly two decimals")
    for tier in TIERS:
        for period in PERIODS:
            count = seen.get((tier, period), 0)
            if count == 0:
                problems.append(f"{tier} {period}: missing row")
            elif count > 1:
                problems.append(f"{tier} {period}: duplicate row ({count} rows)")
    return problems


def tier_price(row: dict) -> Decimal:
    """A tier's price in a period: (3 × input + output) ÷ 4, per million tokens."""
    inp = Decimal(row["input_usd_per_mtok"])
    out = Decimal(row["output_usd_per_mtok"])
    return _CTX.divide(_CTX.add(_CTX.multiply(Decimal(3), inp), out), Decimal(4))


def blend(rows: list[dict], period: str) -> Decimal:
    """The period's blended price: the unweighted mean of the three tiers, six places, half-even."""
    by_tier = {r["tier"]: r for r in rows if r["period"] == period}
    missing = [t for t in TIERS if t not in by_tier]
    if missing:
        raise ValueError(f"period {period} has no row for tier {', '.join(missing)}")
    total = Decimal(0)
    for tier in TIERS:
        total = _CTX.add(total, tier_price(by_tier[tier]))
    return _CTX.divide(total, Decimal(len(TIERS))).quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN)


def _ratio(rows: list[dict]) -> Decimal:
    return _CTX.divide(blend(rows, PERIODS[-1]), blend(rows, PERIODS[0]))


def computed_change(rows: list[dict]) -> Decimal:
    """The blended price of the last period over that of the first, minus one."""
    return _CTX.subtract(_ratio(rows), _ONE)


def monthly_rate(rows: list[dict]) -> Decimal:
    """The constant monthly rate that compounds to the computed change over the series."""
    root = _CTX.power(_ratio(rows), _CTX.divide(_ONE, Decimal(_MONTHS_BETWEEN_ENDS)))
    return _CTX.subtract(root, _ONE)


def factor(persistence: Decimal, rate: Decimal, k: int) -> Decimal:
    """The cost factor k months after the base month: (1 + persistence × rate)^k."""
    return _CTX.power(_CTX.add(_ONE, _CTX.multiply(persistence, rate)), k)


def scaled_cents(base_cents: int, f: Decimal) -> int:
    """Base cents times a factor, rounded half-even to whole cents."""
    return int(_CTX.multiply(Decimal(base_cents), f).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def quarter_of(k: int) -> str:
    """The quarter (`Q1`..`Q4`) of the k-th month after the base month, k = 1..12."""
    if not 1 <= k <= QUARTER_END_K[-1]:
        raise ValueError(f"k must be 1..{QUARTER_END_K[-1]}, got {k}")
    return f"Q{(k - 1) // 3 + 1}"
