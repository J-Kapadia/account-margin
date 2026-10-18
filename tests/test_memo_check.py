"""`memo --check` and `--crawl` on copies of the closed `mini` fixture with the scenario run.

The check passes on the page the build wrote and names what is wrong when the page, a record or the
site is changed: a forged value or digest exits 6 naming the ID, a numeral or a quotation outside the
registries and a broken link exit 3 under their own check ID, and a link to a file a later step writes
is skipped.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, connect, margin, shared_dir
from margin import memo_check

MONTH = "2026-03"
OTHER = "2026-04"
CITE = '<span data-literal="cite" data-ref-id="ref/list_price_series/large">7.5000</span>'
LEAF = f"cp/{MONTH}/acct_0001/revenue"
CHECKS = ("CHK-MEMO-NUM", "CHK-MEMO-QUOTE", "CHK-MEMO-SITE")


@pytest.fixture(scope="module")
def scenario_warehouse(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for two months; read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        for month in (MONTH, OTHER):
            result = margin("scenario", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                            "--warehouse", str(directory / "w.duckdb"))
            assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "memo_check_warehouse", build) / "w.duckdb"


@pytest.fixture(scope="module")
def built_site(tmp_path_factory, scenario_warehouse) -> Path:
    """A site with the memos of two of the fixture's three months; read-only for the tests."""
    def build(directory: Path) -> None:
        for month in (MONTH, OTHER):
            result = margin("memo", "--month", month, "--out", str(directory), "--warehouse", str(scenario_warehouse))
            assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "memo_check_site", build)


@pytest.fixture
def site(built_site, tmp_path) -> Path:
    """A private copy of the built site, for tests that change it."""
    copy = tmp_path / "site"
    shutil.copytree(built_site, copy)
    return copy


@pytest.fixture
def warehouse(scenario_warehouse, tmp_path) -> Path:
    copy = tmp_path / "w.duckdb"
    shutil.copy(scenario_warehouse, copy)
    return copy


def check(warehouse: Path, out: Path, *extra: str):
    return margin("memo", "--month", MONTH, "--out", str(out), "--warehouse", str(warehouse), "--json", *extra)


def statuses(result) -> dict[str, str]:
    return {c["id"]: c["status"] for c in result.json["controls"]}


# ------------------------------------------------------------------ the command


def test_check_passes_and_lists_its_controls(scenario_warehouse, tmp_path):
    result = check(scenario_warehouse, tmp_path / "site", "--check")
    assert result.code == 0, result.stderr
    assert statuses(result) == {c: "pass" for c in CHECKS}
    assert (tmp_path / "site" / "index.html").is_file() and (tmp_path / "site" / MONTH / "index.html").is_file()


def test_crawl_implies_check_and_adds_its_control(scenario_warehouse, tmp_path):
    result = check(scenario_warehouse, tmp_path / "site", "--crawl")
    assert result.code == 0, result.stderr
    assert statuses(result) == {c: "pass" for c in (*CHECKS, "CHK-CRAWL")}


def test_a_forged_value_exits_6_naming_the_id(warehouse, tmp_path):
    with connect(str(warehouse)) as con:
        con.execute("UPDATE prov.records SET value_cents = value_cents + 1 WHERE number_id = ?", [LEAF])
    result = check(warehouse, tmp_path / "site", "--check")
    assert result.code == 6
    assert LEAF in result.json["message"]


@pytest.mark.slow
def test_a_forged_total_exits_6_because_the_records_no_longer_hash_to_the_close(warehouse, tmp_path):
    with connect(str(warehouse)) as con:
        con.execute("UPDATE prov.records SET value_cents = value_cents + 1 WHERE number_id = ?",
                    [f"rollup/{MONTH}/revenue"])
    result = check(warehouse, tmp_path / "site", "--check")
    assert result.code == 6
    assert f"result_hash/{MONTH}" in result.json["message"]


@pytest.mark.slow
def test_a_forged_digest_exits_6_naming_the_id(warehouse, tmp_path):
    forged = f"pnl/{MONTH}/4000"
    with connect(str(warehouse)) as con:
        con.execute("UPDATE prov.records SET input_digest = repeat('f', 64) WHERE number_id = ?", [forged])
    result = check(warehouse, tmp_path / "site", "--check")
    assert result.code == 6
    assert f"digest mismatch for {forged}" in result.json["message"]


def test_a_numeral_in_prose_fails_chk_memo_num(scenario_warehouse, site):
    page = site / MONTH / "index.html"
    page.write_text(page.read_text().replace("<h3>Headline</h3>", "<h3>Headline</h3><p>We grew 12 accounts.</p>"))
    with connect(str(scenario_warehouse), read_only=True) as con:
        results = memo_check.run(con, MONTH, site)
    by_id = {r["id"]: r for r in results}
    assert by_id["CHK-MEMO-NUM"]["status"] == "fail"
    assert any("12 accounts" in d for d in by_id["CHK-MEMO-NUM"]["detail"])
    assert by_id["CHK-MEMO-QUOTE"]["status"] == "pass"


def test_an_unknown_number_id_fails_chk_memo_num(scenario_warehouse, site):
    page = site / MONTH / "index.html"
    page.write_text(page.read_text().replace(
        "<h3>Headline</h3>",
        '<h3>Headline</h3><p><span data-num-id="cp/2026-03/acct_9999/revenue" data-value="1">$0.01</span></p>'))
    with connect(str(scenario_warehouse), read_only=True) as con:
        results = memo_check.run(con, MONTH, site)
    assert results[0]["id"] == "CHK-MEMO-NUM" and results[0]["status"] == "fail"
    assert any(d.startswith("cp/2026-03/acct_9999/revenue") for d in results[0]["detail"])


def test_a_quotation_fails_chk_memo_quote(scenario_warehouse, site):
    page = site / MONTH / "index.html"
    page.write_text(page.read_text().replace(
        "<h3>Headline</h3>", "<h3>Headline</h3><blockquote>Costs fall every year.</blockquote>"))
    with connect(str(scenario_warehouse), read_only=True) as con:
        results = memo_check.run(con, MONTH, site)
    assert {r["id"]: r["status"] for r in results} == {"CHK-MEMO-NUM": "pass", "CHK-MEMO-QUOTE": "fail",
                                                         "CHK-MEMO-SITE": "pass"}


def test_a_missing_site_file_fails_chk_memo_site(scenario_warehouse, site):
    (site / "index.html").unlink()
    with connect(str(scenario_warehouse), read_only=True) as con:
        results = memo_check.run(con, MONTH, site)
    assert {r["id"]: r["status"] for r in results}["CHK-MEMO-SITE"] == "fail"


# ------------------------------------------------------------------ naked numerals


def test_naked_numerals_allow_years_months_dates_sections_and_identifiers():
    registry = frozenset({"ref/list_price_series/large"})
    for text in ("In 2026 the books closed.", "March 2026 and 2026-03 and 2026-03-31", "Q1 to Q4, n/a",
                 "acct_0001 is flagged", "See section 6 and § 10.2", "Closed on March 31, 2026."):
        assert memo_check.naked_numerals(f"<p>{text}</p>", registry) == [], text
    assert memo_check.naked_numerals("<td>4000</td><td>4100</td>", registry) == []


def test_naked_numerals_flag_counts_and_amounts():
    for text in ("We have 12 accounts.", "Revenue was $1,200.50.", "about 3 in 4 accounts", "p25 of margin"):
        found = memo_check.naked_numerals(f"<p>{text}</p>", frozenset())
        assert found, text
    assert any("12 accounts" in s for s in memo_check.naked_numerals("<p>12 accounts</p>", frozenset()))
    assert memo_check.naked_numerals("<p>code 4000 here</p>", frozenset())  # a GL code outside its column


def test_naked_numerals_skip_registered_spans_and_decode_entities():
    span = '<a href="x"><span data-num-id="rollup/2026-03/revenue" data-value="123456">$1,234.56</span></a>'
    assert memo_check.naked_numerals(f"<p>Revenue {span}.</p>", frozenset()) == []
    assert memo_check.naked_numerals("<p>seller&#39;s view</p>", frozenset()) == []
    assert memo_check.naked_numerals("<style>a { margin: 12px }</style><p>words</p>", frozenset()) == []


def test_a_cite_span_passes_only_when_its_id_is_registered():
    assert memo_check.naked_numerals(f"<p>{CITE}</p>", frozenset({"ref/list_price_series/large"})) == []
    assert memo_check.naked_numerals(f"<p>{CITE}</p>", frozenset())
    assert memo_check.naked_numerals(f"<p>{CITE}</p>", frozenset({"cp/2026-03/acct_0001/revenue"}))
    other = CITE.replace("ref/list_price_series/large", "cfg/policy")
    assert memo_check.naked_numerals(f"<p>{other}</p>", frozenset({"cfg/policy"}))  # a cite names a ref/ ID


# ------------------------------------------------------------------ quotes


def test_a_blockquote_is_unregistered_until_the_registry_holds_it():
    html = "<blockquote>Prices only go down.</blockquote>"
    assert memo_check.unregistered_quotes(html, []) == ["Prices only go down."]
    assert memo_check.unregistered_quotes(html, ["Prices only go down."]) == []


def test_attributed_wording_is_unregistered():
    for text in ("According to a vendor, inference is cheap.", "As Smith put it, costs fall.",
                 "Jane Doe writes that margins narrow."):
        assert memo_check.unregistered_quotes(f"<p>{text}</p>", []), text
    assert memo_check.unregistered_quotes("<p>The memo writes its prose from templates. It writes no commentary.</p>", []) == []


def test_a_long_quoted_string_is_unregistered_and_a_short_one_is_not():
    assert memo_check.unregistered_quotes('<p>He said "costs will keep falling for a long time".</p>', [])
    assert memo_check.unregistered_quotes('<p>The "signed rule" applies.</p>', []) == []
    assert memo_check.unregistered_quotes("<p>The &quot;signed rule&quot; applies.</p>", []) == []


def test_the_registry_is_empty_and_an_entry_needs_all_three_fields(tmp_path):
    assert memo_check.load_quotes() == []
    (tmp_path / "quotes.yaml").write_text(
        "quotes:\n  - text: Costs fall.\n    verified_on: 2026-09-01\n    source_url: https://example.com/a\n"
        "  - text: No source given.\n    verified_on: 2026-09-01\n")
    assert memo_check.load_quotes(tmp_path) == ["Costs fall."]


# ------------------------------------------------------------------ the crawl


def test_a_crawl_of_a_site_built_for_two_of_three_months_passes(built_site):
    assert sorted(p.name for p in built_site.iterdir() if p.name.startswith("2026")) == [MONTH, OTHER]
    assert memo_check.crawl(built_site) == []


def test_deleting_an_explain_page_fails_the_crawl_naming_the_path(scenario_warehouse, site):
    (site / "explain" / "rollup" / MONTH / "revenue.html").unlink()
    with connect(str(scenario_warehouse), read_only=True) as con:
        results = memo_check.run(con, MONTH, site, crawl_site=True)
    crawl = results[-1]
    assert crawl["id"] == "CHK-CRAWL" and crawl["status"] == "fail"
    assert any("explain/rollup/2026-03/revenue.html" in d for d in crawl["detail"])


def test_the_command_exits_3_naming_chk_crawl_for_a_broken_link(warehouse, site):
    # A page of another month, left from an earlier build, links to a page that is gone.
    stale = site / "2026-02"
    stale.mkdir()
    (stale / "index.html").write_text('<!doctype html><a href="../explain/cp/2026-02/acct_0001/revenue.html">x</a>')
    result = check(warehouse, site, "--crawl")
    assert result.code == 3
    assert [c["id"] for c in result.json["failed_controls"]] == ["CHK-CRAWL"]
    assert any("explain/cp/2026-02/acct_0001/revenue.html" in d for d in result.json["failed_controls"][0]["detail"])


def test_the_deferred_agreement_link_is_skipped(site):
    assert not (site / MONTH / "agreement.json").exists()
    assert 'data-deferred="agreement"' in (site / MONTH / "index.html").read_text()
    assert memo_check.crawl(site) == []


def test_only_the_marked_link_is_skipped(tmp_path):
    (tmp_path / "index.html").write_text(
        '<a href="agreement.json" data-deferred="agreement">a</a><a href="other.json">b</a>'
        '<a href="https://example.com/x">c</a><a href="#here">d</a><a href="#nowhere">e</a><h2 id="here">h</h2>'
        '<a href="sub/">f</a><a href="../outside.html">g</a>')
    broken = memo_check.crawl(tmp_path)
    assert len(broken) == 4
    assert any("other.json" in b for b in broken) and any("#nowhere" in b for b in broken)
    assert any("sub/" in b for b in broken) and any("../outside.html" in b for b in broken)
    assert not any("agreement.json" in b for b in broken)
