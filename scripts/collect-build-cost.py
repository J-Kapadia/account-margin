#!/usr/bin/env python3
"""Sum a per-item cost ledger into the build-cost totals the build-vs-buy figures read.

    collect-build-cost.py --ledger PATH --review PATH --collected-on YYYY-MM-DD
                          [--run-cost-monthly-usd N] [--out PATH]

The ledger is a CSV with the columns work_id, model, input_tokens, output_tokens, usd, started,
closed, source (timestamps ISO 8601 with a UTC offset or Z); the review file is a CSV with the
columns work_id, minutes. The script takes paths only, never reads the clock, and writes six
totals, one row each, in a fixed order, with LF line endings:

    measure, value, source, collected_on

Elapsed hours are the sum over work items of (closed - started); an item with several ledger rows
(one per model) counts once, from its earliest `started` to its latest `closed`. Dollars, hours
and minutes are summed exactly and rounded once, to two decimals, half-even. A measure's `source`
is the least certain label among the rows behind it: manual, else git, else events. Review minutes
and the monthly run cost are typed in by hand, so both are labelled manual.

Exit 0 on success; exit 2 for a bad argument, an unreadable file or a row that cannot be used,
with a message naming the file and row. Nothing is written on exit 2.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

DEFAULT_OUT = "data/reference/build_cost.csv"
LEDGER_COLUMNS = ["work_id", "model", "input_tokens", "output_tokens", "usd", "started", "closed", "source"]
REVIEW_COLUMNS = ["work_id", "minutes"]
OUT_COLUMNS = ["measure", "value", "source", "collected_on"]
# Least certain last: a measure takes the label of the last of these that any of its rows carries.
SOURCES = ["events", "git", "manual"]
CENT = Decimal("0.01")


class BadInput(Exception):
    """An argument or an input row that cannot be used."""


def two_places(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_EVEN))


def read_rows(path: Path, columns: list[str]):
    """Yield (line number, row) for each data row; the header must carry every column."""
    try:
        handle = path.open(newline="", encoding="utf-8")
    except OSError as err:
        raise BadInput(f"cannot read {path}: {err.strerror}") from err
    with handle:
        reader = csv.DictReader(handle)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise BadInput(f"{path}: header lacks column(s) {', '.join(missing)}")
        for row in reader:
            yield reader.line_num, row


def field(row: dict, name: str, where: str) -> str:
    value = (row.get(name) or "").strip()
    if not value:
        raise BadInput(f"{where}: {name} is missing")
    return value


def count(row: dict, name: str, where: str) -> int:
    text = field(row, name, where)
    if not text.isascii() or not text.isdigit():
        raise BadInput(f"{where}: {name} {text!r} is not a whole number")
    return int(text)


def amount(row: dict, name: str, where: str) -> Decimal:
    text = field(row, name, where)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise BadInput(f"{where}: {name} {text!r} is not a number") from None
    if not value.is_finite() or value < 0:
        raise BadInput(f"{where}: {name} {text!r} is not a non-negative number")
    return value


def timestamp(row: dict, name: str, where: str) -> datetime:
    text = field(row, name, where)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise BadInput(f"{where}: {name} {text!r} is not an ISO 8601 timestamp") from None
    if moment.tzinfo is None:
        raise BadInput(f"{where}: {name} {text!r} has no UTC offset (write ...Z)")
    return moment


def collect_ledger(path: Path) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "usd": Decimal(0)}
    spans: dict[str, list[datetime]] = {}
    sources: set[str] = set()
    for line, row in read_rows(path, LEDGER_COLUMNS):
        where = f"{path} line {line} (work_id {(row.get('work_id') or '').strip() or '?'})"
        work_id = field(row, "work_id", where)
        source = field(row, "source", where)
        if source not in SOURCES:
            raise BadInput(f"{where}: unknown source {source!r} (expected {', '.join(SOURCES)})")
        started, closed = timestamp(row, "started", where), timestamp(row, "closed", where)
        if closed < started:
            raise BadInput(f"{where}: closed {row['closed'].strip()!r} is before started {row['started'].strip()!r}")
        totals["input_tokens"] += count(row, "input_tokens", where)
        totals["output_tokens"] += count(row, "output_tokens", where)
        totals["usd"] += amount(row, "usd", where)
        sources.add(source)
        span = spans.setdefault(work_id, [started, closed])
        span[0], span[1] = min(span[0], started), max(span[1], closed)
    elapsed = sum((end - start for start, end in spans.values()), timedelta())
    totals["elapsed_hours"] = Decimal(elapsed // timedelta(microseconds=1)) / (3600 * 10**6)
    totals["source"] = max(sources, key=SOURCES.index) if sources else SOURCES[0]
    return totals


def collect_review(path: Path) -> Decimal:
    minutes = Decimal(0)
    for line, row in read_rows(path, REVIEW_COLUMNS):
        where = f"{path} line {line} (work_id {(row.get('work_id') or '').strip() or '?'})"
        field(row, "work_id", where)
        minutes += amount(row, "minutes", where)
    return minutes


def collected_on(text: str) -> str:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date written YYYY-MM-DD") from None


def run_cost(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a non-negative number")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sum a per-item cost ledger into the build-cost totals.")
    parser.add_argument("--ledger", required=True, type=Path, help="per-item cost ledger (CSV)")
    parser.add_argument("--review", required=True, type=Path, help="review minutes per item (CSV)")
    parser.add_argument("--run-cost-monthly-usd", type=run_cost, default=Decimal(0), metavar="N",
                        help="monthly run cost in dollars, labelled manual (default 0)")
    parser.add_argument("--collected-on", required=True, type=collected_on, metavar="YYYY-MM-DD",
                        help="the day the figures were collected; the script never reads the clock")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT), help=f"output CSV (default {DEFAULT_OUT})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ledger = collect_ledger(args.ledger)
        review = collect_review(args.review)
    except BadInput as err:
        print(f"collect-build-cost: {err}", file=sys.stderr)
        return 2
    source = ledger["source"]
    rows = [
        ("build_cost_usd", two_places(ledger["usd"]), source),
        ("input_tokens", str(ledger["input_tokens"]), source),
        ("output_tokens", str(ledger["output_tokens"]), source),
        ("elapsed_hours", two_places(ledger["elapsed_hours"]), source),
        ("review_minutes", two_places(review), "manual"),
        ("run_cost_monthly_usd", two_places(args.run_cost_monthly_usd), "manual"),
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUT_COLUMNS)
        for measure, value, label in rows:
            writer.writerow([measure, value, label, args.collected_on])
    width = max(len(measure) for measure, _, _ in rows)
    for measure, value, label in rows:
        print(f"{measure:<{width}}  {value:>12}  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
