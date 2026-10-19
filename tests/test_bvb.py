"""The build-vs-buy figures: `bvb/` records written by `close` from `build_cost.csv` and `vendor_prices.csv`.

A close points at a reference directory holding synthetic copies of the two files (the real totals
are committed separately); the figures are checked against values computed here from the fixture.
"""

from __future__ import annotations

import csv
import shutil
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from conftest import Result, ROOT, close_mini, connect, margin, shared_dir
from margin import prov
from margin.errors import MarginError

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "reference"
MONTH = "2026-03"
FIXED = ("build_cost", "build_cost_amortised", "run_cost_annual", "maintenance_allowance")


def cents(text: str) -> int:
    return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def fixture_rows(name: str, key: str) -> dict[str, dict]:
    with open(FIXTURE_DIR / name, newline="", encoding="utf-8") as handle:
        return {row[key]: row for row in csv.DictReader(handle)}


def expected() -> dict[str, int]:
    """Every `bvb/` figure, computed from the fixture files."""
    totals = fixture_rows("build_cost.csv", "measure")
    build = cents(totals["build_cost_usd"]["value"])
    out = {
        "bvb/build_cost": build,
        "bvb/build_cost_amortised": build,
        "bvb/run_cost_annual": 12 * cents(totals["run_cost_monthly_usd"]["value"]),
        "bvb/maintenance_allowance": int((Decimal(build) / 10).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)),
    }
    for vendor, row in fixture_rows("vendor_prices.csv", "vendor").items():
        out[f"bvb/vendor_annual_cost/{vendor}"] = cents(row["annual_list_price_usd"])
    return out


@pytest.fixture
def reference_dir(tmp_path) -> Path:
    """The committed reference tables plus the two synthetic files."""
    directory = tmp_path / "reference"
    directory.mkdir()
    for source in sorted((ROOT / "data" / "reference").glob("*.csv")):
        shutil.copy(source, directory / source.name)
    for source in sorted(FIXTURE_DIR.glob("*.csv")):
        shutil.copy(source, directory / source.name)
    return directory


def close_with(reference: Path, warehouse: Path, *extra: str):
    return close_mini(warehouse, MONTH, *extra, env={"MARGIN_REFERENCE_DIR": str(reference)})


def bvb(warehouse: Path) -> dict[str, tuple]:
    with connect(str(warehouse), read_only=True) as con:
        return {
            r[0]: r[1:] for r in con.execute(
                "SELECT number_id, value_cents, unit, step, month, source_query FROM prov.records "
                "WHERE number_id LIKE 'bvb/%' ORDER BY 1"
            ).fetchall()
        }


@pytest.fixture(scope="session")
def closed_with_fixture_master(tmp_path_factory) -> Path:
    """One close of March on the reference tables plus the synthetic files, for the whole run; the
    directory holds the warehouse and the close's JSON. Read-only for the tests."""
    def build(directory: Path) -> None:
        reference = directory / "reference"
        reference.mkdir()
        for source in (*sorted((ROOT / "data" / "reference").glob("*.csv")), *sorted(FIXTURE_DIR.glob("*.csv"))):
            shutil.copy(source, reference / source.name)
        result = close_with(reference, directory / "w.duckdb")
        assert result.code == 0, result.stderr
        (directory / "close.json").write_text(result.stdout)

    return shared_dir(tmp_path_factory, "bvb_closed", build)


@pytest.fixture
def closed_with_fixture(closed_with_fixture_master, tmp_path):
    """A private copy of the shared close's warehouse, and the close's result."""
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(closed_with_fixture_master / "w.duckdb", warehouse)
    result = Result(0, (closed_with_fixture_master / "close.json").read_text(), "")
    return warehouse, result


def test_a_close_writes_the_five_figures_and_one_per_vendor(closed_with_fixture):
    warehouse, _ = closed_with_fixture
    records = bvb(warehouse)
    assert sorted(records) == sorted(expected())
    assert set(f"bvb/{item}" for item in FIXED) <= set(records)
    assert len([i for i in records if i.startswith("bvb/vendor_annual_cost/")]) == 2


def test_the_figures_match_the_fixture_arithmetic(closed_with_fixture):
    warehouse, _ = closed_with_fixture
    records = bvb(warehouse)
    assert {k: v[0] for k, v in records.items()} == expected()
    for value in records.values():
        assert isinstance(value[0], int) and value[1] == "cents" and value[2] == "margin.prov.bvb"
        assert value[3] is None  # no month


def test_the_fixture_has_one_vendor_either_side_of_the_threshold(closed_with_fixture):
    figures = expected()
    threshold = sum(figures[f"bvb/{i}"] for i in ("build_cost_amortised", "run_cost_annual", "maintenance_allowance"))
    vendors = sorted(v for k, v in figures.items() if k.startswith("bvb/vendor_annual_cost/"))
    assert vendors[0] < threshold < vendors[1]


def test_explain_reaches_the_source_row(closed_with_fixture):
    warehouse, _ = closed_with_fixture
    result = margin("explain", "bvb/build_cost", "--fixture", "mini", "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr
    assert "= 1200000 cents" in result.stdout
    assert "raw.ref_build_cost" in result.stdout and '"measure": "build_cost_usd"' in result.stdout
    assert '"value": "12000.00"' in result.stdout


def test_a_derived_figure_explains_down_to_the_build_cost(closed_with_fixture):
    warehouse, _ = closed_with_fixture
    result = margin("explain", "bvb/maintenance_allowance", "--fixture", "mini", "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr
    assert "bvb/build_cost = 1200000 cents" in result.stdout
    assert "raw.ref_build_cost" in result.stdout


def test_a_vendor_figure_explains_to_its_row(closed_with_fixture):
    warehouse, _ = closed_with_fixture
    result = margin("explain", "bvb/vendor_annual_cost/vendor_alpha", "--fixture", "mini", "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr
    assert "= 900000 cents" in result.stdout and "raw.ref_vendor_prices" in result.stdout


def test_provenance_passes_on_a_second_close(reference_dir, closed_with_fixture):
    warehouse, first = closed_with_fixture  # the first close, shared; the second runs on a copy of it
    second = close_with(reference_dir, warehouse, "--check")
    assert first.code == 0 and second.code == 0, second.stderr
    statuses = {c["id"]: c["status"] for c in second.json["controls"]}
    assert statuses["CTL-PROV"] == "pass"
    assert bvb(warehouse).keys() == expected().keys()


def test_the_figures_sit_outside_the_result_hash(closed_with_fixture, tmp_path):
    _, with_files = closed_with_fixture
    bare = tmp_path / "bare"
    bare.mkdir()
    for source in sorted((ROOT / "data" / "reference").glob("*.csv")):
        shutil.copy(source, bare / source.name)
    result = close_with(bare, tmp_path / "bare.duckdb")
    assert result.code == 0, result.stderr
    assert result.json["result_hash"] == with_files.json["result_hash"]


def test_a_close_without_the_totals_writes_no_figures(reference_dir, tmp_path):
    (reference_dir / "build_cost.csv").unlink()
    warehouse = tmp_path / "w.duckdb"
    result = close_with(reference_dir, warehouse)
    assert result.code == 0, result.stderr
    assert bvb(warehouse) == {}


def memory_records(totals: list[tuple[str, str]], vendors: list[tuple[str, str]] | None = None) -> list[dict]:
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE TABLE raw.ref_build_cost (measure VARCHAR, value VARCHAR, _row_hash VARCHAR)")
    for i, (measure, value) in enumerate(totals):
        con.execute("INSERT INTO raw.ref_build_cost VALUES (?, ?, ?)", [measure, value, f"h{i}"])
    if vendors is not None:
        con.execute(
            "CREATE TABLE raw.ref_vendor_prices (vendor VARCHAR, annual_list_price_usd VARCHAR, _row_hash VARCHAR)"
        )
        for i, (vendor, price) in enumerate(vendors):
            con.execute("INSERT INTO raw.ref_vendor_prices VALUES (?, ?, ?)", [vendor, price, f"v{i}"])
    return prov.bvb_records(con, "run", SimpleNamespace(signer="s", ref="r"))


def test_the_maintenance_allowance_rounds_half_even():
    for build, allowance in (("12345.65", 123456), ("12345.75", 123458), ("0.05", 0), ("0.15", 2), ("0.25", 2)):
        records = {r["number_id"]: r for r in memory_records(
            [("build_cost_usd", build), ("run_cost_monthly_usd", "1.00")])}
        assert records["bvb/maintenance_allowance"]["value_cents"] == allowance, build


def test_a_total_that_is_not_a_dollar_amount_exits_4():
    with pytest.raises(MarginError) as err:
        memory_records([("build_cost_usd", "lots"), ("run_cost_monthly_usd", "1.00")])
    assert err.value.exit_code == 4


def test_a_missing_run_cost_exits_4():
    with pytest.raises(MarginError) as err:
        memory_records([("build_cost_usd", "100.00")])
    assert err.value.exit_code == 4 and "run_cost_monthly_usd" in err.value.message


def test_a_vendor_listed_twice_exits_4():
    with pytest.raises(MarginError) as err:
        memory_records(
            [("build_cost_usd", "100.00"), ("run_cost_monthly_usd", "1.00")], [("a", "1.00"), ("a", "2.00")]
        )
    assert err.value.exit_code == 4
