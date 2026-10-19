"""The generator's independent Python reference.

This is the one place outside the dbt macros that applies the contribution formula and the
largest-remainder apportionment. The generator's truth writer and the tests use it; `close`,
`memo` and `agreement` never import it (an import-graph test enforces that), so "components
equal truth" compares two implementations. It imports nothing else from `margin`.

Money is integer cents. Prices are integer nano-USD per token, so every weight is an exact
integer. Decimals are used only for the ratios of the reason-code classifier and are rounded
half-even to nine places before they are compared.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

NANO_PER_CENT = 10_000_000  # 1 cent = 10^7 nano-USD
Q4 = Decimal("0.0001")
Q9 = Decimal("0.000000001")
REASON_NONE = "NONE"
# Ties between equal cents when a zero-revenue account is classified by cost.
COST_TIE_ORDER = ("MODEL_MIX", "SUPPORT_LOAD", "DEDICATED_CLOUD")


# ---------------------------------------------------------------- money primitives


def apportion(total_cents: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Largest-remainder apportionment; ties go to the ascending key. Weights <= 0 get nothing.

    A positive total with no eligible weight is an error: there is no fallback.
    """
    if total_cents < 0:
        raise ValueError("a pool cannot be negative")
    out = {key: 0 for key in weights}
    if total_cents == 0:
        return out
    eligible = {key: w for key, w in weights.items() if w > 0}
    total_weight = sum(eligible.values())
    if total_weight == 0:
        raise ValueError(f"pool of {total_cents} cents has no eligible weight")
    remainders = {}
    for key, w in eligible.items():
        out[key], remainders[key] = divmod(total_cents * w, total_weight)
    left = total_cents - sum(out.values())
    for key in sorted(eligible, key=lambda k: (-remainders[k], k))[:left]:
        out[key] += 1
    return out


def contribution_cents(revenue: int, inference: int, cloud_direct: int, cloud_shared: int,
                       support: int) -> int:
    return revenue - inference - cloud_direct - cloud_shared - support


def below(profit_cents: int, revenue_cents: int, floor_pct: Decimal | int) -> bool:
    """The flag predicate: strict, all integers, never a rounded percentage."""
    return Fraction(profit_cents * 100) < Fraction(Decimal(floor_pct)) * revenue_cents


def margin_points(profit_cents: int, revenue_cents: int) -> Fraction:
    """Exact margin in percentage points; revenue must be positive."""
    return Fraction(profit_cents * 100, revenue_cents)


def margin_pct(profit_cents: int, revenue_cents: int) -> Decimal | None:
    """Display margin, DECIMAL(9,4) half-even; None at zero revenue (rendered `n/a`)."""
    if revenue_cents == 0:
        return None
    return (Decimal(profit_cents) * 100 / Decimal(revenue_cents)).quantize(Q4, ROUND_HALF_EVEN)


def lower_median(values: Sequence[Decimal]) -> Decimal:
    """The value at 0-based index (n-1)//2 of the sorted values; always a value in the data."""
    if not values:
        return Decimal(0)
    return sorted(values)[(len(values) - 1) // 2]


def nearest_rank(values: Sequence[Decimal | int], p: Decimal) -> Decimal | int:
    """Percentile p in (0, 1] by nearest rank: the value at 1-based rank ceil(p*n)."""
    ordered = sorted(values)
    rank = int((p * len(ordered)).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(rank, 1) - 1]


# ---------------------------------------------------------------- one month of an account


@dataclass
class AcctMonth:
    """What the books say about one account in one month (integer cents)."""

    account_id: str
    discount: Decimal = Decimal(0)
    fee_cents: int = 0
    usage_cents: int = 0
    credit_cents: int = 0
    tokens: dict[str, tuple[int, int]] = field(default_factory=dict)  # model -> (input, output)
    direct_cents: int = 0
    support_cents: int = 0

    @property
    def revenue_cents(self) -> int:
        return self.fee_cents + self.usage_cents + self.credit_cents

    @property
    def has_usage(self) -> bool:
        return bool(self.tokens)

    @property
    def total_tokens(self) -> int:
        return sum(i + o for i, o in self.tokens.values())


@dataclass
class Components:
    revenue: int
    inference: int
    direct: int
    support: int
    shared: dict[str, int]
    profit: dict[str, int]
    below: dict[str, bool]

    @property
    def rules_below(self) -> int:
        return sum(self.below.values())

    @property
    def flag(self) -> str:
        n = self.rules_below
        return "firm" if n == len(self.below) else "soft" if n else "none"


def shared_weights(rule: str, accts: Mapping[str, AcctMonth]) -> dict[str, int]:
    """The apportionment weight of each account for a shared-cloud rule."""
    if rule == "by_tokens":
        return {k: a.total_tokens for k, a in accts.items()}
    if rule == "by_revenue":
        return {k: max(a.revenue_cents, 0) for k, a in accts.items()}
    if rule == "equal_usage":
        return {k: 1 if a.has_usage else 0 for k, a in accts.items()}
    raise ValueError(f"unknown allocation rule {rule!r}")


def month_components(
    accts: Mapping[str, AcctMonth],
    provider_cents: Mapping[str, int],
    nano_prices: Mapping[str, tuple[int, int]],
    pool_cents: int,
    rules: Sequence[str],
    floor_pct: Decimal | int,
) -> dict[str, Components]:
    """Every account's cost components and below-floor result under every rule."""
    inference: dict[str, int] = defaultdict(int)
    models = sorted({m for a in accts.values() for m in a.tokens} | set(provider_cents))
    for model in models:
        if model not in nano_prices:
            raise ValueError(f"model {model!r} has no pinned price")
        nano_in, nano_out = nano_prices[model]
        weights = {k: a.tokens[model][0] * nano_in + a.tokens[model][1] * nano_out
                   for k, a in accts.items() if model in a.tokens}
        if model not in provider_cents:
            raise ValueError(f"model {model!r} has usage but no provider invoice")
        for key, cents in apportion(provider_cents[model], weights).items():
            inference[key] += cents
    allocated = {rule: apportion(pool_cents, shared_weights(rule, accts)) for rule in rules}
    out = {}
    for key, a in accts.items():
        shared = {rule: allocated[rule][key] for rule in rules}
        profit = {rule: contribution_cents(a.revenue_cents, inference[key], a.direct_cents,
                                           shared[rule], a.support_cents) for rule in rules}
        out[key] = Components(
            revenue=a.revenue_cents, inference=inference[key], direct=a.direct_cents,
            support=a.support_cents, shared=shared, profit=profit,
            below={rule: below(profit[rule], a.revenue_cents, floor_pct) for rule in rules},
        )
    return out


# ---------------------------------------------------------------- reason-code classifier


def list_price_revenue(fee_cents: int, usage_cents: int, discount: Decimal) -> Decimal:
    """Platform fee plus usage restored to list price; credits are not added back."""
    return Decimal(fee_cents) + Decimal(usage_cents) / (1 - discount)


def _ratio(numerator: int | Decimal, denominator: Decimal) -> Decimal:
    return (Decimal(numerator) / denominator).quantize(Q9, ROUND_HALF_EVEN)


def driver_values(a: AcctMonth, c: Components) -> dict[str, Decimal]:
    """The four drivers of an account with positive revenue, DECIMAL(18,9) half-even."""
    list_revenue = list_price_revenue(a.fee_cents, a.usage_cents, a.discount)
    return {
        "DEEP_DISCOUNT": a.discount.quantize(Q9, ROUND_HALF_EVEN),
        "MODEL_MIX": _ratio(c.inference, list_revenue),
        "SUPPORT_LOAD": _ratio(c.support, list_revenue),
        "DEDICATED_CLOUD": _ratio(c.direct, list_revenue),
    }


@dataclass(frozen=True)
class Classification:
    code: str
    multiples: dict[str, Decimal]  # empty when the cents rule was used
    gap: Decimal | None  # winner's multiple minus the runner-up's; None for the cents rule


def classify_month(
    accts: Mapping[str, AcctMonth],
    comps: Mapping[str, Components],
    drivers: Sequence[tuple[str, Decimal]],
) -> dict[str, Classification]:
    """Reason code for every account. `drivers` is (code, median_floor) in tie order."""
    values = {k: driver_values(a, comps[k]) for k, a in accts.items() if comps[k].revenue > 0}
    cohort = [k for k, a in accts.items() if a.has_usage and comps[k].revenue > 0]
    denominators = {
        code: max(lower_median([values[k][code] for k in cohort]), floor) for code, floor in drivers
    }
    out = {}
    for key, a in accts.items():
        c = comps[key]
        if c.revenue <= 0:
            costs = {"MODEL_MIX": c.inference, "SUPPORT_LOAD": c.support, "DEDICATED_CLOUD": c.direct}
            best = max(costs.values())
            code = next((k for k in COST_TIE_ORDER if costs[k] == best), REASON_NONE) if best else REASON_NONE
            out[key] = Classification(code, {}, None)
            continue
        multiples = {code: _ratio(values[key][code], denominators[code]) for code, _ in drivers}
        top = max(multiples.values())
        if top <= 1:
            out[key] = Classification(REASON_NONE, multiples, None)
            continue
        order = [code for code, _ in drivers]
        winner = next(code for code in order if multiples[code] == top)
        rest = [m for code, m in multiples.items() if code != winner]
        out[key] = Classification(winner, multiples, top - max(rest) if rest else top)
    return out


# ---------------------------------------------------------------- recomputing from the books


@dataclass
class MonthResult:
    accts: dict[str, AcctMonth]
    comps: dict[str, Components]
    pool_cents: int
    provider_cents: dict[str, int]
    reasons: dict[str, Classification]


def read_nano_prices(path: Path) -> dict[str, tuple[int, int]]:
    out = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            out[rec["model"]] = (int(Decimal(rec["input_per_mtok"]) * 1000),
                                 int(Decimal(rec["output_per_mtok"]) * 1000))
    return out


def read_rates(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8", newline="") as fh:
        return {rec["tier"]: int(rec["cents_per_minute"]) for rec in csv.DictReader(fh)}


def _rows(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def _cents(amount: str) -> int:
    value = Decimal(amount) * 100
    if value != int(value):
        raise ValueError(f"{amount} is not a whole number of cents")
    return int(value)


def recompute_from_books(
    books: Path,
    prices_csv: Path,
    rates_csv: Path,
    rules: Sequence[str],
    floor_pct: Decimal | int,
    drivers: Sequence[tuple[str, Decimal]],
) -> dict[str, MonthResult]:
    """Re-derive every month's components, flags and reasons from the books' CSV files alone."""
    discount = {r["account_id"]: Decimal(r["discount_pct"]) for r in _rows(books / "accounts.csv")}
    rates = read_rates(rates_csv)
    nano = read_nano_prices(prices_csv)
    accts: dict[str, dict[str, AcctMonth]] = defaultdict(dict)

    def acct(month: str, account_id: str) -> AcctMonth:
        return accts[month].setdefault(account_id, AcctMonth(account_id, discount[account_id]))

    for r in _rows(books / "invoices.csv"):
        a = acct(r["service_period"], r["account_id"])
        cents = int(r["amount_cents"])
        if r["line_type"] == "platform_fee":
            a.fee_cents += cents
        elif r["line_type"] == "usage":
            a.usage_cents += cents
        else:
            a.credit_cents += cents
    for r in _rows(books / "usage_monthly.csv"):
        a = acct(r["month"], r["account_id"])
        i, o = a.tokens.get(r["model"], (0, 0))
        a.tokens[r["model"]] = (i + int(r["input_tokens"]), o + int(r["output_tokens"]))
    pools: dict[str, int] = defaultdict(int)
    for r in _rows(books / "cloud_focus.csv"):
        month = r["ChargePeriodStart"][:7]
        tenant = json.loads(r["Tags"] or "{}").get("tenant")
        if tenant and tenant != "shared":
            acct(month, tenant).direct_cents += _cents(r["EffectiveCost"])
        else:
            pools[month] += _cents(r["EffectiveCost"])
    for r in _rows(books / "support_tickets.csv"):
        if r["closed_date"]:
            a = acct(r["closed_date"][:7], r["account_id"])
            a.support_cents += int(r["handle_minutes"]) * rates[r["tier"]]
    provider: dict[str, dict[str, int]] = defaultdict(dict)
    for r in _rows(books / "provider_invoices.csv"):
        provider[r["month"]][r["model"]] = int(r["amount_cents"])

    results = {}
    for month in sorted(accts):
        comps = month_components(accts[month], provider[month], nano, pools[month], rules, floor_pct)
        results[month] = MonthResult(
            accts[month], comps, pools[month], dict(provider[month]),
            classify_month(accts[month], comps, drivers),
        )
    return results
