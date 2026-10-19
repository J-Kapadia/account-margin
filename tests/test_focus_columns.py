"""The six optional FOCUS columns of the cloud bill: how they load, the cents staging forms from
them, and what CTL-PRICE-PIN says about a row that carries them badly. A book without the
columns closes as it always did.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_PRICES, build_mini_warehouse, connect, margin

SIX = ["ListCost", "ContractedCost", "ChargeCategory", "CommitmentDiscountCategory",
       "CommitmentDiscountType", "RegionId"]
MARCH = "2026-03"


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def copy_books(tmp_path: Path) -> Path:
    target = tmp_path / "b" / "books"
    shutil.copytree(MINI_BOOKS, target)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", target.parent / "books.manifest.sha256")
    return target


def with_columns(books: Path, columns: list[str] = SIX, change=None) -> dict[str, dict]:
    """Append `columns` to the cloud bill: every row a Usage row whose list and contracted cost
    equal its effective cost. `change(rows)` then edits the rows; returns the rows by line_id."""
    fields, rows = _read(books / "cloud_focus.csv")
    for row in rows:
        row.update(ListCost=row["EffectiveCost"], ContractedCost=row["EffectiveCost"],
                   ChargeCategory="Usage", CommitmentDiscountCategory="", CommitmentDiscountType="",
                   RegionId="region-1")
    if change:
        change(rows)
    _write(books / "cloud_focus.csv", fields + columns, [{k: r[k] for k in fields + columns if k in r} for r in rows])
    return {r["line_id"]: r for r in rows}


def first(rows: list[dict], *, month: str = MARCH, untagged: bool, resource: str | None = None) -> dict:
    return next(
        r for r in rows
        if r["ChargePeriodStart"].startswith(month) and (r["Tags"] == "{}") == untagged
        and (resource is None or r["ResourceId"] == resource)
    )


def shape(rows: list[dict]) -> dict[str, dict]:
    """The rows the staging test pins, by role."""
    covered = first(rows, untagged=False)
    purchase = first(rows, untagged=True, resource="res-gateway-01")
    shared, capital = (r for r in [r for r in rows if r["ChargePeriodStart"].startswith(MARCH)
                                   and r["Tags"] == "{}" and r["ResourceId"] == "res-observe-01"][:2])
    covered.update(ListCost="500.00", ContractedCost="450.00", CommitmentDiscountCategory="Spend",
                   CommitmentDiscountType="Savings Plan")
    purchase.update(ChargeCategory="Purchase", CommitmentDiscountCategory="Spend")
    shared["Tags"] = '{"tenant":"shared"}'
    capital["Tags"] = '{"Tenant":"acct_0009"}'
    return {"covered": covered, "purchase": purchase, "shared": shared, "capital": capital}


@pytest.fixture(scope="module")
def six_books(tmp_path_factory) -> tuple[Path, dict]:
    books = copy_books(tmp_path_factory.mktemp("six"))
    roles: dict = {}
    with_columns(books, change=lambda rows: roles.update(shape(rows)))
    return books, roles


@pytest.fixture(scope="module")
def six_warehouse(six_books, tmp_path_factory) -> Path:
    books, _ = six_books
    return build_mini_warehouse(books, tmp_path_factory.mktemp("six_wh") / "w.duckdb")


def staging_row(warehouse: Path, line_id: str) -> dict:
    with connect(str(warehouse), read_only=True) as con:
        cur = con.execute("SELECT * FROM stg.stg_cloud_focus WHERE line_id = ?", [line_id])
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def cents(text: str) -> int:
    return round(float(text) * 100)


def test_the_staging_columns_have_the_stated_values(six_books, six_warehouse):
    _, roles = six_books
    covered = staging_row(six_warehouse, roles["covered"]["line_id"])
    assert (covered["list_cents"], covered["contracted_cents"]) == (50000, 45000)
    assert covered["effective_cents"] == covered["billed_cents"] == cents(roles["covered"]["EffectiveCost"])
    assert covered["is_covered"] is True and covered["is_unused_commitment"] is False
    assert covered["benefit_cents"] == 45000 - covered["effective_cents"]
    assert covered["charge_category"] == "Usage" and covered["tag_class"] == "direct"
    assert covered["tenant"] == roles["covered"]["Tags"].split('"')[3] and covered["region_id"] == "region-1"

    purchase = staging_row(six_warehouse, roles["purchase"]["line_id"])
    assert purchase["is_unused_commitment"] is True and purchase["is_covered"] is False
    assert purchase["benefit_cents"] == 0 and purchase["tag_class"] == "untagged"

    shared = staging_row(six_warehouse, roles["shared"]["line_id"])
    assert shared["tag_class"] == "shared" and shared["tenant"] is None
    capital = staging_row(six_warehouse, roles["capital"]["line_id"])
    assert capital["tag_class"] == "untagged" and capital["tenant"] is None

    with connect(str(six_warehouse), read_only=True) as con:
        plain = con.execute(
            "SELECT count(*) FROM stg.stg_cloud_focus WHERE charge_category = 'Usage' AND NOT is_covered"
            " AND list_cents = effective_cents AND contracted_cents = effective_cents AND benefit_cents = 0"
        ).fetchone()[0]
        total = con.execute("SELECT count(*) FROM stg.stg_cloud_focus").fetchone()[0]
    assert total - plain == 2  # the covered row and the purchase row


def test_the_cost_basis_is_row_cents_summed(six_warehouse):
    with connect(str(six_warehouse), read_only=True) as con:
        pool, direct = con.execute(
            "SELECT (SELECT max(pool_cents) FROM intermediate.int_cloud_shared_pools WHERE month = DATE '2026-03-01'),"
            " (SELECT sum(cloud_direct_cents) FROM intermediate.int_cloud_direct WHERE month = DATE '2026-03-01')"
        ).fetchone()
        total = con.execute(
            "SELECT sum(effective_cents) FROM stg.stg_cloud_focus WHERE month = DATE '2026-03-01'"
        ).fetchone()[0]
    assert pool + direct == total


def test_a_book_without_the_columns_stages_every_row_as_plain_usage(mini_warehouse):
    with connect(str(mini_warehouse), read_only=True) as con:
        rows = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE charge_category = 'Usage' AND NOT is_covered"
            " AND NOT is_unused_commitment AND list_cents = effective_cents AND contracted_cents = effective_cents"
            " AND benefit_cents = 0 AND region_id IS NULL AND tag_class <> 'shared') FROM stg.stg_cloud_focus"
        ).fetchone()
    assert rows[0] > 0 and rows[0] == rows[1]


def test_a_header_with_some_of_the_columns_loads_and_the_others_are_null(tmp_path):
    books = copy_books(tmp_path)
    with_columns(books, ["RegionId"])
    warehouse = build_mini_warehouse(books, tmp_path / "w.duckdb")
    with connect(str(warehouse), read_only=True) as con:
        nulls = con.execute(
            'SELECT count(*), count("ListCost"), count("ChargeCategory"), count("RegionId") FROM raw.cloud_focus'
        ).fetchone()
    assert nulls[0] > 0 and nulls[1] == nulls[2] == 0 and nulls[3] == nulls[0]


def close(books: Path, tmp_path: Path, month: str = MARCH):
    return margin(
        "close", "--fixture", "mini", "--books", str(books), "--warehouse", str(tmp_path / "w.duckdb"),
        "--config", str(MINI_CONFIG), "--month", month, "--json", env={"MARGIN_PRICES": str(MINI_PRICES)},
    )


def test_a_book_with_the_columns_closes_with_the_same_profit_rows(six_books, mini_closed_march, tmp_path):
    books, _ = six_books
    result = close(books, tmp_path)
    assert result.code == 0, result.stderr
    assert all(c["status"] == "pass" for c in result.json["controls"])
    assert "CTL-ALLOC-CONSERVE" in {c["id"] for c in result.json["controls"]}
    query = "SELECT * FROM mart.fct_contribution_profit WHERE month = DATE '2026-03-01' ORDER BY ALL"
    with connect(str(tmp_path / "w.duckdb"), read_only=True) as a, connect(str(mini_closed_march), read_only=True) as b:
        assert a.execute(query).fetchall() == b.execute(query).fetchall() != []


def failed_row(result, resource: str) -> bool:
    assert result.code == 3, result.stderr
    failed = {f["id"]: f for f in result.json["failed_controls"]}
    assert "CTL-PRICE-PIN" in failed
    return any(resource in line for line in failed["CTL-PRICE-PIN"]["detail"])


def test_a_covered_row_without_a_contracted_cost_fails_the_price_pin_naming_the_row(tmp_path):
    books = copy_books(tmp_path)
    target = {}

    def change(rows):
        row = first(rows, untagged=False)
        row.update(CommitmentDiscountCategory="Spend", CommitmentDiscountType="Savings Plan", ContractedCost="")
        target.update(row)

    with_columns(books, change=change)
    assert failed_row(close(books, tmp_path), target["ResourceId"])


def test_an_empty_cost_cell_fails_the_price_pin_naming_the_row(tmp_path):
    books = copy_books(tmp_path)
    target = {}

    def change(rows):
        row = first(rows, untagged=True, resource="res-gateway-01")
        row["ListCost"] = ""
        target.update(row)

    with_columns(books, change=change)
    assert failed_row(close(books, tmp_path), target["ResourceId"])


@pytest.mark.parametrize("category", ["Refund", ""])
def test_a_charge_category_outside_the_five_fails_the_price_pin_naming_the_row(tmp_path, category):
    books = copy_books(tmp_path)
    target = {}

    def change(rows):
        row = first(rows, untagged=True, resource="res-gateway-01")
        row["ChargeCategory"] = category
        target.update(row)

    with_columns(books, change=change)
    assert failed_row(close(books, tmp_path), target["ResourceId"])
