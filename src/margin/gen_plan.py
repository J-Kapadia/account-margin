"""Who the accounts are and what each is planted to be in each month (plan §4.1, §7.2).

The plan is a table of roles. A role says what the books must make of an account-month; the
generator then tunes costs until the §4.1 predicate, evaluated by the Python reference, agrees.
Nothing here decides a flag: the planted set that reaches `truth/` is always derived.

Randomness comes only from `random.Random` seeded per entity and uses basic arithmetic, so the
output is byte-stable across platforms (no exp, log or pow).
"""

from __future__ import annotations

import calendar
import datetime
import random
from dataclasses import dataclass
from decimal import Decimal

from .errors import EXIT_USAGE, MarginError

DEFAULT_SEED = 20261013
DEFAULT_ACCOUNTS = 300
DEFAULT_MONTHS = 12
DEFAULT_FIRST_MONTH = "2025-10"

REASONS = ("DEEP_DISCOUNT", "MODEL_MIX", "SUPPORT_LOAD", "DEDICATED_CLOUD")
# Support rates the generator's own ledger uses. data/reference/support_rates.csv is the copy
# `close` reads; a test and a generator check keep the two equal, so GL 5300 and the tickets'
# cost are two independent figures that must agree (CTL-TIE-SUP).
SUPPORT_RATES = {"standard": 90, "priority": 150, "enterprise": 240}
TIER_OF_PLAN = {"usage": "standard", "committed": "priority", "enterprise": "enterprise"}

PLANS = ("usage", "committed", "enterprise")
# plan -> (monthly list revenue range in dollars, platform fee in cents)
PLAN_SHAPE = {
    "usage": ((1800, 4800), 9900),
    "committed": ((3500, 7500), 49900),
    "enterprise": ((6500, 12500), 99900),
}
ADJECTIVES = [
    "Alder", "Birch", "Cedar", "Dune", "Elm", "Fjord", "Garnet", "Harbor", "Iris", "Juniper",
    "Kestrel", "Linden", "Maple", "Northwind", "Opal", "Pine", "Quartz", "Rowan", "Sable", "Tundra",
]
NOUNS = [
    "Analytics", "Robotics", "Health Labs", "Logistics", "Media", "Energy", "Finance", "Foods",
    "Systems", "Networks", "Studios", "Biotech", "Mobility", "Retail", "Learning", "Security",
    "Dynamics",
]


def rng(seed: int, entity: str, purpose: str) -> random.Random:
    return random.Random(f"{seed}:{entity}:{purpose}")


def month_list(first: str, count: int) -> list[str]:
    y, m = int(first[:4]), int(first[5:])
    out = []
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def last_day(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:]))[1]


@dataclass(frozen=True)
class Role:
    """kind: normal | firm | decoy | zero_revenue | fee_only | negative_revenue."""

    kind: str
    reason: str | None = None

    @property
    def planted_reason(self) -> str | None:
        """The reason the classifier must reproduce; None for accounts that are not planted."""
        return {
            "firm": self.reason,
            "zero_revenue": "DEDICATED_CLOUD",
            "fee_only": "SUPPORT_LOAD",
            "negative_revenue": "MODEL_MIX",
        }.get(self.kind)

    @property
    def is_firm(self) -> bool:
        return self.kind in ("firm", "zero_revenue", "fee_only", "negative_revenue")


NORMAL = Role("normal")


@dataclass
class Spec:
    index: int
    account_id: str
    name: str
    plan: str
    discount: Decimal
    start_idx: int
    end_idx: int
    start_date: str
    base_cents: int
    growth: float
    f_in: float
    fee_cents: int
    dedicated_rate: float
    support_share: float
    credit_prob: float
    credit_always: bool = False

    @property
    def tier(self) -> str:
        return TIER_OF_PLAN[self.plan]

    def active(self, t: int) -> bool:
        return self.start_idx <= t <= self.end_idx


@dataclass
class Layout:
    seed: int
    preset: str | None
    months: list[str]
    specs: list[Spec]
    roles: dict[tuple[int, int], Role]

    def role(self, index: int, t: int) -> Role:
        return self.roles.get((index, t), NORMAL)


# Forced content of the presets, by 1-based account number: (plan, discount, {month index: role}).
# `mini` has one account of each injected-error target type (a credit line, an invoice, a
# revenue line, an untagged cloud row and a ledger entry exist in every month), a zero-revenue
# account with cost, a platform-fee-only account with cost, a negative-revenue account, and one
# account entering and one exiting the firm set between its second and third month.
_ALL3 = range(3)
PRESETS = {
    "mini": {"accounts": 12, "first_month": "2026-02", "months": 3},
    "scenario": {"accounts": 3, "first_month": "2026-03", "months": 1},
}
_PRESET_ROLES = {
    "mini": {
        2: ("usage", "0.00", {}),
        6: ("committed", "0.00", {t: Role("firm", "MODEL_MIX") for t in _ALL3}),
        7: ("usage", "0.00", {2: Role("firm", "SUPPORT_LOAD")}),
        8: ("usage", "0.40", {t: Role("firm", "DEEP_DISCOUNT") for t in (0, 1)}),
        9: ("usage", "0.00", {t: Role("decoy") for t in _ALL3}),
        10: ("usage", "0.00", {t: Role("zero_revenue") for t in _ALL3}),
        11: ("committed", "0.00", {t: Role("fee_only") for t in _ALL3}),
        12: ("usage", "0.00", {t: Role("negative_revenue") for t in _ALL3}),
    },
    "scenario": {
        1: ("usage", "0.00", {0: Role("firm", "MODEL_MIX")}),
        2: ("committed", "0.00", {0: Role("firm", "SUPPORT_LOAD")}),
        3: ("usage", "0.00", {0: Role("firm", "DEDICATED_CLOUD")}),
    },
}
# Accounts that get a credit line in every month of `mini` (the sign-flip target).
MINI_CREDIT_ACCOUNTS = (2,)


def _names(seed: int, count: int) -> list[str]:
    pairs = [f"{a} {n}" for a in ADJECTIVES for n in NOUNS]
    rng(seed, "names", "shuffle").shuffle(pairs)
    names = pairs[:count]
    names += [f"Account {i}" for i in range(len(names) + 1, count + 1)]
    return names


def _start_date(r: random.Random, first_month: str, start_idx: int, months: list[str]) -> str:
    if start_idx > 0:
        return f"{months[start_idx]}-{r.randrange(1, 28):02d}"
    first = datetime.date(int(first_month[:4]), int(first_month[5:]), 1)
    return (first - datetime.timedelta(days=r.randrange(15, 500))).isoformat()


def build_layout(
    seed: int, n_accounts: int, first_month: str, n_months: int, preset: str | None
) -> Layout:
    if preset is None and n_accounts < 40:
        raise MarginError(
            EXIT_USAGE, "the full books need at least 40 accounts to plant firm flags and decoys"
        )
    months = month_list(first_month, n_months)
    names = _names(seed, n_accounts)
    forced = _PRESET_ROLES.get(preset or "", {})
    roles: dict[tuple[int, int], Role] = {}

    # Planted accounts of the full books: a rotating half of each pool is flagged in a month,
    # so every month has the same count and one account enters and one exits at each step.
    firm_pool = max(2, 2 * round(n_accounts * 0.04))
    decoy_pool = max(2, 2 * round(n_accounts * 0.027))
    order = list(range(n_accounts))
    rng(seed, "planting", "order").shuffle(order)
    firm_ids = order[:firm_pool]
    decoy_ids = order[firm_pool:firm_pool + decoy_pool]
    planted_firm = {} if preset else {idx: k for k, idx in enumerate(firm_ids)}
    planted_decoy = {} if preset else {idx: k for k, idx in enumerate(decoy_ids)}
    for idx, k in planted_firm.items():
        for t in range(n_months):
            if (t + k) % firm_pool < firm_pool // 2:
                roles[(idx, t)] = Role("firm", REASONS[k % 4])
    for idx, k in planted_decoy.items():
        for t in range(n_months):
            if (t + k) % decoy_pool < decoy_pool // 2:
                roles[(idx, t)] = Role("decoy")

    specs = []
    for idx in range(n_accounts):
        account_id = f"acct_{idx + 1:04d}"
        r = rng(seed, account_id, "profile")
        draws = [r.random() for _ in range(12)]
        plan = PLANS[0 if draws[0] < 0.60 else 1 if draws[0] < 0.85 else 2]
        discount = Decimal(0) if draws[1] < 0.62 else Decimal(5 * (1 + int(draws[2] * 4))) / 100
        start_idx, end_idx = 0, n_months - 1
        late = not preset and idx not in planted_firm and idx not in planted_decoy
        if late and draws[3] < 0.12 and n_months > 2:
            start_idx = 1 + int(draws[4] * (n_months - 2))
        elif late and draws[3] > 0.96 and n_months > 3:
            end_idx = n_months // 2 + int(draws[4] * (n_months - 1 - n_months // 2))
        if idx in planted_decoy:
            plan = "usage"
        if idx in planted_firm:
            k = planted_firm[idx]
            discount = Decimal(35 + int(draws[2] * 16)) / 100 if k % 4 == 0 else Decimal(0)
        number = idx + 1
        if number in forced:
            plan, forced_discount, forced_roles = forced[number]
            discount = Decimal(forced_discount)
            for t, role in forced_roles.items():
                if t < n_months:
                    roles[(idx, t)] = role
        (lo, hi), fee = PLAN_SHAPE[plan]
        specs.append(Spec(
            index=idx, account_id=account_id, name=names[idx], plan=plan, discount=discount,
            start_idx=start_idx, end_idx=end_idx,
            start_date=_start_date(r, first_month, start_idx, months),
            base_cents=int((lo + (hi - lo) * draws[5]) * 100),
            growth=-0.01 + 0.03 * draws[6],
            f_in=0.65 + 0.20 * draws[7],
            fee_cents=fee,
            dedicated_rate=(0.010 + 0.015 * draws[9]) if draws[8] < 0.10 else 0.0,
            support_share=0.010 + 0.035 * draws[10],
            credit_prob=0.08 if draws[11] < 0.5 else 0.0,
            credit_always=preset == "mini" and number in MINI_CREDIT_ACCOUNTS,
        ))
    return Layout(seed=seed, preset=preset, months=months, specs=specs, roles=roles)
