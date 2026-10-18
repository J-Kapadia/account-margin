"""The site `memo` writes on the closed `mini` fixture: an explain page for every number the
memo renders, the compiled SQL, the cached values and `llms.txt`, all bounded in size."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, connect, margin, shared_dir
from margin import memo as memo_mod
from margin import prov

MONTH, NEXT = "2026-03", "2026-04"
NUM_ID = re.compile(r'data-num-id="([^"]+)"')
LLMS_LINES = [
    r"# fct_contribution_profit",
    r"",
    r"owner: finance owner \(synthetic\)",
    r"definition: .*`contribution_cents\(\)`.*",
    r"",
    r"- \[sql\]\(sql/fct_contribution_profit\.sql\)",
]


def build_site(warehouse: Path, out: Path, month: str = MONTH) -> None:
    result = margin("memo", "--month", month, "--out", str(out), "--warehouse", str(warehouse))
    assert result.code == 0, result.stderr


@pytest.fixture(scope="module")
def mini_scenario(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for both memo months; read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        for month in (MONTH, NEXT):
            result = margin("scenario", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                            "--warehouse", str(directory / "w.duckdb"))
            assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "mini_scenario_months", build) / "w.duckdb"


@pytest.fixture(scope="module")
def site(tmp_path_factory, mini_scenario) -> Path:
    """The site after a memo for 2026-03; read-only for the tests."""
    return shared_dir(tmp_path_factory, "site", lambda d: build_site(mini_scenario, d))


@pytest.fixture(scope="module")
def memo_ids(site) -> list[str]:
    return sorted(set(NUM_ID.findall((site / MONTH / "index.html").read_text())))


def field(page: str, name: str) -> str:
    found = re.search(rf'data-field="{name}">(.*?)</(?:dd|pre|p)>', page, re.S)
    assert found, f"no {name} on the page"
    return re.sub(r"<[^>]+>", "", found.group(1))


def page_of(site: Path, number_id: str) -> str:
    return (site / "explain" / f"{number_id}.html").read_text()


def rows_shown(page: str) -> int:
    return len(re.findall(r"<tr><td>", page))


def test_every_memo_number_has_a_page_with_matching_digests(site, memo_ids):
    assert memo_ids
    for number_id in memo_ids:
        page = page_of(site, number_id)
        assert f"<title>{number_id}</title>" in page
        assert field(page, "stored-digest") == field(page, "recomputed-digest") != ""
        assert field(page, "verified") == "yes"


def test_each_number_links_to_its_page(site, memo_ids):
    html = (site / MONTH / "index.html").read_text()
    for number_id in memo_ids:
        assert f'<a href="../explain/{number_id}.html"><span data-num-id="{number_id}"' in html


def test_a_leaf_over_the_untagged_pool_is_capped_at_25_rows(site, memo_ids):
    capped = [i for i in memo_ids if i.endswith("/cloud_shared") and i.startswith("cp/")
              if "more rows" in page_of(site, i)]
    assert capped, "no cloud_shared page has more than 25 rows"
    for number_id in capped:
        page = page_of(site, number_id)
        assert rows_shown(page) == 25
        total = int(field(page, "input-row-count"))
        assert field(page, "more-rows") == f"{total - 25} more rows: run margin explain {number_id}"
        hashes = re.findall(r"<code>([0-9a-f]{16,})</code>", re.search(r'data-field="rows".*?</table>', page, re.S).group(0))
        assert hashes == sorted(hashes)


def test_a_composite_page_links_to_children_that_exist(site, memo_ids):
    composites = [i for i in memo_ids if i.endswith("/contribution_profit") and i.startswith("cp/")]
    assert composites
    for number_id in composites:
        page = page_of(site, number_id)
        links = re.findall(r'<li><a href="([^"]+)">', re.search(r'data-field="children".*?</ul>', page, re.S).group(0))
        assert links
        for href in links:
            assert (site / "explain" / number_id).parent.joinpath(href).resolve().is_file(), href


def test_every_explain_page_is_under_64_kb(site):
    pages = list((site / "explain").rglob("*.html"))
    assert pages
    assert [str(p) for p in pages if p.stat().st_size >= 64 * 1024] == []


def test_values_csv_holds_the_agreement_set(site, mini_closed):
    with open(site / MONTH / "values.csv", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["number_id", "value"]
    values = dict(rows[1:])
    with connect(str(mini_closed), read_only=True) as con:
        accounts = con.execute("SELECT account_id FROM mart.fct_contribution_profit WHERE month = ?",
                               [f"{MONTH}-01"]).fetchall()
        records = {r[0]: r[1:] for r in con.execute(
            "SELECT number_id, value_cents, value_text FROM prov.records WHERE month = ?", [f"{MONTH}-01"]).fetchall()}
    assert len(rows) - 1 == 7 * len(accounts) + 6 == len(values)
    for number_id, value in values.items():
        cents, text = records[number_id]
        assert value == (text if cents is None else str(cents)), number_id
    assert "n/a" in {v for i, v in values.items() if i.endswith("/margin_pct")}


def test_llms_txt_follows_the_grammar(site):
    lines = (site / "llms.txt").read_text().splitlines()
    patterns = [*LLMS_LINES, r"- \[values 2026-03\]\(2026-03/values\.csv\)"]
    assert len(lines) == len(patterns)
    for line, pattern in zip(lines, patterns):
        assert re.fullmatch(pattern, line), line


def test_compiled_sql_is_the_stored_model(site, mini_closed):
    with connect(str(mini_closed), read_only=True) as con:
        stored = con.execute("SELECT sql FROM state.model_sql WHERE model = 'fct_contribution_profit'").fetchone()[0]
    assert stored.strip()
    assert (site / "sql" / "fct_contribution_profit.sql").read_text() == stored


def test_the_indexes_list_the_months_with_a_memo(tmp_path, mini_scenario):
    out = tmp_path / "site"
    build_site(mini_scenario, out)
    build_site(mini_scenario, out, NEXT)
    for text in ((out / "index.html").read_text(), (out / "llms.txt").read_text()):
        assert MONTH in text and NEXT in text
    assert "[values 2026-04](2026-04/values.csv)" in (out / "llms.txt").read_text()
    shutil.rmtree(out / MONTH)
    build_site(mini_scenario, out, NEXT)
    for text in ((out / "index.html").read_text(), (out / "llms.txt").read_text()):
        assert NEXT in text and MONTH not in text


def test_memo_exits_4_without_the_stored_sql(tmp_path, mini_scenario):
    mini_closed_copy = tmp_path / "w.duckdb"
    shutil.copy(mini_scenario, mini_closed_copy)
    with connect(str(mini_closed_copy)) as con:
        con.execute("DROP TABLE state.model_sql")
    result = margin("memo", "--month", MONTH, "--out", str(tmp_path / "site"), "--warehouse", str(mini_closed_copy))
    assert result.code == 4
    assert "compiled SQL not stored: run close" in result.stderr
    assert not (tmp_path / "site").exists()
    with connect(str(mini_closed_copy)) as con:
        con.execute("CREATE TABLE state.model_sql (model VARCHAR PRIMARY KEY, sql VARCHAR)")
    result = margin("memo", "--month", MONTH, "--out", str(tmp_path / "site"), "--warehouse", str(mini_closed_copy))
    assert result.code == 4
