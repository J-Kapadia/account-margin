"""The memo's sections on the closed `mini` fixture, month 2026-03.

Every section heading is present in order; the flagged tables, the cost-with-no-revenue table and
the action table follow the mart; every number resolves through `explain`; the appendix holds
the whole agreement set; and no numeral appears in prose outside the number registry.
"""

from __future__ import annotations

import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, MINI_TRUTH, connect, margin, shared_dir
from margin import explain as explain_mod
from margin import memo as memo_mod
from margin import prov

MONTH = "2026-03"
HEADINGS = [
    "What this memo measures",
    "Contribution profit by account",
    "Flagged accounts",
    "Allocation rule",
    "Actions",
    "Token-price scenario",
    "Build versus buy",
    "Controls and limits",
    "Appendix",
]
MONTH_NAMES = ("January|February|March|April|May|June|July|August|September|October|November|December")
NUM_SPAN = re.compile(r'<span data-num-id="([^"]+)" data-value="([^"]+)">(.*?)</span>')
CITE_SPAN = re.compile(r'<span data-literal="cite" data-ref-id="([^"]+)">.*?</span>')


@pytest.fixture(scope="module")
def mini_scenario(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for 2026-03; read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        result = margin("scenario", "--fixture", "mini", "--month", "2026-03", "--json", "--config", str(MINI_CONFIG),
                        "--warehouse", str(directory / "w.duckdb"))
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "mini_scenario", build) / "w.duckdb"


@pytest.fixture(scope="module")
def page(tmp_path_factory, mini_scenario) -> str:
    def build(directory):
        result = margin("memo", "--month", MONTH, "--out", str(directory), "--warehouse", str(mini_scenario))
        assert result.code == 0, result.stderr

    return (shared_dir(tmp_path_factory, "memo_sections", build) / MONTH / "index.html").read_text()


@pytest.fixture(scope="module")
def mart(mini_closed) -> dict:
    """The month's accounts from the mart and the registry's lower revenue quartile."""
    d = f"{MONTH}-01"
    with connect(str(mini_closed), read_only=True) as con:
        rows = con.execute(
            "SELECT p.account_id, p.revenue_cents, f.flag, f.reason_code FROM mart.fct_contribution_profit p "
            "JOIN mart.fct_flags f USING (month, account_id) WHERE p.month = ? ORDER BY p.account_id", [d],
        ).fetchall()
        p25 = con.execute("SELECT value_cents FROM prov.records WHERE number_id = ?",
                          [f"rollup/{MONTH}/revenue_p25"]).fetchone()[0]
        controls = [r[0] for r in con.execute(
            "SELECT control_id FROM ctl.control_results WHERE month = ? AND run_id = "
            "(SELECT closed_run_id FROM state.close_state WHERE month = ?) ORDER BY control_id", [d, d]).fetchall()]
    return {"accounts": rows, "p25": p25, "controls": controls}


def section(html: str, section_id: str) -> str:
    found = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.S)
    assert found, f"no section {section_id}"
    return found.group(1)


def table_rows(html: str, table_id: str) -> dict[str, str]:
    """The rows of the table with this id, by account."""
    found = re.search(rf'<table id="{table_id}">(.*?)</table>', html, re.S)
    assert found, f"no table {table_id}"
    return {m.group(1): m.group(0) for m in re.finditer(r'<tr data-account="([^"]+)".*?</tr>', found.group(1), re.S)}


def number(row: str, prefix: str) -> tuple[str, str]:
    """(data-value, shown text) of the number in `row` whose ID starts with `prefix`."""
    for number_id, value, shown in NUM_SPAN.findall(row):
        if number_id.startswith(prefix):
            return value, shown
    raise AssertionError(f"no {prefix} in {row}")


def naked_numerals(html: str, known: frozenset[str] = frozenset()) -> list[str]:
    """Numerals left in the page text once the number registry's spans, the cite spans of the `known`
    record IDs and the allowed labels (account IDs, GL codes, ISO months, month names, quarter
    labels, n/a) are removed."""
    body = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    body = NUM_SPAN.sub("", body)
    body = CITE_SPAN.sub(lambda m: "" if m.group(1) in known else m.group(0), body)
    body = re.sub(r"acct_\d{4}", "", body)
    body = re.sub(r"<td>(?:4000|4100|4900)</td>", "", body)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\b\d{4}-\d{2}\b", " ", text)
    text = re.sub(rf"\b(?:{MONTH_NAMES})\b(?:\s+\d{{4}})?", " ", text)
    text = re.sub(r"\bQ[1-4]\b", " ", text)
    text = text.replace("n/a", " ")
    return re.findall(r".{0,20}\d.{0,20}", text)


def test_section_headings_are_present_in_order(page):
    assert re.findall(r"<h2>(.*?)</h2>", page) == HEADINGS


def test_page_one_defines_support_as_cost_of_revenue_and_states_the_data_is_synthetic(page):
    text = re.sub(r"<[^>]+>", " ", section(page, "page-one"))
    assert "Support is part of cost of revenue" in text
    assert "All figures are synthetic" in text
    assert "seller's view" in text
    for number_id in ("revenue", "contribution_profit", "accounts_firm", "accounts_soft"):
        assert f'data-num-id="rollup/{MONTH}/{number_id}"' in section(page, "page-one")


def test_distribution_and_ranked_accounts(page, mart):
    by_account = section(page, "by-account")
    for metric in ("accounts_active", "accounts_zero_revenue", "margin_pct_p25", "margin_pct_p50", "margin_pct_p75"):
        assert f'data-num-id="rollup/{MONTH}/{metric}"' in by_account
    assert "%</span>" in by_account
    ids = {a[0] for a in mart["accounts"]}
    ranked = re.findall(r'<tr data-account="([^"]+)"', by_account)
    assert ranked and set(ranked) <= ids and len(ranked) == len(set(ranked))
    assert all(f'data-num-id="cp/{MONTH}/{a}/trailing3_margin"' in by_account for a in ranked)


def test_firm_and_soft_accounts_carry_their_reason_codes(page):
    planted = json.loads((MINI_TRUTH / "planted_flags.json").read_text())
    firm = {p["account_id"]: p["reason_code"] for p in planted[MONTH]}
    firm_rows = table_rows(page, "firm-flags")
    assert set(firm_rows) == set(firm)
    for account_id, reason in firm.items():
        assert number(firm_rows[account_id], f"flag/{MONTH}/{account_id}")[0] == f"firm:{reason}"
    soft_rows = table_rows(page, "soft-flags")
    assert set(soft_rows) == set(planted["decoys"][MONTH])
    assert all(number(row, "flag/")[0].startswith("soft:") for row in soft_rows.values())


def test_the_zero_revenue_account_is_listed_with_no_margin(page, mart):
    zero = [a[0] for a in mart["accounts"] if a[1] == 0]
    assert zero, "the fixture has a zero-revenue account"
    rows = table_rows(page, "zero-revenue")
    assert set(rows) == set(zero)
    for account_id in zero:
        assert number(rows[account_id], f"cp/{MONTH}/{account_id}/margin_pct") == ("n/a", "n/a")


@pytest.mark.parametrize("reason, revenue, p25, expected", [
    ("DEEP_DISCOUNT", 100, 50, "reprice_at_renewal"),
    ("DEDICATED_CLOUD", 0, 50, "reprice_at_renewal"),
    ("MODEL_MIX", 100, 50, "migrate_per_model_plan"),
    ("SUPPORT_LOAD", 49, 50, "let_churn"),
    ("SUPPORT_LOAD", 50, 50, "reprice"),
    ("SUPPORT_LOAD", -5, None, "reprice"),
    ("NONE", 100, 50, "review"),
])
def test_action_rule(reason, revenue, p25, expected):
    assert memo_mod.action_for(reason, revenue, p25)[0] == expected


def test_each_firm_account_gets_the_action_its_rule_gives(page, mart):
    rows = table_rows(page, "action-table")
    firm = {a[0]: a for a in mart["accounts"] if a[2] == "firm"}
    assert set(rows) == set(firm)
    for account_id, (_, revenue, _, reason) in firm.items():
        if reason == "SUPPORT_LOAD":
            expected = "let_churn" if revenue < mart["p25"] else "reprice"
        else:
            expected = {"DEEP_DISCOUNT": "reprice_at_renewal", "DEDICATED_CLOUD": "reprice_at_renewal",
                        "MODEL_MIX": "migrate_per_model_plan"}[reason]
        assert f'data-action="{expected}"' in rows[account_id]
        assert f"<code>{reason}</code>" in rows[account_id]
    assert {a[3] for a in firm.values()} >= {"SUPPORT_LOAD"}, "the fixture exercises the revenue test"


def test_allocation_section_recommends_by_tokens_and_counts_status_changes(page):
    allocation = section(page, "allocation")
    assert "<code>by_tokens</code>" in allocation
    for rule in ("by_revenue", "equal_usage"):
        assert f'data-num-id="rollup/{MONTH}/accounts_status_change_{rule}"' in allocation
    assert "not evidence about how the shared cloud cost of any real business behaves" in allocation


def test_controls_lists_the_months_controls_by_name(page, mart):
    listed = re.findall(r"<li>(CTL-[A-Z-]+)</li>", section(page, "controls"))
    assert listed == mart["controls"] and "CTL-PROV" in listed


def test_the_agreement_link_is_deferred_and_never_embeds_a_result(page):
    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = []

        def handle_starttag(self, tag, attrs):
            if tag == "a" and dict(attrs).get("data-deferred") == "agreement":
                self.found.append(dict(attrs))

    anchors = Anchors()
    anchors.feed(page)
    assert len(anchors.found) == 1 and anchors.found[0]["href"] == "agreement.json"
    assert "agreement" not in " ".join(i for i, _, _ in NUM_SPAN.findall(page))


def test_the_appendix_holds_exactly_the_agreement_set(page, mart):
    ids = [i for i, _, _ in NUM_SPAN.findall(section(page, "appendix"))]
    accounts = [a[0] for a in mart["accounts"]]
    expected = {f"cp/{MONTH}/{a}/{c}" for a in accounts for c in (*prov.LEAF_COMPONENTS, "contribution_profit", "margin_pct")}
    expected |= {f"rollup/{MONTH}/{m}" for m in prov.ROLLUP_SUMS}
    assert len(ids) == 7 * len(accounts) + 6 == len(set(ids))
    assert set(ids) == expected


def test_every_number_resolves_through_explain_with_its_value(page, mini_scenario):
    found = {(i, v) for i, v, _ in NUM_SPAN.findall(page)}
    assert len(found) > 7 * 12
    with connect(str(mini_scenario), read_only=True) as con:
        for number_id, value in sorted(found):
            node = explain_mod.explain(con, number_id)
            assert node["digest_verified"], number_id
            expected = node["value_cents"] if node["value_cents"] is not None else node["value_text"]
            assert str(expected) == value, number_id


def test_no_numeral_stands_outside_the_number_registry(page, mini_scenario):
    with connect(str(mini_scenario), read_only=True) as con:
        known = frozenset(r[0] for r in con.execute("SELECT number_id FROM prov.records").fetchall())
    assert naked_numerals(page, known) == []
    assert naked_numerals("<p>March 2026, n/a, acct_0001, 2026-03, Q1 and Q4</p>") == []
    assert naked_numerals("<p>about 42 accounts</p>")
    cite = '<span data-literal="cite" data-ref-id="ref/list_price_series/large">7.5000</span>'
    assert naked_numerals(f"<p>{cite}</p>", frozenset({"ref/list_price_series/large"})) == []
    assert naked_numerals(f"<p>{cite}</p>")  # a cite span with no record behind it is a naked numeral


def test_a_script_in_a_display_name_is_escaped(mini_scenario, tmp_path):
    hostile = "<script>alert(1)</script>"
    mini_closed_copy = tmp_path / "w.duckdb"
    shutil.copy(mini_scenario, mini_closed_copy)
    with connect(str(mini_closed_copy)) as con:
        con.execute("UPDATE raw.accounts SET display_name = ? WHERE account_id = 'acct_0006'", [hostile])
    result = margin("memo", "--month", MONTH, "--out", str(tmp_path / "site"), "--warehouse", str(mini_closed_copy))
    assert result.code == 0, result.stderr
    html = (tmp_path / "site" / MONTH / "index.html").read_text()
    assert hostile not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script" not in html
