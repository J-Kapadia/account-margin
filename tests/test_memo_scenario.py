"""Memo section 6, the token-price scenario, on copies of the closed `mini` fixture.

`memo` refuses a month the scenario has not run for. After the run the section holds the list-price
trend with each price cited to its reference record, the rate and the computed change, the
three-case recovery counts, a table per case for the firm accounts and another for the soft ones,
and the two disclaimers; every scenario number resolves through `explain`, and no numeral stands
outside the number registry.
"""

from __future__ import annotations

import csv
import re
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, ROOT, connect, margin, shared_dir
from margin import explain as explain_mod
from margin import scenario
from test_memo_sections import NUM_SPAN, naked_numerals, section, table_rows

MONTH = "2026-03"
CASES = ("full", "half", "flat")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
CITE = re.compile(r'<a href="([^"]+)"><span data-literal="cite" data-ref-id="([^"]+)">(.*?)</span></a>')


def run_scenario(warehouse: Path) -> None:
    result = margin("scenario", "--fixture", "mini", "--month", MONTH, "--json", "--config", str(MINI_CONFIG),
                    "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr


@pytest.fixture(scope="module")
def mini_scenario(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for 2026-03; read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        run_scenario(directory / "w.duckdb")

    return shared_dir(tmp_path_factory, "mini_scenario", build) / "w.duckdb"


@pytest.fixture(scope="module")
def page(tmp_path_factory, mini_scenario) -> str:
    def build(directory: Path) -> None:
        result = margin("memo", "--month", MONTH, "--out", str(directory), "--warehouse", str(mini_scenario))
        assert result.code == 0, result.stderr

    return (shared_dir(tmp_path_factory, "memo_scenario", build) / MONTH / "index.html").read_text()


@pytest.fixture(scope="module")
def records(mini_scenario) -> dict[str, tuple]:
    """(value_cents, value_text) of every record, by number ID."""
    with connect(str(mini_scenario), read_only=True) as con:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT number_id, value_cents, value_text FROM prov.records").fetchall()}


@pytest.fixture(scope="module")
def flagged(mini_scenario) -> dict[str, list[str]]:
    """The month's firm and soft accounts from the mart."""
    with connect(str(mini_scenario), read_only=True) as con:
        rows = con.execute("SELECT flag, account_id FROM mart.fct_flags WHERE month = ? AND flag IN ('firm', 'soft') "
                           "ORDER BY account_id", [f"{MONTH}-01"]).fetchall()
    return {kind: [a for k, a in rows if k == kind] for kind in ("firm", "soft")}


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_memo_without_the_scenario_step_exits_4_and_after_it_exits_0(mini_closed_copy, tmp_path):
    out = tmp_path / "site"
    flags = ["--month", MONTH, "--out", str(out), "--warehouse", str(mini_closed_copy), "--json"]
    result = margin("memo", *flags)
    assert result.code == 4
    assert result.json["message"] == f"scenario not run for month {MONTH}"
    assert not out.exists()
    run_scenario(mini_closed_copy)
    result = margin("memo", *flags)
    assert result.code == 0, result.stderr
    assert (out / MONTH / "index.html").is_file()


def test_the_scenario_of_another_month_does_not_satisfy_the_memo(mini_scenario, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copy(mini_scenario, copy)
    result = margin("memo", "--month", "2026-04", "--out", str(tmp_path / "site"), "--warehouse", str(copy), "--json")
    assert result.code == 4
    assert result.json["message"] == "scenario not run for month 2026-04"


def test_the_page_carries_the_computed_change(page, records):
    scenario_section = section(page, "scenario")
    number_id = f"scenario/{MONTH}/summary/computed_change"
    found = {i: (v, shown) for i, v, shown in NUM_SPAN.findall(scenario_section)}
    assert found[number_id][0] == records[number_id][1]
    assert found[f"scenario/{MONTH}/summary/monthly_rate"][0] == records[f"scenario/{MONTH}/summary/monthly_rate"][1]
    assert found[number_id][1].endswith("%") and Decimal(records[number_id][1]) < 0


def test_every_scenario_number_resolves_through_explain(page, mini_scenario):
    ids = sorted({i for i, _, _ in NUM_SPAN.findall(page) if i.startswith("scenario/")})
    assert len(ids) > 20
    with connect(str(mini_scenario), read_only=True) as con:
        for number_id in ids:
            node = explain_mod.explain(con, number_id)
            assert node["digest_verified"], number_id


def test_the_trend_cites_the_series_rows(page, records):
    with open(ROOT / "data" / "reference" / "list_price_series.csv", newline="") as fh:
        series = list(csv.DictReader(fh))
    rows = table_rows_by_tier(section(page, "scenario"))
    assert set(rows) == set(scenario.TIERS)
    for tier, row in rows.items():
        ref = f"ref/list_price_series/{tier}"
        assert ref in records
        cites = CITE.findall(row)
        assert [c[1] for c in cites] == [ref] * 3
        mine = {r["period"]: r for r in series if r["tier"] == tier}
        first, last = mine[scenario.PERIODS[0]], mine[scenario.PERIODS[-1]]
        assert [c[2] for c in cites] == [
            f"{scenario.tier_price(first):.4f}", f"{scenario.tier_price(last):.4f}", last["retrieved_on"]]
        assert first["source_url"] in row and last["source_url"] in row


def table_rows_by_tier(html: str) -> dict[str, str]:
    found = re.search(r'<table id="price-trend">(.*?)</table>', html, re.S)
    assert found, "no price-trend table"
    return {m.group(1): m.group(0) for m in re.finditer(r'<tr data-tier="([^"]+)".*?</tr>', found.group(1), re.S)}


def test_the_three_cases_count_the_accounts_recovered_by_quarter(page, records):
    found = re.search(r'<table id="scenario-recovered">(.*?)</table>', section(page, "scenario"), re.S)
    assert found
    rows = {m.group(1): m.group(0) for m in re.finditer(r'<tr data-case="([^"]+)".*?</tr>', found.group(1), re.S)}
    assert tuple(rows) == CASES
    for case, row in rows.items():
        shown = {i: v for i, v, _ in NUM_SPAN.findall(row)}
        assert shown[f"scenario/{MONTH}/{case}/summary/persistence_pct"] == records[
            f"scenario/{MONTH}/{case}/summary/persistence_pct"][1]
        for quarter in QUARTERS:
            number_id = f"scenario/{MONTH}/{case}/summary/accounts_recovered_{quarter}"
            assert shown[number_id] == str(records[number_id][0])


@pytest.mark.parametrize("case", CASES)
def test_firm_accounts_are_in_the_recovery_table_with_their_recovery_quarters(page, records, flagged, case):
    rows = table_rows(page, f"scenario-firm-{case}")
    assert sorted(rows) == flagged["firm"]
    for account_id, row in rows.items():
        number_id = f"scenario/{MONTH}/{case}/summary/{account_id}/recovery_quarter"
        value, shown = next((v, s) for i, v, s in NUM_SPAN.findall(row) if i == number_id)
        assert value == shown == records[number_id][1] and value in (*QUARTERS, "none")
        margins = [i for i, _, _ in NUM_SPAN.findall(row) if i.endswith("/margin_pct")]
        assert margins == [f"scenario/{MONTH}/{case}/{q}/{account_id}/margin_pct" for q in QUARTERS]


@pytest.mark.parametrize("case", CASES)
def test_soft_accounts_are_listed_apart_from_the_firm_ones(page, flagged, case):
    assert flagged["soft"], "the fixture has soft-flagged accounts"
    soft = table_rows(page, f"scenario-soft-{case}")
    assert sorted(soft) == flagged["soft"]
    assert not set(soft) & set(table_rows(page, f"scenario-firm-{case}"))


def test_the_headline_counts_the_firm_accounts(page, flagged, records):
    scenario_section = section(page, "scenario")
    shown = {i: v for i, v, _ in NUM_SPAN.findall(scenario_section)}
    assert shown[f"rollup/{MONTH}/accounts_firm"] == str(len(flagged["firm"]))
    assert shown[f"rollup/{MONTH}/accounts_soft"] == str(len(flagged["soft"]))


def test_the_counts_agree_with_the_recovery_quarters(records, flagged):
    everyone = flagged["firm"] + flagged["soft"]
    for case in CASES:
        for i, quarter in enumerate(QUARTERS):
            recovered = sum(1 for a in everyone
                            if records[f"scenario/{MONTH}/{case}/summary/{a}/recovery_quarter"][1] in QUARTERS[: i + 1])
            assert records[f"scenario/{MONTH}/{case}/summary/accounts_recovered_{quarter}"][0] == recovered


def test_both_disclaimers_are_in_words(page):
    text = text_of(section(page, "scenario"))
    for phrase in (
        "blend of model vendors' list prices with fixed weights",
        "not Acme's per-model price",
        "not any customer's cost",
        "applies a real published trend to synthetic accounts",
        "illustrative what-if relative to",
        "not a forecast made then",
    ):
        assert phrase in text, phrase


def test_no_numeral_stands_outside_the_number_registry(page, records):
    known = frozenset(records)
    assert naked_numerals(page, known) == []
    assert 'data-literal="cite"' in page
    assert naked_numerals(page, frozenset())  # the cite spans are excluded only for IDs that have a record
