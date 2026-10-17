"""Injected errors are caught: each error `margin inject` makes in a copy of the `mini` books
fails `close --month 2026-03` with exit 3, the control built for it is in `failed_controls`, and
its detail names a number the error touches and the `_row_hash` of the row it changed. Every copy
closes in its own temporary warehouse; the clean books pass.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from conftest import connect
import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_PRICES, Result, margin, shared_dir
from margin.inject import ERRORS

MONTH = "2026-03"
# error -> the control that must name it (the rounding slip is the one named by a tie-out)
NAMED = {
    "duplicate_invoice": "CTL-REV-DUP",
    "cutoff_slip": "CTL-CUTOFF",
    "alloc_twice": "CTL-ALLOC-CONSERVE",
    "sign_flip": "CTL-SIGN",
    "rounding_slip": "CTL-TIE-INF",
}
# controls with nothing to do with any injected error
UNRELATED = {"CTL-PRICE-PIN", "CTL-SIGNOFF", "CTL-LOCK", "CTL-PROV"}
# error -> (raw table, key column) of the row it changes
ROW = {
    "duplicate_invoice": ("invoices", "line_id"),
    "cutoff_slip": ("invoices", "line_id"),
    "alloc_twice": ("cloud_focus", "line_id"),
    "sign_flip": ("invoices", "line_id"),
    "rounding_slip": ("gl", "entry_id"),
}


def inject(error: str, out: Path, *extra: str):
    return margin("inject", "--fixture", "mini", "--error", error, "--out", str(out), "--json", *extra)


def close(books: Path, warehouse: Path, month: str = MONTH):
    # Closes run side by side, so each gets its own dbt target and log directory.
    return margin(
        "close", "--fixture", "mini", "--books", str(books), "--warehouse", str(warehouse),
        "--config", str(MINI_CONFIG), "--month", month, "--json",
        env={"MARGIN_PRICES": str(MINI_PRICES), "DBT_TARGET_PATH": str(warehouse.parent / "target"),
             "DBT_LOG_PATH": str(warehouse.parent / "logs")},
    )


def clean_books(root: Path) -> Path:
    books = root / "books"
    shutil.copytree(MINI_BOOKS, books)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", root / "books.manifest.sha256")
    return books


class Closes:
    """Each copy (the five injected and the clean books) closed once for the whole run, in its own
    warehouse, the first time a test asks for it: workers share the closes rather than repeat them."""

    def __init__(self, tmp_path_factory):
        self.tmp_path_factory = tmp_path_factory

    def __getitem__(self, name: str) -> dict:
        def build(root: Path) -> None:
            if name == "clean":
                books, target = clean_books(root), None
            else:
                injected = inject(name, root / "books")
                assert injected.code == 0, injected.stderr
                books, target = root / "books", injected.json["target"]
            result = close(books, root / "w.duckdb")
            (root / "close.json").write_text(json.dumps(
                {"code": result.code, "stdout": result.stdout, "stderr": result.stderr, "target": target}))

        root = shared_dir(self.tmp_path_factory, f"inject-{name}", build)
        saved = json.loads((root / "close.json").read_text())
        return {"result": Result(saved["code"], saved["stdout"], saved["stderr"]), "target": saved["target"],
                "warehouse": root / "w.duckdb"}


@pytest.fixture(scope="module")
def closes(tmp_path_factory) -> Closes:
    return Closes(tmp_path_factory)


def expected_number_id(error: str, target: dict) -> str:
    if error == "rounding_slip":
        return f"pnl/{MONTH}/5100"
    if error == "alloc_twice":
        return f"cp/{MONTH}/{target['account_id']}/cloud_direct"
    return f"cp/{MONTH}/{target['account_id']}/revenue"


def row_hash(warehouse: Path, error: str, key: str) -> str:
    table, column = ROW[error]
    con = connect(str(warehouse), read_only=True)
    try:
        (found,) = con.execute(f"SELECT _row_hash FROM raw.{table} WHERE {column} = ?", [key]).fetchall()
    finally:
        con.close()
    return found[0]


# The other three errors close in the full tier; the first two keep a close-failure check in the fast one.
SLOW_ERRORS = ("cutoff_slip", "alloc_twice", "sign_flip")


@pytest.mark.parametrize(
    "error", [pytest.param(e, marks=pytest.mark.slow) if e in SLOW_ERRORS else e for e in ERRORS])
def test_the_injected_error_fails_the_close_naming_its_control(error, closes):
    result, target, warehouse = closes[error].values()

    assert result.code == 3, result.stderr
    env = result.json
    assert env["status"] == "fail" and env["month"] == MONTH
    failed = {f["id"]: f for f in env["failed_controls"]}
    assert NAMED[error] in failed, f"{error} not named by {NAMED[error]}: {sorted(failed)}"
    detail = "\n".join(failed[NAMED[error]]["detail"])
    assert expected_number_id(error, target) in detail
    assert row_hash(warehouse, error, target["key"]) in detail
    if error == "rounding_slip":
        assert list(failed) == ["CTL-TIE-INF"]  # a one-cent slip is the tie-out's alone
    assert UNRELATED.isdisjoint(failed)
    # the month stays open: nothing can be explained from it
    explained = margin("explain", f"cp/{MONTH}/{target.get('account_id', 'acct_0001')}/revenue",
                       "--warehouse", str(warehouse), "--json")
    assert explained.code == 4, explained.stderr


def test_the_clean_books_pass(closes):
    result = closes["clean"]["result"]
    assert result.code == 0, result.stderr
    assert all(c["status"] == "pass" for c in result.json["controls"])
    ids = {c["id"] for c in result.json["controls"]}
    assert {"CTL-REV-DUP", "CTL-CUTOFF", "CTL-SIGN"} <= ids


def test_inject_is_seeded_and_records_the_seed_in_the_manifest(tmp_path):
    first = inject("duplicate_invoice", tmp_path / "a" / "books")
    again = inject("duplicate_invoice", tmp_path / "b" / "books")
    assert first.json["target"] == again.json["target"]
    assert (tmp_path / "a" / "books" / "invoices.csv").read_bytes() == (tmp_path / "b" / "books" / "invoices.csv").read_bytes()
    manifest = (tmp_path / "a" / "books.manifest.sha256").read_text()
    assert "# inject_seed: 20261013" in manifest and "# injected_error: duplicate_invoice" in manifest
    assert "# seed: 20261013" in manifest  # the books' own header is kept
    others = {inject("duplicate_invoice", tmp_path / f"s{s}" / "books", "--seed", str(s)).json["target"]["key"]
              for s in range(1, 8)}
    assert len(others) > 1  # the seed moves the choice


@pytest.mark.parametrize("error", ERRORS)
def test_inject_changes_only_the_one_row_in_one_file(error, tmp_path):
    out = tmp_path / "books"
    target = inject(error, out).json["target"]
    changed = [p.name for p in sorted(MINI_BOOKS.iterdir()) if p.read_bytes() != (out / p.name).read_bytes()]
    assert changed == [target["file"]]
    before = (MINI_BOOKS / target["file"]).read_text().splitlines()
    after = (out / target["file"]).read_text().splitlines()
    added = len(after) - len(before)
    assert added == (1 if error in ("duplicate_invoice", "alloc_twice") else 0)
    assert sum(1 for line in after if line not in before) == 1


def test_inject_refuses_a_used_output_directory_and_an_unknown_error(tmp_path):
    out = tmp_path / "books"
    assert inject("sign_flip", out).code == 0
    again = inject("sign_flip", out)
    assert again.code == 2 and json.loads(again.stdout)["status"] == "fail"
    assert margin("inject", "--fixture", "mini", "--error", "nonsense", "--out", str(tmp_path / "x")).code == 2
    assert re.search(r"not empty", again.json["message"])


def test_an_exact_reimported_invoice_line_fails_the_close_naming_ctl_rev_dup(tmp_path):
    """The line as booked, line_id and all (what `inject` does not do): the dbt unique test used to
    fire first and the close exited 70; the control owns it and the close exits 3."""
    books = clean_books(tmp_path)
    lines = (books / "invoices.csv").read_text().splitlines(keepends=True)
    row = next(line for line in lines[1:] if f",{MONTH}," in line)
    with open(books / "invoices.csv", "a") as f:
        f.write(row if row.endswith("\n") else row + "\n")

    result = close(books, tmp_path / "w.duckdb")

    assert result.code == 3, result.stderr
    failed = {f["id"]: f for f in result.json["failed_controls"]}
    assert "CTL-REV-DUP" in failed, sorted(failed)
    assert "invoice_id and line_id" in "\n".join(failed["CTL-REV-DUP"]["detail"])
    assert UNRELATED.isdisjoint(failed)
