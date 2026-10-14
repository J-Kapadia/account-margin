"""`margin inject`: copy a books directory and apply one named, seeded error to 2026-03.

The five mutations are fixed here, independently of the controls that must catch them: the
target is the row of the month chosen by `random.Random(f"{seed}:inject:{error}")` among the
rows the mutation applies to, in file order. The copy's manifest is rewritten, with the error
and the seed in its header, so it is a books directory in its own right.
"""

from __future__ import annotations

import csv
import random
import shutil
from pathlib import Path

from . import books as books_mod
from .errors import EXIT_PRECONDITION, EXIT_USAGE, MarginError

TARGET_MONTH = "2026-03"
ERRORS = ("duplicate_invoice", "cutoff_slip", "alloc_twice", "sign_flip", "rounding_slip")
SLIPPED_PERIOD = "2026-04"


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _pick(rng: random.Random, rows: list[dict], applies, what: str) -> int:
    """Index of the chosen row among those `applies` to; exit 4 if the books have none."""
    candidates = [i for i, r in enumerate(rows) if applies(r)]
    if not candidates:
        raise MarginError(EXIT_PRECONDITION, f"the books have no {what} in {TARGET_MONTH} to inject into")
    return rng.choice(candidates)


def _untagged(row: dict) -> bool:
    return '"tenant"' not in row["Tags"]


def _mutate(error: str, out: Path, rng: random.Random) -> dict:
    """Apply `error` to the files of `out`; return what was touched (file, key, ordinal)."""
    if error in ("duplicate_invoice", "cutoff_slip", "sign_flip"):
        fields, rows = _read(out / "invoices.csv")
        in_month = lambda r: r["service_period"] == TARGET_MONTH  # noqa: E731
        if error == "duplicate_invoice":
            k = _pick(rng, rows, in_month, "invoice line")
            rows.append({**rows[k], "line_id": rows[k]["line_id"] + "_dup"})
            k = len(rows) - 1
        elif error == "cutoff_slip":
            k = _pick(rng, rows, lambda r: in_month(r) and r["line_type"] != "credit", "revenue line")
            rows[k] = {**rows[k], "gl_period": SLIPPED_PERIOD}
        else:
            k = _pick(rng, rows, lambda r: in_month(r) and r["line_type"] == "credit", "credit line")
            rows[k] = {**rows[k], "amount_cents": str(-int(rows[k]["amount_cents"]))}
        _write(out / "invoices.csv", fields, rows)
        return {"file": "invoices.csv", "key": rows[k]["line_id"], "ordinal": k + 1,
                "account_id": rows[k]["account_id"]}
    if error == "alloc_twice":
        fields, rows = _read(out / "cloud_focus.csv")
        k = _pick(rng, rows, lambda r: r["ChargePeriodStart"].startswith(TARGET_MONTH) and _untagged(r),
                  "untagged FOCUS row")
        _, accounts = _read(out / "accounts.csv")
        tenant = rng.choice(accounts)["account_id"]
        rows.append({**rows[k], "Tags": f'{{"tenant":"{tenant}"}}', "line_id": rows[k]["line_id"] + "_dup"})
        _write(out / "cloud_focus.csv", fields, rows)
        return {"file": "cloud_focus.csv", "key": rows[-1]["line_id"], "ordinal": len(rows), "account_id": tenant}
    fields, rows = _read(out / "gl.csv")
    k = _pick(rng, rows, lambda r: r["gl_period"] == TARGET_MONTH and r["gl_code"] == "5100", "GL 5100 entry")
    rows[k] = {**rows[k], "amount_cents": str(int(rows[k]["amount_cents"]) + 1)}
    _write(out / "gl.csv", fields, rows)
    return {"file": "gl.csv", "key": rows[k]["entry_id"], "ordinal": k + 1, "gl_code": "5100"}


def inject(books: Path, out: Path, error: str, seed: int) -> dict:
    """Copy `books` to `out` with one `error`; write the copy's manifest and return what changed."""
    if error not in ERRORS:
        raise MarginError(EXIT_USAGE, f"unknown error {error!r}: valid errors are {', '.join(ERRORS)}")
    header, _ = books_mod.read_manifest(books)  # exit 4 if the source has no manifest
    if out.resolve() == books.resolve():
        raise MarginError(EXIT_USAGE, "--out must be a different directory from the books")
    if out.exists() and any(out.iterdir()):
        raise MarginError(EXIT_USAGE, f"{out} is not empty: inject writes a new directory")
    out.mkdir(parents=True, exist_ok=True)
    for path in books_mod.books_files(books):
        shutil.copy2(path, out / path.name)
    touched = _mutate(error, out, random.Random(f"{seed}:inject:{error}"))
    books_mod.write_manifest(out, {**header, "injected_error": error, "inject_seed": str(seed)})
    return {"error": error, "seed": seed, "month": TARGET_MONTH, "out": str(out), "target": touched}
