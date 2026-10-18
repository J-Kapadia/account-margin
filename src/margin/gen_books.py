"""The books generator proper: accounts, invoices, usage, cloud, support, ledger.

Each month is built in three steps. First every account gets a volume of usage, a platform fee
and a role-shaped mix of models. Then the shared-cloud pool and the three allocations are
evaluated with the Python reference and the free costs (direct cloud, support minutes, platform
fee, decoy size) are tuned until every account-month sits where its role needs it, relative to
the floor and the guard bands. Last the books are written and the whole thing is recomputed from
the written files by the same reference: the truth is that recomputation, and the planted set is
derived by evaluating the flag predicate, never listed by hand.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path

from . import truth_reference as ref
from .errors import EXIT_INTERNAL, MarginError
from .gen_config import Config, Prices
from .gen_plan import SUPPORT_RATES, Layout, Role, Spec, last_day, rng

BOOKS_FILES = (
    "accounts.csv", "invoices.csv", "usage_monthly.csv", "provider_invoices.csv",
    "cloud_focus.csv", "support_tickets.csv", "gl.csv", "window.json",
)
TRUTH_FILES = (
    "usage_by_model.csv", "list_prices.csv", "discounts.csv", "support_load.csv",
    "planted_flags.json", "allowed_rules.json", "cost_components.csv", "rule_flags.csv",
)
INVOICE_COLS = [
    "invoice_id", "line_id", "account_id", "service_period", "gl_period", "issued_date",
    "line_type", "model", "quantity_tokens", "unit_price", "amount_cents",
]
FOCUS_COLS = [
    "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency",
    "ServiceName", "ServiceCategory", "ResourceId", "Tags", "line_id",
]
TICKET_COLS = ["ticket_id", "account_id", "opened_date", "closed_date", "handle_minutes", "tier"]
# Untagged shared services: (name, FOCUS category, resource, weight, emitted daily)
SHARED_SERVICES = [
    ("Managed API Gateway", "Networking", "res-gateway-01", 35, False),
    ("Observability Platform", "Management and Governance", "res-observe-01", 25, True),
    ("Object Storage", "Storage", "res-storage-01", 15, False),
    ("Shared Compute", "Compute", "res-shared-01", 25, False),
]
TAGGED_SERVICES = [
    ("Dedicated Inference Cluster", "Compute", "res-dedicated-{acct}", 70),
    ("Dedicated Network Egress", "Networking", "res-egress-{acct}", 30),
]
SHARED_POOL_FRACTION = Decimal("0.10")  # of the month's provider inference cost
MAX_ITERATIONS = 40
MAX_SHRINKS = 10


class GenerationError(MarginError):
    """The plan could not be realised: a bug in the generator or an unusable price list."""

    def __init__(self, message: str) -> None:
        super().__init__(EXIT_INTERNAL, f"generator: {message}")


# ------------------------------------------------------------------ run context


@dataclass
class Ctx:
    layout: Layout
    config: Config
    prices: Prices
    models: list[str]  # priciest first
    provider_of: dict[str, str]
    nano: dict[str, tuple[int, int]]
    acme: dict[str, Decimal]  # Acme's list price per million tokens
    cost_ratio_in: dict[str, float]
    cost_ratio_out: dict[str, float]
    factors: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    @property
    def seed(self) -> int:
        return self.layout.seed

    @property
    def rules(self) -> tuple[str, ...]:
        return self.config.allocation.rules

    @property
    def floor(self) -> Decimal:
        return self.config.policy.floor_pct

    def net_price(self, model: str, discount: Decimal) -> Decimal:
        return (self.acme[model] * (1 - discount)).quantize(Decimal("0.0001"), ROUND_HALF_EVEN)

    def cost_ratio(self, model: str, f_in: float) -> float:
        """Provider cost per dollar of list revenue for a model at an input-token fraction."""
        return f_in * self.cost_ratio_in[model] + (1 - f_in) * self.cost_ratio_out[model]


def build_ctx(layout: Layout, config: Config, prices: Prices) -> Ctx:
    ordered = sorted(prices.rows, key=lambda p: (-(3 * p.input_per_mtok + p.output_per_mtok), p.model))
    count = len(ordered)
    acme, cin, cout = {}, {}, {}
    for j, price in enumerate(ordered):
        # Acme's markup runs from thin on the priciest model to rich on the cheapest, so a heavy
        # mix of expensive models is what pulls an account's margin down.
        markup = Decimal("1.10") + Decimal("3.40") * j / (count - 1) if count > 1 else Decimal("2")
        blend = (3 * price.input_per_mtok + price.output_per_mtok) / 4
        acme[price.model] = max((blend * markup).quantize(Decimal("0.01"), ROUND_HALF_EVEN), Decimal("0.01"))
        cin[price.model] = float(price.input_per_mtok / acme[price.model])
        cout[price.model] = float(price.output_per_mtok / acme[price.model])
    return Ctx(
        layout=layout, config=config, prices=prices, models=[p.model for p in ordered],
        provider_of={p.model: p.provider for p in prices.rows},
        nano={p.model: (p.nano_in, p.nano_out) for p in prices.rows},
        acme=acme, cost_ratio_in=cin, cost_ratio_out=cout,
    )


# ------------------------------------------------------------------ one account-month


@dataclass
class AM:
    spec: Spec
    t: int
    role: Role
    tokens: dict[str, tuple[int, int]]
    billed: bool
    fee: int
    credit: int
    direct: int
    minutes: int
    shrink: float = 1.0

    @property
    def account_id(self) -> str:
        return self.spec.account_id


def usage_line_cents(tokens: int, net_price: Decimal) -> int:
    cents = Decimal(tokens) * net_price * 100 / Decimal(1_000_000)
    return int(cents.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def usage_cents(ctx: Ctx, am: AM) -> int:
    if not am.billed:
        return 0
    return sum(
        usage_line_cents(i + o, ctx.net_price(m, am.spec.discount)) for m, (i, o) in am.tokens.items()
    )


def _tokens(ctx: Ctx, shares: dict[str, float], list_cents: float, f_in: float) -> dict[str, tuple[int, int]]:
    out = {}
    for model in ctx.models:
        share = shares.get(model, 0.0)
        if share <= 0:
            continue
        total = round(int(list_cents * share / 100 / float(ctx.acme[model]) * 1_000_000) / 1000) * 1000
        if total < 1000:
            continue
        inp = round(total * f_in / 1000) * 1000
        out[model] = (inp, total - inp)
    return out


def _spread(ctx: Ctx, draws: list[float]) -> dict[str, float]:
    """Normal accounts lean towards the cheaper models."""
    n = len(ctx.models)
    weights = [(0.2 + draws[j]) * ((1 + 2 * j / (n - 1)) if n > 1 else 1.0) for j in range(n)]
    total = sum(weights)
    return {m: w / total for m, w in zip(ctx.models, weights)}


def _only(model: str) -> dict[str, float]:
    return {model: 1.0}


def make_am(ctx: Ctx, spec: Spec, t: int, role: Role, shrink: float = 1.0) -> AM | None:
    if not spec.active(t):
        return None
    policy = ctx.config.policy
    month = ctx.layout.months[t]
    r = rng(ctx.seed, spec.account_id, f"vol:{month}")
    u = [r.random() for _ in range(8)]
    draws = [r.random() for _ in ctx.models]
    kind, reason = role.kind, role.reason
    flagship, cheapest = ctx.models[0], ctx.models[-1]
    f_in = 0.75 if kind == "firm" and reason == "MODEL_MIX" else spec.f_in

    total = spec.base_cents * (1 + spec.growth * t) * (0.92 + 0.16 * u[0])
    fee = min(spec.fee_cents, int(total * 0.30))
    billed, direct = True, 0
    shares = _spread(ctx, draws)
    if kind == "decoy":
        total = (25_000 + 20_000 * u[0]) * shrink
        fee = min(9900, int(total * 0.40))
    usage_list = total - fee

    if kind == "firm" and reason == "MODEL_MIX":
        rest = len(ctx.models) - 1
        shares = {m: (0.94 if m == flagship else 0.06 / rest) for m in ctx.models} if rest else _only(flagship)
    elif kind == "firm" and reason == "DEEP_DISCOUNT":
        band = policy.floor_pct - policy.planted_band_pts - 8
        target = float(1 - band / 100) * (fee + usage_list * float(1 - spec.discount)) / usage_list
        lo, hi = ctx.cost_ratio(cheapest, f_in), ctx.cost_ratio(flagship, f_in)
        t_share = min(max((target - lo) / (hi - lo), 0.0), 1.0) if hi > lo else 1.0
        shares = {flagship: t_share, cheapest: 1 - t_share} if flagship != cheapest else _only(flagship)
    elif spec.discount >= Decimal("0.30") and kind != "firm":
        shares = _only(cheapest)  # a deeply discounted account is only profitable on cheap models
    elif kind == "zero_revenue":
        billed, fee, shares, usage_list = False, 0, _only(cheapest), 40_000
    elif kind == "fee_only":
        fee, shares, usage_list = 20_000, {}, 0
    elif kind == "negative_revenue":
        fee, shares, usage_list = 5_000, _only(cheapest), 20_000

    tokens = _tokens(ctx, shares, usage_list, f_in) if usage_list > 0 else {}
    am = AM(spec, t, role, tokens, billed, fee, 0, direct, 0, shrink)

    if kind == "negative_revenue":
        am.credit = -(fee + usage_cents(ctx, am) + 4_000)
    elif kind == "normal" and (spec.credit_always or u[1] < spec.credit_prob):
        am.credit = -int(total * (0.02 + 0.06 * u[2]))

    rate = SUPPORT_RATES[spec.tier]
    base_minutes = round(total * spec.support_share * (0.7 + 0.6 * u[4]) / rate) if u[5] > 0.15 else 0
    if kind in ("normal", "decoy"):
        am.minutes = base_minutes
        if kind == "normal" and spec.dedicated_rate:
            am.direct = int(total * spec.dedicated_rate * (0.8 + 0.4 * u[3]))
    elif kind == "firm":
        am.minutes = base_minutes if reason == "SUPPORT_LOAD" else 0  # keep the other drivers quiet
    return am


def to_acct_month(ctx: Ctx, am: AM) -> ref.AcctMonth:
    return ref.AcctMonth(
        account_id=am.account_id, discount=am.spec.discount, fee_cents=am.fee,
        usage_cents=usage_cents(ctx, am), credit_cents=am.credit, tokens=dict(am.tokens),
        direct_cents=am.direct, support_cents=am.minutes * SUPPORT_RATES[am.spec.tier],
    )


# ------------------------------------------------------------------ the month's pools


@dataclass
class Eval:
    accts: dict[str, ref.AcctMonth]
    provider: dict[str, int]
    pool: int
    comps: dict[str, ref.Components]


def _factor(ctx: Ctx, kind: str, month: str, key: str, lo: int, span: int) -> Decimal:
    cache_key = (kind, f"{month}:{key}")
    if cache_key not in ctx.factors:
        ctx.factors[cache_key] = Decimal(lo + rng(ctx.seed, kind, f"{month}:{key}").randrange(0, span + 1)) / 1000
    return ctx.factors[cache_key]


def provider_cents(ctx: Ctx, month: str, accts: dict[str, ref.AcctMonth]) -> dict[str, int]:
    """What the model providers invoice: list-price weight scaled by a seeded +/-4% factor."""
    out = {}
    for model in ctx.models:
        nano_in, nano_out = ctx.nano[model]
        weight = sum(a.tokens[model][0] * nano_in + a.tokens[model][1] * nano_out
                     for a in accts.values() if model in a.tokens)
        if weight:
            factor = _factor(ctx, "provider", month, model, 960, 80)
            out[model] = int((Decimal(weight) * factor / ref.NANO_PER_CENT).quantize(Decimal(1), ROUND_HALF_EVEN))
    return out


def pool_cents(ctx: Ctx, month: str, provider: dict[str, int]) -> int:
    """Untagged shared cloud cost scales with inference volume, with seeded noise."""
    base = Decimal(sum(provider.values())) * SHARED_POOL_FRACTION * _factor(ctx, "pool", month, "total", 950, 100)
    return int(base.quantize(Decimal(1), ROUND_HALF_EVEN))


def evaluate(ctx: Ctx, month: str, ams: list[AM]) -> Eval:
    accts = {am.account_id: to_acct_month(ctx, am) for am in ams}
    provider = provider_cents(ctx, month, accts)
    pool = pool_cents(ctx, month, provider)
    comps = ref.month_components(accts, provider, ctx.nano, pool, ctx.rules, ctx.floor)
    return Eval(accts, provider, pool, comps)


# ------------------------------------------------------------------ tuning


def _pts(profit: int, revenue: int) -> Fraction:
    return ref.margin_points(profit, revenue)


def _ceil(x: Fraction) -> int:
    return -((-x.numerator) // x.denominator)


def tune(ctx: Ctx, am: AM, c: ref.Components) -> bool:
    """Move one free cost towards its role; return True if anything changed."""
    pol = ctx.config.policy
    floor = Fraction(pol.floor_pct)
    kind, reason = am.role.kind, am.role.reason
    rate = SUPPORT_RATES[am.spec.tier]
    firm_target = floor - Fraction(pol.planted_band_pts) - 6

    if kind == "normal":
        need = floor + Fraction(pol.unplanted_band_pts) + 1
        if c.revenue <= 0 or min(_pts(p, c.revenue) for p in c.profit.values()) >= need:
            return False
        if am.direct or am.minutes:
            am.direct = am.minutes = 0
            return True
        share = need / 100
        lift = _ceil((share * c.revenue - min(c.profit.values())) / (1 - share))
        am.fee += max(lift, 100)
        return True

    if kind == "decoy":
        # Shift every margin down by x (the direct cost) so the floor falls inside a gap between
        # two rules' margins with at least the decoy band, plus a point and a half, on each side.
        base = sorted(_pts(p + c.direct, c.revenue) for p in c.profit.values())
        half = Fraction(pol.decoy_band_pts) + Fraction(3, 2)
        best = None
        for lo, hi in zip(base, base[1:]):
            low, high = max(lo - floor + half, Fraction(0)), hi - floor - half
            if low <= high:
                x = min(max((lo + hi) / 2 - floor, low), high)
                if best is None or hi - lo > best[0]:
                    best = (hi - lo, x)
        if best is None:
            am.shrink *= 0.7
            if am.shrink < 0.7 ** MAX_SHRINKS:
                raise GenerationError(f"{am.account_id} cannot straddle the floor even at minimum size")
            return True
        wanted = _ceil(best[1] / 100 * c.revenue)
        changed = wanted != am.direct
        am.direct = wanted
        return changed

    if kind == "zero_revenue":
        wanted = 3 * c.inference + 20_000
        changed = wanted != am.direct
        am.direct = wanted
        return changed

    if kind == "fee_only" or (kind == "firm" and reason == "SUPPORT_LOAD"):
        best = max(_pts(p + c.support, c.revenue) for p in c.profit.values())
        need = max(Fraction(0), (best - firm_target) / 100 * c.revenue)
        minutes = _ceil(need / rate)
        changed = minutes != am.minutes
        am.minutes = minutes
        return changed

    if kind == "firm" and reason == "DEDICATED_CLOUD":
        best = max(_pts(p + c.direct, c.revenue) for p in c.profit.values())
        wanted = _ceil(max(Fraction(0), (best - firm_target) / 100 * c.revenue))
        changed = wanted != am.direct
        am.direct = wanted
        return changed
    return False


def calibrate(ctx: Ctx, month: str, ams: list[AM]) -> tuple[list[AM], Eval]:
    for _ in range(MAX_ITERATIONS):
        ev = evaluate(ctx, month, ams)
        changed = False
        for i, am in enumerate(ams):
            before = am.shrink
            if tune(ctx, am, ev.comps[am.account_id]):
                changed = True
                if am.shrink != before:  # a smaller decoy is a new set of tokens
                    ams[i] = make_am(ctx, am.spec, am.t, am.role, am.shrink)
        if not changed:
            return ams, ev
    raise GenerationError(f"{month}: tuning did not settle in {MAX_ITERATIONS} rounds")


# ------------------------------------------------------------------ validation


def check_month(
    ctx: Ctx, month: str, roles: dict[str, Role], accts: dict[str, ref.AcctMonth],
    comps: dict[str, ref.Components], reasons: dict[str, ref.Classification],
) -> list[str]:
    """Problems with where each account sits against the floor and the guard bands."""
    pol = ctx.config.policy
    floor = Fraction(pol.floor_pct)
    problems = []
    for key, c in comps.items():
        role = roles[key]
        pts = [_pts(p, c.revenue) for p in c.profit.values()] if c.revenue > 0 else []
        if role.kind == "normal":
            if c.flag != "none" or (pts and min(pts) < floor + Fraction(pol.unplanted_band_pts)):
                problems.append(f"{month} {key}: unplanned account inside its band ({c.flag})")
        elif role.kind == "decoy":
            inside = any(abs(p - floor) < Fraction(pol.decoy_band_pts) for p in pts)
            if c.flag != "soft" or inside or c.revenue <= 0:
                problems.append(f"{month} {key}: decoy is {c.flag} or inside the decoy band")
        else:
            if c.flag != "firm" or (pts and max(pts) > floor - Fraction(pol.planted_band_pts)):
                problems.append(f"{month} {key}: planted account is {c.flag} or inside its band")
            got = reasons[key]
            if got.code != role.planted_reason:
                problems.append(f"{month} {key}: planted {role.planted_reason}, classifier says {got.code}")
            elif got.gap is not None and got.gap < pol.reason_margin:
                problems.append(f"{month} {key}: reason {got.code} leads by only {got.gap}")
    return problems


# ------------------------------------------------------------------ writing the books


def _fmt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _stamp(month: str, day: int | None = None, *, end: bool = False) -> str:
    y, m = int(month[:4]), int(month[5:])
    if day is None:  # whole-month charge
        if end:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return f"{y:04d}-{m:02d}-01T00:00:00Z"
    d = day + 1 if end else day
    if d > last_day(month):
        return _stamp(month, None, end=True)
    return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


@dataclass
class Rows:
    accounts: list[list] = field(default_factory=list)
    invoices: list[list] = field(default_factory=list)
    usage: list[list] = field(default_factory=list)
    provider: list[list] = field(default_factory=list)
    focus: list[list] = field(default_factory=list)
    tickets: list[list] = field(default_factory=list)
    gl: list[list] = field(default_factory=list)
    support_load: list[list] = field(default_factory=list)


def emit_month(ctx: Ctx, month: str, ams: list[AM], ev: Eval, rows: Rows) -> None:
    seed = ctx.seed
    issued = f"{month}-{last_day(month):02d}"
    totals = {"4000": 0, "4100": 0, "4900": 0}
    focus_total = 0
    support_total = 0
    n_focus = 0

    def add_focus(start, end, cents, service, category, resource, tags):
        nonlocal n_focus, focus_total
        n_focus += 1
        focus_total += cents
        rows.focus.append([start, end, _fmt(cents), _fmt(cents), "USD", service, category, resource,
                           tags, f"focus_{month}_{n_focus:05d}"])

    for am in sorted(ams, key=lambda a: a.account_id):
        acct, spec = am.account_id, am.spec
        invoice_id, line = f"inv_{month}_{acct}", 0

        def add_line(line_type, model, qty, price, cents):
            nonlocal line
            line += 1
            rows.invoices.append([invoice_id, f"{invoice_id}_{line:02d}", acct, month, month, issued,
                                  line_type, model, qty, price, cents])

        if am.fee:
            add_line("platform_fee", "", "", "", am.fee)
            totals["4000"] += am.fee
        for model in ctx.models:
            if model not in am.tokens:
                continue
            i, o = am.tokens[model]
            rows.usage.append([acct, month, model, i, o])
            if am.billed:
                net = ctx.net_price(model, spec.discount)
                cents = usage_line_cents(i + o, net)
                add_line("usage", model, i + o, f"{net:.4f}", cents)
                totals["4100"] += cents
        if am.credit:
            add_line("credit", "", "", "", am.credit)
            totals["4900"] += am.credit

        if am.direct:
            parts = ref.apportion(am.direct, {name: w for name, _, _, w in TAGGED_SERVICES})
            for name, category, resource, _ in TAGGED_SERVICES:
                if parts[name]:
                    add_focus(_stamp(month), _stamp(month, end=True), parts[name], name, category,
                              resource.format(acct=acct), json.dumps({"tenant": acct}, separators=(",", ":")))

        if am.minutes:
            n = min(30, max(1, am.minutes // 40))
            tr = rng(seed, acct, f"tickets:{month}")
            split = ref.apportion(am.minutes - n, {f"{k:03d}": 1 + tr.randrange(9) for k in range(n)})
            for k in range(n):
                closed_day = 1 + tr.randrange(min(28, last_day(month)))
                if tr.random() < 0.2:
                    opened = f"{_prev_month(month)}-{20 + tr.randrange(9):02d}"
                else:
                    opened = f"{month}-{1 + tr.randrange(closed_day):02d}"
                rows.tickets.append([f"tkt_{month}_{acct}_{k:02d}", acct, opened,
                                     f"{month}-{closed_day:02d}", 1 + split[f"{k:03d}"], spec.tier])
            support_total += am.minutes * SUPPORT_RATES[spec.tier]
            rows.support_load.append([month, acct, spec.tier, n, am.minutes,
                                      am.minutes * SUPPORT_RATES[spec.tier]])

    if ctx.layout.months[-1] == month:  # a few tickets still open at the end of the window
        for am in ams:
            if am.role.kind == "normal" and rng(seed, am.account_id, "open").random() < 0.02:
                rows.tickets.append([f"tkt_open_{am.account_id}", am.account_id,
                                     f"{month}-{20 + rng(seed, am.account_id, 'open-day').randrange(9):02d}",
                                     "", 20, am.spec.tier])

    pr = rng(seed, "pool", f"rows:{month}")
    weights = {s[0]: s[3] + pr.randrange(6) for s in SHARED_SERVICES}
    for name, category, resource, _, daily in SHARED_SERVICES:
        cents = ref.apportion(ev.pool, weights)[name]
        if not cents:
            continue
        if daily:
            days = {f"{d:02d}": 1 for d in range(1, last_day(month) + 1)}
            for day, part in ref.apportion(cents, days).items():
                if part:
                    add_focus(_stamp(month, int(day)), _stamp(month, int(day), end=True), part,
                              name, category, resource, "{}")
        else:
            add_focus(_stamp(month), _stamp(month, end=True), cents, name, category, resource, "{}")

    for model, cents in sorted(ev.provider.items()):
        tokens = sum(sum(a.tokens[model]) for a in ev.accts.values() if model in a.tokens)
        rows.provider.append([ctx.provider_of[model], model, month, tokens, cents])
    entries = [("4000", totals["4000"], "platform fees"), ("4100", totals["4100"], "usage revenue"),
               ("4900", totals["4900"], "credits")]
    entries += [("5100", cents, f"inference {model}") for model, cents in sorted(ev.provider.items())]
    entries += [("5200", focus_total, "cloud"), ("5300", support_total, "support")]
    for n, (code, cents, memo) in enumerate(entries, start=1):
        rows.gl.append([f"gl_{month}_{n:02d}", month, code, cents, f"synthetic {month} {memo}"])


def _write_csv(path: Path, cols: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def write_books(layout: Layout, rows: Rows, books: Path) -> None:
    books.mkdir(parents=True, exist_ok=True)
    _write_csv(books / "accounts.csv", ["account_id", "display_name", "plan", "start_date", "discount_pct"], rows.accounts)
    _write_csv(books / "invoices.csv", INVOICE_COLS, rows.invoices)
    _write_csv(books / "usage_monthly.csv", ["account_id", "month", "model", "input_tokens", "output_tokens"], rows.usage)
    _write_csv(books / "provider_invoices.csv", ["provider", "model", "month", "tokens", "amount_cents"], rows.provider)
    _write_csv(books / "cloud_focus.csv", FOCUS_COLS, rows.focus)
    _write_csv(books / "support_tickets.csv", TICKET_COLS, rows.tickets)
    _write_csv(books / "gl.csv", ["entry_id", "gl_period", "gl_code", "amount_cents", "memo"], rows.gl)
    (books / "window.json").write_text(
        json.dumps({"first": layout.months[0], "last": layout.months[-1]}) + "\n", encoding="utf-8"
    )


# ------------------------------------------------------------------ the whole run


def build_books(ctx: Ctx) -> tuple[Rows, dict[str, dict[str, Role]], dict[str, Eval]]:
    layout = ctx.layout
    rows = Rows()
    for spec in layout.specs:
        rows.accounts.append([spec.account_id, spec.name, spec.plan, spec.start_date, f"{spec.discount:.2f}"])
    roles_by_month: dict[str, dict[str, Role]] = {}
    evals: dict[str, Eval] = {}
    for t, month in enumerate(layout.months):
        ams = [am for spec in layout.specs
               if (am := make_am(ctx, spec, t, layout.role(spec.index, t))) is not None]
        ams, ev = calibrate(ctx, month, ams)
        emit_month(ctx, month, ams, ev, rows)
        roles_by_month[month] = {am.account_id: am.role for am in ams}
        evals[month] = ev
    return rows, roles_by_month, evals


def write_truth(
    ctx: Ctx, truth: Path, books: Path, rates_csv: Path, prices_csv: Path,
    roles_by_month: dict[str, dict[str, Role]], evals: dict[str, Eval], rows: Rows,
) -> dict[str, ref.MonthResult]:
    """Recompute everything from the written books and write `truth/` from that."""
    pol, rules = ctx.config.policy, ctx.rules
    drivers = [(d.code, d.median_floor) for d in pol.drivers]
    results = ref.recompute_from_books(books, prices_csv, rates_csv, rules, pol.floor_pct, drivers)
    if sorted(results) != sorted(ctx.layout.months):
        raise GenerationError("the books do not cover every month of the window")
    problems = []
    for month in ctx.layout.months:
        res, roles = results[month], roles_by_month[month]
        if res.comps != evals[month].comps or res.pool_cents != evals[month].pool:
            problems.append(f"{month}: the written books do not recompute to the tuned components")
        problems += check_month(ctx, month, roles, res.accts, res.comps, res.reasons)
    if problems:
        raise GenerationError("; ".join(problems[:6]) + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""))

    truth.mkdir(parents=True, exist_ok=True)
    planted: dict = {}
    decoys: dict = {}
    for month in ctx.layout.months:
        res, roles = results[month], roles_by_month[month]
        planted[month] = [
            {"account_id": k, "reason_code": roles[k].planted_reason}
            for k in sorted(res.comps) if res.comps[k].flag == "firm"
        ]
        decoys[month] = [k for k in sorted(res.comps) if res.comps[k].flag == "soft"]
    planted["decoys"] = decoys
    (truth / "planted_flags.json").write_text(json.dumps(planted, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (truth / "allowed_rules.json").write_text(json.dumps(
        {"rules": list(rules), "recommended": ctx.config.allocation.recommended, "floor_pct": str(pol.floor_pct)},
        indent=1) + "\n", encoding="utf-8")

    comp_cols = ["month", "account_id", "revenue_cents", "inference_cost_cents", "cloud_direct_cents",
                 "support_cost_cents"] + [f"{x}_{r}_cents" for r in rules for x in ("cloud_shared", "contribution_profit")]
    flag_cols = ["month", "account_id"] + [f"below_{r}" for r in rules] + ["rules_below_floor", "flag"]
    comp_rows, flag_rows = [], []
    for month in ctx.layout.months:
        for key in sorted(results[month].comps):
            c = results[month].comps[key]
            comp_rows.append([month, key, c.revenue, c.inference, c.direct, c.support]
                             + [v for r in rules for v in (c.shared[r], c.profit[r])])
            flag_rows.append([month, key] + [str(c.below[r]).lower() for r in rules] + [c.rules_below, c.flag])
    _write_csv(truth / "cost_components.csv", comp_cols, comp_rows)
    _write_csv(truth / "rule_flags.csv", flag_cols, flag_rows)
    _write_csv(truth / "usage_by_model.csv", ["month", "account_id", "model", "input_tokens", "output_tokens"],
               [[m, a, mo, i, o] for a, m, mo, i, o in sorted(rows.usage, key=lambda r: (r[1], r[0], r[2]))])
    _write_csv(truth / "list_prices.csv", ["provider", "model", "input_per_mtok", "output_per_mtok"],
               [[p.provider, p.model, p.input_per_mtok, p.output_per_mtok] for p in ctx.prices.rows])
    _write_csv(truth / "discounts.csv", ["account_id", "discount_pct"],
               [[s.account_id, f"{s.discount:.2f}"] for s in ctx.layout.specs])
    _write_csv(truth / "support_load.csv",
               ["month", "account_id", "tier", "tickets_closed", "handle_minutes", "cost_cents"],
               sorted(rows.support_load))
    return results
