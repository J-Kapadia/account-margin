"""scripts/collect-build-cost.py sums a synthetic cost ledger into the six build-cost totals.

Every case runs the script as a subprocess on a ledger and a review file written under tmp_path.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPT = ROOT / "scripts" / "collect-build-cost.py"
LEDGER_HEADER = "work_id,model,input_tokens,output_tokens,usd,started,closed,source\n"
MEASURES = ["build_cost_usd", "input_tokens", "output_tokens", "elapsed_hours", "review_minutes",
            "run_cost_monthly_usd"]
# Three items: w1 spans midnight (1.5 h), w2 is 7.5 minutes (0.125 h), w3 is one hour.
# Dollars sum to 4.005 and hours to 2.625, both exact ties: half-even gives 4.00 and 2.62.
LEDGER = (
    LEDGER_HEADER
    + "w1,model-a,1000,200,1.005,2026-10-01T23:30:00Z,2026-10-02T01:00:00Z,events\n"
    + "w2,model-b,2500,500,2.50,2026-10-03T09:00:00Z,2026-10-03T09:07:30Z,git\n"
    + "w3,model-a,300,100,0.50,2026-10-04T12:00:00+00:00,2026-10-04T13:00:00+00:00,events\n"
)
REVIEW = "work_id,minutes\nw1,30\nw2,45\nw3,12.5\n"


def collect(tmp_path: Path, ledger: str = LEDGER, review: str = REVIEW, *flags: str,
            out: Path | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    (tmp_path / "ledger.csv").write_text(ledger, encoding="utf-8")
    (tmp_path / "review.csv").write_text(review, encoding="utf-8")
    out = out or tmp_path / "build_cost.csv"
    args = ["--ledger", str(tmp_path / "ledger.csv"), "--review", str(tmp_path / "review.csv"),
            "--out", str(out), *flags]
    if "--collected-on" not in flags:
        args += ["--collected-on", "2026-10-18"]
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path, capture_output=True,
                          text=True, check=False)
    return proc, out


def table(out: Path) -> dict[str, dict[str, str]]:
    with out.open(newline="", encoding="utf-8") as handle:
        return {row["measure"]: row for row in csv.DictReader(handle)}


def test_totals_sources_and_date(tmp_path):
    proc, out = collect(tmp_path)
    assert proc.returncode == 0, proc.stderr
    rows = table(out)
    assert {m: r["value"] for m, r in rows.items()} == {
        "build_cost_usd": "4.00",
        "input_tokens": "3800",
        "output_tokens": "800",
        "elapsed_hours": "2.62",
        "review_minutes": "87.50",
        "run_cost_monthly_usd": "0.00",
    }
    # One git row among the ledger's rows makes the ledger's measures git; typed-in figures are manual.
    assert {m: r["source"] for m, r in rows.items()} == {
        "build_cost_usd": "git", "input_tokens": "git", "output_tokens": "git",
        "elapsed_hours": "git", "review_minutes": "manual", "run_cost_monthly_usd": "manual",
    }
    assert {r["collected_on"] for r in rows.values()} == {"2026-10-18"}


def test_columns_order_and_line_endings(tmp_path):
    proc, out = collect(tmp_path)
    assert proc.returncode == 0, proc.stderr
    raw = out.read_bytes()
    assert b"\r" not in raw
    lines = raw.decode().split("\n")
    assert lines[0] == "measure,value,source,collected_on"
    assert [line.split(",")[0] for line in lines[1:7]] == MEASURES
    assert lines[7] == "" and len(lines) == 8


def test_prints_the_totals(tmp_path):
    proc, _ = collect(tmp_path)
    assert proc.returncode == 0, proc.stderr
    printed = {line.split()[0]: line.split()[1:] for line in proc.stdout.splitlines()}
    assert list(printed) == MEASURES
    assert printed["build_cost_usd"] == ["4.00", "git"]
    assert printed["review_minutes"] == ["87.50", "manual"]


def test_run_twice_gives_identical_bytes(tmp_path):
    _, first = collect(tmp_path, out=tmp_path / "one.csv")
    _, second = collect(tmp_path, out=tmp_path / "two.csv")
    assert first.read_bytes() == second.read_bytes()
    collect(tmp_path, out=tmp_path / "one.csv")
    assert first.read_bytes() == second.read_bytes()


def test_run_cost_defaults_to_zero_and_takes_a_value(tmp_path):
    _, out = collect(tmp_path, LEDGER, REVIEW, "--run-cost-monthly-usd", "12.5")
    assert table(out)["run_cost_monthly_usd"]["value"] == "12.50"
    assert table(out)["run_cost_monthly_usd"]["source"] == "manual"


@pytest.mark.parametrize(("sources", "label"), [
    (["events", "events", "events"], "events"),
    (["events", "git", "events"], "git"),
    (["events", "git", "manual"], "manual"),
    (["manual", "manual", "manual"], "manual"),
])
def test_ledger_source_is_the_least_certain_row(tmp_path, sources, label):
    rows = LEDGER.splitlines()
    ledger = "\n".join([rows[0]] + [r.rsplit(",", 1)[0] + "," + s for r, s in zip(rows[1:], sources)]) + "\n"
    proc, out = collect(tmp_path, ledger)
    assert proc.returncode == 0, proc.stderr
    got = table(out)
    assert {got[m]["source"] for m in MEASURES[:4]} == {label}


def test_an_item_with_several_ledger_rows_counts_its_elapsed_time_once(tmp_path):
    ledger = (LEDGER_HEADER
              + "w1,model-a,10,1,1.00,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,events\n"
              + "w1,model-b,20,2,2.00,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,events\n")
    proc, out = collect(tmp_path, ledger)
    assert proc.returncode == 0, proc.stderr
    got = table(out)
    assert got["elapsed_hours"]["value"] == "2.00"
    assert got["input_tokens"]["value"] == "30"
    assert got["build_cost_usd"]["value"] == "3.00"


@pytest.mark.parametrize(("row", "needle"), [
    ("w9,model-a,1,1,1.00,,2026-10-01T12:00:00Z,events", "started is missing"),
    ("w9,model-a,1,1,1.00,2026-10-01T10:00:00Z,,events", "closed is missing"),
    ("w9,model-a,1,1,1.00,yesterday,2026-10-01T12:00:00Z,events", "not an ISO 8601 timestamp"),
    ("w9,model-a,1,1,1.00,2026-10-01T10:00:00,2026-10-01T12:00:00Z,events", "no UTC offset"),
    ("w9,model-a,1,1,1.00,2026-10-01T12:00:00Z,2026-10-01T10:00:00Z,events", "before started"),
    ("w9,model-a,1,1,1.00,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,guess", "unknown source 'guess'"),
    ("w9,model-a,1,1,1.00,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,", "source is missing"),
    ("w9,model-a,many,1,1.00,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,events", "not a whole number"),
    ("w9,model-a,1,1,lots,2026-10-01T10:00:00Z,2026-10-01T12:00:00Z,events", "not a number"),
])
def test_a_bad_ledger_row_exits_2_naming_it(tmp_path, row, needle):
    proc, out = collect(tmp_path, LEDGER + row + "\n")
    assert proc.returncode == 2
    assert "line 5" in proc.stderr and "w9" in proc.stderr and needle in proc.stderr
    assert not out.exists()


def test_a_bad_review_row_exits_2_naming_it(tmp_path):
    proc, out = collect(tmp_path, LEDGER, "work_id,minutes\nw1,30\nw2,soon\n")
    assert proc.returncode == 2
    assert "line 3" in proc.stderr and "w2" in proc.stderr and "minutes" in proc.stderr
    assert not out.exists()


def test_a_missing_input_or_column_exits_2(tmp_path):
    proc, _ = collect(tmp_path, "work_id,model\nw1,a\n")
    assert proc.returncode == 2 and "header lacks column" in proc.stderr
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(tmp_path / "nope.csv"), "--review",
         str(tmp_path / "review.csv"), "--collected-on", "2026-10-18", "--out", str(tmp_path / "o.csv")],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and "cannot read" in proc.stderr


@pytest.mark.parametrize("flags", [
    ["--collected-on", "18/10/2026"],
    ["--collected-on", "2026-13-01"],
    ["--run-cost-monthly-usd", "free"],
])
def test_a_bad_argument_exits_2(tmp_path, flags):
    proc, out = collect(tmp_path, LEDGER, REVIEW, *flags)
    assert proc.returncode == 2
    assert not out.exists()


def test_collected_on_is_required(tmp_path):
    (tmp_path / "ledger.csv").write_text(LEDGER, encoding="utf-8")
    (tmp_path / "review.csv").write_text(REVIEW, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--ledger", str(tmp_path / "ledger.csv"), "--review",
         str(tmp_path / "review.csv"), "--out", str(tmp_path / "o.csv")],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and "--collected-on" in proc.stderr
