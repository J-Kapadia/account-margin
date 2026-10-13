"""The generator (plan §7.2): seeded books for the `small` fixture.

`small` is 3 accounts and 2026-03 only. It plants no flags, reads no config and
writes no `truth/`: Phase 0 computes revenue only, and the answer key arrives
with the generator proper. Every random draw comes from a `random.Random`
seeded per entity, so output is byte-stable and independent of generation order.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from .books import write_manifest

DEFAULT_SEED = 20261013
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
