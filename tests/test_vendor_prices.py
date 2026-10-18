"""The vendor price file: its shape, its citations, and how the close loads it."""

from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pytest

from margin import load, prov

VENDORS = Path(__file__).resolve().parents[1] / "data" / "reference" / "vendor_prices.csv"
COLUMNS = ["vendor", "product", "annual_list_price_usd", "source_url", "retrieved_on", "notes"]
NOT_A_PRICING_PAGE = ("web.archive.org", "archive.org", "archive.ph", "archive.today", "webcache.googleusercontent.com")
SEARCH_PATHS = ("/search", "/results")


@pytest.fixture(scope="module")
def rows():
    with VENDORS.open(newline="") as f:
        return list(csv.DictReader(f))


def test_the_file_has_exactly_these_columns():
    with VENDORS.open(newline="") as f:
        assert next(csv.reader(f)) == COLUMNS


def test_the_file_has_at_least_three_vendors(rows):
    assert len(rows) >= 3


def test_vendors_are_unique_lower_case_and_path_safe(rows):
    names = [r["vendor"] for r in rows]
    assert len(names) == len(set(names))
    for name in names:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", name), name
        assert name == name.lower(), name


def test_every_source_is_a_vendor_pricing_page_over_https(rows):
    for r in rows:
        url = urlparse(r["source_url"])
        assert url.scheme == "https", r["source_url"]
        assert url.hostname and "." in url.hostname, r["source_url"]
        assert url.hostname not in NOT_A_PRICING_PAGE, r["source_url"]
        assert not url.path.startswith(SEARCH_PATHS) and "q=" not in url.query, r["source_url"]


def test_every_retrieval_date_is_an_iso_date_not_after_today(rows):
    for r in rows:
        day = date.fromisoformat(r["retrieved_on"])
        assert day.isoformat() == r["retrieved_on"]
        assert day <= date.today(), r["retrieved_on"]


def test_every_price_is_a_positive_amount_with_at_most_two_places(rows):
    for r in rows:
        price = Decimal(r["annual_list_price_usd"])
        assert price.is_finite() and price > 0, r["annual_list_price_usd"]
        assert price.as_tuple().exponent >= -2, r["annual_list_price_usd"]


def test_every_row_names_its_product_and_says_what_the_price_covers(rows):
    for r in rows:
        assert r["product"].strip() and r["notes"].strip(), r["vendor"]


def test_the_close_loads_one_hashed_row_per_vendor(rows):
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA raw")
    load.load_reference(con, Path("/nonexistent/prices.csv"))
    total, hashes = con.execute(
        "SELECT count(*), count(DISTINCT _row_hash) FROM raw.ref_vendor_prices"
    ).fetchone()
    assert total == len(rows)
    assert hashes == len(rows)
    assert {v for (v,) in con.execute("SELECT vendor FROM raw.ref_vendor_prices").fetchall()} == {
        r["vendor"] for r in rows
    }


def test_every_vendor_has_a_build_versus_buy_provenance_id(rows):
    for r in rows:
        assert prov.family_of("bvb/vendor_annual_cost/" + r["vendor"]) == "bvb"
