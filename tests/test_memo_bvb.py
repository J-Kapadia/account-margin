"""Memo section 7, build versus buy, on copies of the closed `mini` fixture.

With the two reference files present the section lists the measured build cost, the cost to beat and
a verdict per vendor, every figure through `num` and every measure cited to its `ref/` record; each
`bvb/` ID resolves through `explain` and no numeral stands outside the number registry. Without the
files the section is its heading and one sentence in words.
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, ROOT, close_mini, connect, margin
from margin import explain as explain_mod
from test_memo_scenario import CITE, text_of
from test_memo_sections import HEADINGS, NUM_SPAN, naked_numerals, section

MONTH = "2026-03"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "reference"
NO_DATA = "The build cost has not been collected yet, so this section has no figures."


def read_rows(name: str) -> list[dict]:
    with open(FIXTURE_DIR / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def reference_dir(tmp_path: Path, with_fixture: bool) -> Path:
    directory = tmp_path / "reference"
    directory.mkdir()
    for source in sorted((ROOT / "data" / "reference").glob("*.csv")):
        if with_fixture or source.name != "build_cost.csv":
            shutil.copy(source, directory / source.name)
    if with_fixture:
        for source in sorted(FIXTURE_DIR.glob("*.csv")):
            shutil.copy(source, directory / source.name)
    return directory


def build_memo(tmp_path: Path, with_fixture: bool) -> tuple[Path, Path, str]:
    """Close `mini`, run the scenario and build the memo; (warehouse, site, page)."""
    reference = reference_dir(tmp_path, with_fixture)
    env = {"MARGIN_REFERENCE_DIR": str(reference)}
    warehouse, site = tmp_path / "w.duckdb", tmp_path / "site"
    assert close_mini(warehouse, MONTH, env=env).code == 0
    result = margin("scenario", "--fixture", "mini", "--month", MONTH, "--json", "--config", str(MINI_CONFIG),
                    "--warehouse", str(warehouse), env=env)
    assert result.code == 0, result.stderr
    result = margin("memo", "--month", MONTH, "--out", str(site), "--warehouse", str(warehouse), env=env)
    assert result.code == 0, result.stderr
    return warehouse, site, (site / MONTH / "index.html").read_text()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_memo(tmp_path_factory.mktemp("bvb_fixture"), True)


@pytest.fixture(scope="module")
def bare(tmp_path_factory):
    return build_memo(tmp_path_factory.mktemp("bvb_bare"), False)


@pytest.fixture(scope="module")
def records(built) -> dict[str, tuple]:
    with connect(str(built[0]), read_only=True) as con:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT number_id, value_cents, value_text FROM prov.records").fetchall()}


def vendor_rows(html: str) -> dict[str, str]:
    found = re.search(r'<table id="vendor-verdicts">(.*?)</table>', html, re.S)
    assert found, "no vendor-verdicts table"
    return {m.group(1): m.group(0) for m in re.finditer(r'<tr data-vendor="([^"]+)".*?</tr>', found.group(1), re.S)}


def test_the_sections_keep_their_order(built):
    assert re.findall(r"<h2>(.*?)</h2>", built[2]) == HEADINGS


def test_the_method_is_stated_in_words(built):
    text = text_of(section(built[2], "build-vs-buy"))
    for phrase in (
        "annual list price is below the build cost spread over twelve months",
        "plus twelve months of run cost",
        "plus a maintenance allowance",
        "tenth of the build cost per year",
    ):
        assert phrase in text, phrase
    assert "has not been collected" not in text


def test_the_build_cost_table_cites_each_measure_with_its_source(built, records):
    html = section(built[2], "build-vs-buy")
    table = re.search(r'<table id="build-cost">(.*?)</table>', html, re.S).group(1)
    cites = {ref: shown for _, ref, shown in CITE.findall(table) if shown != "manual"}
    totals = {r["measure"]: r for r in read_rows("build_cost.csv")}
    for measure in ("input_tokens", "output_tokens", "elapsed_hours", "review_minutes", "run_cost_monthly_usd"):
        assert f"ref/build_cost/{measure}" in records and f"ref/build_cost/{measure}" in cites
    assert cites["ref/build_cost/input_tokens"] == "40,000,000"
    assert cites["ref/build_cost/output_tokens"] == "8,000,000"
    assert "100 hours" in table and "600 minutes" in table and "$50" in table
    assert all(r["source"] == "manual" for r in totals.values())
    assert [s for _, _, s in CITE.findall(table)].count("manual") == 6
    shown = {i: v for i, v, _ in NUM_SPAN.findall(table)}
    assert shown == {"bvb/build_cost": str(records["bvb/build_cost"][0])}


def test_the_cost_to_beat_renders_the_derived_figures(built, records):
    table = re.search(r'<table id="cost-to-beat">(.*?)</table>', section(built[2], "build-vs-buy"), re.S).group(1)
    shown = {i: v for i, v, _ in NUM_SPAN.findall(table)}
    assert shown == {f"bvb/{i}": str(records[f"bvb/{i}"][0])
                     for i in ("build_cost_amortised", "run_cost_annual", "maintenance_allowance")}


def test_each_vendor_has_a_verdict_below_or_above_the_threshold(built, records):
    rows = vendor_rows(built[2])
    assert set(rows) == {"vendor_alpha", "vendor_beta"}
    threshold = sum(records[f"bvb/{i}"][0] for i in ("build_cost_amortised", "run_cost_annual", "maintenance_allowance"))
    prices = {v: records[f"bvb/vendor_annual_cost/{v}"][0] for v in rows}
    assert prices["vendor_alpha"] < threshold < prices["vendor_beta"]
    expected = {"vendor_alpha": "buy", "vendor_beta": "build"}
    for vendor, row in rows.items():
        assert f'data-verdict="{expected[vendor]}"' in row
        assert f"<td>{expected[vendor]}</td>" in row
        assert (f'data-num-id="bvb/vendor_annual_cost/{vendor}" data-value="{prices[vendor]}"') in row


def test_each_vendor_row_cites_its_source_and_date(built):
    for row in read_rows("vendor_prices.csv"):
        html = vendor_rows(built[2])[row["vendor"]]
        assert f'<a href="{row["source_url"]}">' in html
        assert (f"../explain/ref/vendor_prices/{row['vendor']}.html", f"ref/vendor_prices/{row['vendor']}",
                row["retrieved_on"]) in CITE.findall(html)
        assert row["product"] in html


def test_every_bvb_number_resolves_through_explain(built):
    warehouse, site, page = built
    ids = sorted({i for i, _, _ in NUM_SPAN.findall(page) if i.startswith("bvb/")})
    assert len(ids) == 6
    with connect(str(warehouse), read_only=True) as con:
        for number_id in ids:
            assert explain_mod.explain(con, number_id)["digest_verified"], number_id
            assert (site / "explain" / f"{number_id}.html").is_file(), number_id
            result = margin("explain", number_id, "--fixture", "mini", "--warehouse", str(warehouse))
            assert result.code == 0, result.stderr


def test_the_cited_records_have_explain_pages(built):
    _, site, page = built
    refs = {ref for _, ref, _ in CITE.findall(section(page, "build-vs-buy"))}
    assert len(refs) == 8  # six build-cost measures and two vendors
    assert all((site / "explain" / f"{ref}.html").is_file() for ref in refs)


def test_no_numeral_stands_outside_the_number_registry(built, records):
    known = frozenset(records)
    assert naked_numerals(built[2], known) == []
    assert naked_numerals(section(built[2], "build-vs-buy"), frozenset())  # the cites are the only numerals there


def test_the_agreement_check_still_passes_on_the_built_site(built):
    warehouse, site, _ = built
    result = margin("agreement", "--month", MONTH, "--check", "--site", str(site), "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr


def test_without_the_reference_files_the_section_is_one_sentence(bare):
    warehouse, site, page = bare
    html = section(page, "build-vs-buy")
    assert re.findall(r"<h2>(.*?)</h2>", page) == HEADINGS
    assert text_of(html).split("Build versus buy", 1)[1].strip() == NO_DATA
    assert not re.search(r"\d", re.sub(r"<[^>]+>", " ", html))
    assert 'data-num-id="bvb/' not in page and "<table" not in html
    with connect(str(warehouse), read_only=True) as con:
        known = frozenset(r[0] for r in con.execute("SELECT number_id FROM prov.records").fetchall())
    assert naked_numerals(page, known) == []
    assert not (site / "explain" / "bvb").exists()
    result = margin("agreement", "--month", MONTH, "--check", "--site", str(site), "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr
