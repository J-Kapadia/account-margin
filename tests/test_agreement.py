"""`margin agreement` on the closed `mini` fixture and a site built by `memo`: every ID of the
month's agreement set reads the same through the CLI, SQL, the memo and the cached values."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, connect, margin, shared_dir
from margin import agreement
from margin.cli import explain

MONTH = "2026-03"
HOLDER = (
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
    "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.readline()"
)


@pytest.fixture(scope="module")
def site(tmp_path_factory, mini_closed) -> Path:
    """The site after a memo for 2026-03; read-only for the tests."""
    def build(directory: Path) -> None:
        # `memo` refuses a month without scenario records: run the scenario on a private copy
        warehouse = directory.parent / f"{directory.name}-w.duckdb"
        shutil.copy(mini_closed, warehouse)
        result = margin("scenario", "--fixture", "mini", "--month", MONTH, "--config", str(MINI_CONFIG),
                        "--warehouse", str(warehouse))
        assert result.code == 0, result.stderr
        result = margin("memo", "--month", MONTH, "--out", str(directory), "--warehouse", str(warehouse))
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "agreement-site", build)


@pytest.fixture
def site_copy(site, tmp_path) -> Path:
    copy = tmp_path / "site"
    shutil.copytree(site, copy)
    return copy


def run(mini_closed: Path, site: Path, *extra: str):
    return margin("agreement", "--month", MONTH, "--check", "--json", "--site", str(site),
                  "--warehouse", str(mini_closed), *extra)


@pytest.fixture(scope="module")
def clean(tmp_path_factory, mini_closed, site):
    """One passing `--check --write` run on a private copy of the site."""
    copy = tmp_path_factory.mktemp("agreement-clean") / "site"
    shutil.copytree(site, copy)
    return copy, run(mini_closed, copy, "--write")


def account_count(mini_closed: Path) -> int:
    with connect(str(mini_closed), read_only=True) as con:
        return con.execute("SELECT count(*) FROM mart.fct_contribution_profit WHERE month = ?",
                           [f"{MONTH}-01"]).fetchone()[0]


def test_check_passes_with_a_zero_disagreement_rate(clean, mini_closed):
    result = clean[1]
    assert result.code == 0, result.stderr
    body = result.json
    assert body["disagreement_rate"] == "0"
    assert body["interfaces"] == ["cli", "sql", "memo", "values"]
    assert body["ids_checked"] == 7 * account_count(mini_closed) + 6
    assert body["sampled_subprocess"] == min(20, body["ids_checked"])


def test_write_stores_the_three_fields(clean, mini_closed):
    site, result = clean
    stored = json.loads((site / MONTH / "agreement.json").read_text())
    assert stored == {"ids_checked": result.json["ids_checked"], "interfaces": ["cli", "sql", "memo", "values"],
                      "disagreement_rate": "0"}


def test_without_write_nothing_is_stored(mini_closed, site_copy):
    assert run(mini_closed, site_copy).code == 0
    assert not (site_copy / MONTH / "agreement.json").exists()


def test_a_cent_changed_in_the_cached_values_names_the_id_and_both_interfaces(mini_closed, site_copy, tmp_path):
    lines = (site_copy / MONTH / "values.csv").read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.split(",")[0].endswith("/revenue") and line.split(",")[1] != "0")
    number_id, cents = lines[index].split(",")
    lines[index] = f"{number_id},{int(cents) + 1}"
    seeded = tmp_path / "values.csv"
    seeded.write_text("\n".join(lines) + "\n")
    result = run(mini_closed, site_copy, "--values", str(seeded))
    assert result.code == 3
    body = result.json
    assert body["failed_controls"][0]["id"] == "CHK-AGREEMENT"
    for word in (number_id, "values", "sql"):
        assert word in body["message"], body["message"]
    assert body["failed_controls"][0]["detail"] == [
        f"{number_id}: sql has {cents!r}, values has {str(int(cents) + 1)!r}"]


def test_a_changed_memo_value_names_the_memo(mini_closed, site_copy):
    page = site_copy / MONTH / "index.html"
    text = page.read_text()
    marker = f'data-num-id="rollup/{MONTH}/revenue" data-value="'
    start = text.index(marker) + len(marker)
    end = text.index('"', start)
    page.write_text(text[:start] + str(int(text[start:end]) + 1) + text[end:])
    result = run(mini_closed, site_copy)
    assert result.code == 3
    assert f"rollup/{MONTH}/revenue: sql has" in result.json["message"]
    assert "memo has" in result.json["message"]


def test_without_check_a_disagreement_is_reported_not_failed(mini_closed, site_copy):
    (site_copy / MONTH / "values.csv").write_text("number_id,value\n")
    result = margin("agreement", "--month", MONTH, "--json", "--site", str(site_copy),
                    "--warehouse", str(mini_closed))
    assert result.code == 0
    assert result.json["disagreement_rate"] != "0"


def test_llms_txt_without_the_values_link_fails(mini_closed, site_copy):
    llms = site_copy / "llms.txt"
    llms.write_text("".join(l for l in llms.read_text().splitlines(keepends=True) if "[values" not in l))
    result = run(mini_closed, site_copy)
    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CHK-AGREEMENT"
    assert f"values {MONTH}" in result.json["message"]


def test_a_malformed_llms_txt_fails(mini_closed, site_copy):
    (site_copy / "llms.txt").write_text("# fct_contribution_profit\n\nsee values.csv for the numbers\n")
    result = run(mini_closed, site_copy)
    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CHK-AGREEMENT"


@pytest.mark.parametrize("text", [
    "see values.csv\n",
    "- [values 2026-03] (2026-03/values.csv)\n",
    "- [values 2026-03](2026-03/values.csv) trailing\n",
    "- [a](x.csv)\n- [a](y.csv)\n",
    "  - [indented](x.csv)\n",
])
def test_the_parser_rejects_a_malformed_llms_txt(text):
    with pytest.raises(agreement.MarginError) as raised:
        agreement.parse_llms(text)
    assert raised.value.exit_code == 3
    assert raised.value.failed_controls[0]["id"] == "CHK-AGREEMENT"


def test_the_parser_reads_the_links_of_a_well_formed_file(site):
    links = agreement.parse_llms((site / "llms.txt").read_text())
    assert links == {"sql": "sql/fct_contribution_profit.sql", f"values {MONTH}": f"{MONTH}/values.csv"}


def test_a_site_without_the_memo_exits_4(mini_closed, site_copy):
    (site_copy / MONTH / "index.html").unlink()
    result = run(mini_closed, site_copy)
    assert result.code == 4
    assert result.json["message"] == f"memo not built for month {MONTH}: run margin memo"


def test_an_open_month_exits_4(mini_closed, site_copy):
    result = margin("agreement", "--month", "2026-12", "--check", "--json", "--site", str(site_copy),
                    "--warehouse", str(mini_closed))
    assert result.code == 4
    assert result.json["message"] == "month 2026-12 not closed"


def test_an_exclusive_lock_held_elsewhere_exits_70(mini_closed_copy, site_copy):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(mini_closed_copy) + ".lock"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        result = run(mini_closed_copy, site_copy)
        assert result.code == 70
        assert result.json["message"] == "another margin process holds the warehouse"
    finally:
        holder.kill()
        holder.wait()


def test_the_zero_revenue_account_reads_n_a_through_all_four_interfaces(mini_closed, site):
    with connect(str(mini_closed), read_only=True) as con:
        zero = con.execute("SELECT account_id FROM mart.fct_contribution_profit "
                           "WHERE month = ? AND revenue_cents = 0 ORDER BY account_id",
                           [f"{MONTH}-01"]).fetchall()
        sql = agreement.read_sql(con, MONTH)
    assert zero, "the mini fixture has no zero-revenue account"
    wanted = [f"cp/{MONTH}/{a}/margin_pct" for (a,) in zero]
    readings = {
        "sql": sql,
        "cli": agreement.read_cli(mini_closed, wanted, explain),
        "memo": agreement.read_memo(site / MONTH / "index.html"),
        "values": agreement.read_values(site / MONTH / "values.csv"),
    }
    for name, reading in readings.items():
        assert [reading[i] for i in wanted] == ["n/a"] * len(wanted), name
    assert agreement.compare(readings, wanted) == []


def test_the_subprocess_sample_is_seeded_and_capped():
    ids = [f"id{i}" for i in range(100)]
    assert agreement.sample_ids(ids) == agreement.sample_ids(ids)
    assert len(agreement.sample_ids(ids)) == 20
    assert agreement.sample_ids(ids[:5]) and len(agreement.sample_ids(ids[:5])) == 5


def test_compare_reports_a_missing_id_and_names_the_baseline():
    problems = agreement.compare({"sql": {"a": "1", "b": "2"}, "cli": {"a": "1"}, "memo": {"a": "1", "b": "2"},
                                  "values": {"a": "1", "b": "3"}}, ["a", "b"])
    assert [(p["id"], p["a"], p["b"]) for p in problems] == [("b", "sql", "cli")]
    assert agreement.rate(problems, 4) == "0.250000"
    assert agreement.rate([], 4) == "0"
