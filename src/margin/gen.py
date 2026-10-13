"""The generator (plan §7.2): seeded books, the hidden answer key and the manifest.

`generate_small` is the Phase 0 `small` fixture: 3 accounts and 2026-03 only, no flags, no
config, no `truth/`. `generate` is the generator proper (the full books and the `mini` and
`scenario` presets): it reads the config directory and a price list, plants firm flags and
decoys, writes `truth/`, and records the config hashes in the manifest header. `verify`
compares a books directory, and a fresh regeneration, with that manifest.

Every random draw comes from a `random.Random` seeded per entity, so output is byte-stable
and independent of generation order.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import hashlib
import tempfile

from . import gen_books, gen_plan
from . import truth_reference as ref
from .books import read_manifest, write_manifest
from .errors import EXIT_CONTROL, EXIT_USAGE, MarginError
from .gen_config import Config, Prices, load_config, load_prices, sha256_file
from .gen_plan import DEFAULT_ACCOUNTS, DEFAULT_FIRST_MONTH, DEFAULT_MONTHS
from .paths import ROOT
from .truth_reference import read_rates

DEFAULT_SEED = gen_plan.DEFAULT_SEED
SUPPORT_RATES_CSV = ROOT / "data" / "reference" / "support_rates.csv"
MONTH = "2026-03"
ISSUED = "2026-03-31"

ACCOUNTS = [
    # account_id, display_name, plan, start_date, discount_pct, platform fee in cents
    ("acct_0001", "Alder Analytics", "usage", "2025-10-06", "0.00", 9900),
    ("acct_0002", "Birch Robotics", "committed", "2025-11-18", "0.10", 49900),
    ("acct_0003", "Cedar Health Labs", "enterprise", "2025-12-02", "0.25", 99900),
]
# Synthetic list prices in USD per million tokens; invoice unit_price is net of discount.
MODELS = [("alpha-1", Decimal("6.00")), ("alpha-mini", Decimal("1.50"))]
CREDITS = {"acct_0003": -2500}

INVOICE_COLS = [
    "invoice_id", "line_id", "account_id", "service_period", "gl_period", "issued_date",
    "line_type", "model", "quantity_tokens", "unit_price", "amount_cents",
]
USAGE_COLS = ["account_id", "month", "model", "input_tokens", "output_tokens"]
GL_COLS = ["entry_id", "gl_period", "gl_code", "amount_cents", "memo"]
ACCOUNT_COLS = ["account_id", "display_name", "plan", "start_date", "discount_pct"]


def _rng(seed: int, account_id: str, purpose: str) -> random.Random:
    return random.Random(f"{seed}:{account_id}:{purpose}")


def _usage_cents(tokens: int, unit_price: Decimal) -> int:
    cents = Decimal(tokens) * unit_price * 100 / Decimal(1_000_000)
    return int(cents.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _write_csv(path: Path, cols: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def generate_small(out: Path, seed: int = DEFAULT_SEED) -> dict:
    """Write the `small` books into `out` (replacing it) and its manifest."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    accounts, invoices, usage = [], [], []
    gl_totals = {"4000": 0, "4100": 0, "4900": 0}
    for n, (acct, name, plan, start, discount, fee) in enumerate(ACCOUNTS, start=1):
        accounts.append([acct, name, plan, start, discount])
        invoice_id = f"inv_{MONTH}_{acct}"
        line = 0

        def add(line_type, model, qty, price, cents):
            nonlocal line
            line += 1
            invoices.append([
                invoice_id, f"{invoice_id}_{line:02d}", acct, MONTH, MONTH, ISSUED,
                line_type, model, qty, price, cents,
            ])

        add("platform_fee", "", "", "", fee)
        gl_totals["4000"] += fee
        rng = _rng(seed, acct, "usage")
        for model, list_price in MODELS:
            qty = rng.randrange(20, 400) * 1_000_000 + rng.randrange(0, 1000) * 1_000
            net = (list_price * (1 - Decimal(discount))).quantize(Decimal("0.0001"))
            cents = _usage_cents(qty, net)
            add("usage", model, qty, f"{net}", cents)
            gl_totals["4100"] += cents
            out_tokens = qty // 4
            usage.append([acct, MONTH, model, qty - out_tokens, out_tokens])
        if acct in CREDITS:
            add("credit", "", "", "", CREDITS[acct])
            gl_totals["4900"] += CREDITS[acct]

    gl = [
        [f"gl_{i:06d}", MONTH, code, cents, f"synthetic {MONTH} revenue"]
        for i, (code, cents) in enumerate(sorted(gl_totals.items()), start=1)
    ]
    _write_csv(out / "accounts.csv", ACCOUNT_COLS, accounts)
    _write_csv(out / "invoices.csv", INVOICE_COLS, invoices)
    _write_csv(out / "usage_monthly.csv", USAGE_COLS, usage)
    _write_csv(out / "gl.csv", GL_COLS, gl)
    (out / "window.json").write_text(
        json.dumps({"first": MONTH, "last": MONTH}) + "\n", encoding="utf-8"
    )
    books_id = write_manifest(out)
    return {
        "books": str(out),
        "seed": seed,
        "accounts": len(accounts),
        "window": {"first": MONTH, "last": MONTH},
        "books_id": books_id,
    }


# ---------------------------------------------------------------- the generator proper


def _truth_digest(truth: Path) -> str:
    lines = [f"{sha256_file(truth / name)}  {name}" for name in gen_books.TRUTH_FILES]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


MART_COLS = [
    "month", "rule", "account_id", "revenue_cents", "inference_cost_cents", "cloud_direct_cents",
    "cloud_shared_cents", "support_cost_cents", "contribution_profit_cents", "margin_pct",
    "trailing3_margin_pct", "below_floor",
]
EXPECTED_MART = "mini_expected_mart.csv"


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:]) - 1


def expected_mart_rows(results: dict, rules: tuple[str, ...] | list[str]) -> list[list]:
    """`fct_contribution_by_rule` as the Python reference computes it, one row per account,
    rule and month. `margin_pct` and the trailing margin are four-decimal text, `n/a` at zero
    revenue; the trailing margin is a ratio of sums over the month and up to two months before."""
    rows = []
    for month in sorted(results):
        for rule in rules:
            for account_id in sorted(results[month].comps):
                c = results[month].comps[account_id]
                window = [
                    results[m].comps[account_id] for m in sorted(results)
                    if _month_index(month) - 2 <= _month_index(m) <= _month_index(month)
                    and account_id in results[m].comps
                ]
                trailing = ref.margin_pct(sum(w.profit[rule] for w in window), sum(w.revenue for w in window))
                shown = ref.margin_pct(c.profit[rule], c.revenue)
                rows.append([
                    month, rule, account_id, c.revenue, c.inference, c.direct, c.shared[rule], c.support,
                    c.profit[rule], "n/a" if shown is None else str(shown),
                    "n/a" if trailing is None else str(trailing), str(c.below[rule]).lower(),
                ])
    return rows


def write_expected_mart(path: Path, results: dict, rules: tuple[str, ...] | list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(MART_COLS)
        writer.writerows(expected_mart_rows(results, rules))


def _check_rates() -> None:
    """The generator's own support rates and the reference file `close` reads must agree."""
    if not SUPPORT_RATES_CSV.is_file() or read_rates(SUPPORT_RATES_CSV) != gen_plan.SUPPORT_RATES:
        raise MarginError(
            EXIT_USAGE, f"{SUPPORT_RATES_CSV} does not match the generator's support rate table"
        )


def generate(
    *,
    books: Path,
    truth: Path,
    config: Config,
    prices: Prices,
    seed: int = DEFAULT_SEED,
    n_accounts: int = DEFAULT_ACCOUNTS,
    n_months: int = DEFAULT_MONTHS,
    first_month: str = DEFAULT_FIRST_MONTH,
    preset: str | None = None,
) -> dict:
    """Write the books, `truth/` and the manifest; return a summary."""
    _check_rates()
    layout = gen_plan.build_layout(seed, n_accounts, first_month, n_months, preset)
    ctx = gen_books.build_ctx(layout, config, prices)
    rows, roles_by_month, evals = gen_books.build_books(ctx)
    gen_books.write_books(layout, rows, books)
    results = gen_books.write_truth(
        ctx, truth, books, SUPPORT_RATES_CSV, prices.path, roles_by_month, evals, rows
    )
    if preset == "mini":
        # The expected mart sits beside the committed truth: tests/fixtures/mini_expected_mart.csv.
        write_expected_mart(truth.parent / EXPECTED_MART, results, config.allocation.rules)
    header = {
        "seed": str(seed), "accounts": str(n_accounts), "months": str(n_months),
        "first_month": first_month, "fixture": preset or "none",
        "policy_sha256": config.policy.sha256, "allocation_sha256": config.allocation.sha256,
        "prices_sha256": prices.sha256, "truth_sha256": _truth_digest(truth),
    }
    books_id = write_manifest(books, header, gen_books.BOOKS_FILES)
    firm = {m: sum(c.flag == "firm" for c in r.comps.values()) for m, r in results.items()}
    soft = {m: sum(c.flag == "soft" for c in r.comps.values()) for m, r in results.items()}
    return {
        "books": str(books), "truth": str(truth), "seed": seed, "accounts": n_accounts,
        "window": {"first": layout.months[0], "last": layout.months[-1]},
        "books_id": books_id, "firm_per_month": firm, "decoys_per_month": soft,
    }


def _verify_failure(details: list[str]) -> MarginError:
    return MarginError(
        EXIT_CONTROL, details[0],
        failed_controls=[{"id": "CHK-GEN-VERIFY", "violating_rows": len(details), "detail": details}],
    )


def verify(*, books: Path, truth: Path, config_dir: Path, prices_path: Path) -> dict:
    """`gen --verify` (CHK-GEN-VERIFY): config, price list, books on disk and a fresh regeneration."""
    header, lines = read_manifest(books)
    config = load_config(config_dir)
    prices = load_prices(prices_path)
    stale = []
    if config.policy.sha256 != header.get("policy_sha256") or config.allocation.sha256 != header.get("allocation_sha256"):
        stale.append("config changed since the books were generated: regenerate")
    if prices.sha256 != header.get("prices_sha256"):
        stale.append("price list changed since the books were generated: regenerate")
    if stale:
        raise _verify_failure(stale)

    drift = []
    if books.is_dir():
        names = [line.split("  ", 1)[1] for line in lines]
        on_disk = [f"{sha256_file(books / n)}  {n}" if (books / n).is_file() else "" for n in names]
        if on_disk != lines:
            drift.append("books on disk differ from the manifest: regenerate")
    if truth.is_dir() and header.get("truth_sha256"):
        if not all((truth / n).is_file() for n in gen_books.TRUTH_FILES) or _truth_digest(truth) != header["truth_sha256"]:
            drift.append("truth on disk differs from the manifest: regenerate")
    with tempfile.TemporaryDirectory() as tmp:
        fresh_books, fresh_truth = Path(tmp) / "books", Path(tmp) / "truth"
        generate(
            books=fresh_books, truth=fresh_truth, config=config, prices=prices,
            seed=int(header["seed"]), n_accounts=int(header["accounts"]),
            n_months=int(header["months"]), first_month=header["first_month"],
            preset=None if header["fixture"] == "none" else header["fixture"],
        )
        _, fresh_lines = read_manifest(fresh_books)
        fresh_header, _ = read_manifest(fresh_books)
    if fresh_lines != lines or fresh_header["truth_sha256"] != header.get("truth_sha256"):
        drift.append("generator output drifted from the manifest: regenerate and re-commit it")
    if drift:
        raise _verify_failure(drift)
    return {"books": str(books), "files": len(lines), "seed": int(header["seed"])}
