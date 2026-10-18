"""`margin scenario --check`: the scenario against the hand-worked reference in tests/fixtures/scenario.

The check generates the `scenario` preset and closes it in a temporary warehouse, which takes a
while, so the passing run is shared by the tests that only read its outcome.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from conftest import ROOT, margin

REFERENCE = ROOT / "tests" / "fixtures" / "scenario"
SERIES = ROOT / "data" / "reference" / "list_price_series.csv"
SCHEMA = json.loads((ROOT / "src" / "margin" / "schemas" / "envelope.schema.json").read_text())
CHANGED_ID = "scenario/2026-03/full/Q1/acct_0001/contribution_profit"


def snapshot(*roots: Path) -> set[Path]:
    return {p for root in roots if root.exists() for p in root.rglob("*")}


@pytest.fixture(scope="module")
def passing(tmp_path_factory):
    """One passing check with a private working directory and TMPDIR, no warehouse named and no
    sign-off directory given; (result, working directory, TMPDIR, tree before, tree after)."""
    work, tmp = tmp_path_factory.mktemp("cwd"), tmp_path_factory.mktemp("tmp")
    trees = (ROOT / "warehouse", ROOT / "data" / "books" / "scenario", ROOT / "data" / "truth" / "scenario")
    before = snapshot(*trees)
    result = margin("scenario", "--check", "--json", cwd=work,
                    env={"MARGIN_WAREHOUSE": "", "MARGIN_SIGNOFF_DIR": "", "TMPDIR": str(tmp)})
    return result, work, tmp, before, snapshot(*trees)


def test_the_check_passes_and_its_envelope_validates(passing):
    result = passing[0]
    assert result.code == 0, result.stderr
    jsonschema.Draft202012Validator(SCHEMA).validate(result.json)
    summary = json.loads((REFERENCE / "summary.json").read_text())
    assert result.json["accounts"] == 3
    assert result.json["computed_change"] == summary["computed_change"]
    assert result.json["checks"] == [{"id": "CHK-SCENARIO", "status": "pass", "violating_rows": 0}]


def test_the_check_leaves_no_warehouse_or_books_outside_its_temporary_directory(passing):
    _, work, tmp, before, after = passing
    assert list(work.iterdir()) == []
    assert list(tmp.iterdir()) == []
    assert after == before


def test_a_reference_off_by_one_cent_fails_the_check_naming_the_number(tmp_path):
    reference = tmp_path / "reference"
    shutil.copytree(REFERENCE, reference)
    with (reference / "expected.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if (r["case"], r["quarter"], r["account_id"]) == ("full", "Q1", "acct_0001"))
    row["contribution_profit_cents"] = str(int(row["contribution_profit_cents"]) + 1)
    with (reference / "expected.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    result = margin("scenario", "--check", "--json", "--reference", str(reference))

    assert result.code == 3
    failed = result.json["failed_controls"]
    assert [c["id"] for c in failed] == ["CHK-SCENARIO"]
    assert failed[0]["violating_rows"] == 1
    assert failed[0]["detail"][0].startswith(f"{CHANGED_ID}: ")
    jsonschema.Draft202012Validator(SCHEMA).validate(result.json)


def test_a_series_with_a_missing_period_fails_the_check(tmp_path):
    series = tmp_path / "series.csv"
    lines = SERIES.read_text().splitlines(keepends=True)
    series.write_text("".join(line for line in lines if not line.startswith("medium,gpt-5.5,2026-06,")))

    result = margin("scenario", "--check", "--json", env={"MARGIN_LIST_PRICE_SERIES": str(series)})

    assert result.code == 3
    failed = result.json["failed_controls"]
    assert failed[0]["id"] == "CHK-SCENARIO"
    assert failed[0]["detail"] == ["medium 2026-06: missing row"]


@pytest.mark.parametrize("args", [(), ("--month", "2026-03", "--check")])
def test_exactly_one_of_month_and_check_is_required(args):
    result = margin("scenario", "--json", *args)
    assert result.code == 2
    assert result.json["status"] == "fail"
