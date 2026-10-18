"""scripts/profile-close.sh closes `mini` twice and reports where each close spends its time."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from conftest import ROOT

SCRIPT = ROOT / "scripts" / "profile-close.sh"
PHASES = ["dbt subprocess", "dbt import", "dbt parse", "dbt run", "dbt other", "outside dbt"]
# The phases that add up to the wall time; `dbt subprocess` is their dbt part, shown beside them.
PARTS = ["dbt import", "dbt parse", "dbt run", "dbt other", "outside dbt"]

pytestmark = pytest.mark.slow


def profile(*flags: str) -> str:
    proc = subprocess.run(["bash", str(SCRIPT), *flags], cwd=ROOT, capture_output=True, text=True,
                          timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture(scope="module")
def table() -> dict[str, list[float]]:
    out = profile()
    header = out.splitlines()[0]
    assert "first close (2026-04)" in header and "second close (2026-03)" in header
    rows = {}
    for line in out.splitlines()[1:]:
        found = re.fullmatch(r"(\D+?)\s{2,}(-?\d+\.\d)\s+(-?\d+\.\d)", line)
        if found:
            rows[found[1]] = [float(found[2]), float(found[3])]
    return rows


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(profile("--json"))


def test_table_has_a_row_per_phase_and_both_closes(table):
    assert list(table) == ["wall", *PHASES]
    assert all(len(values) == 2 and all(v >= 0 for v in values) for values in table.values())


def test_phases_sum_to_the_wall_time_of_each_close(table):
    for close in (0, 1):
        total = sum(table[phase][close] for phase in PARTS)
        assert abs(total - table["wall"][close]) <= 1.0, (close, total, table["wall"][close])


def test_json_is_the_same_report(report):
    closes = report["closes"]
    assert [(c["close"], c["month"]) for c in closes] == [("first close", "2026-04"), ("second close", "2026-03")]
    keys = ["wall", "dbt_subprocess", "dbt_import", "dbt_parse", "dbt_run", "dbt_other", "outside_dbt"]
    for close in closes:
        assert all(close[k] >= 0 for k in keys)
        parts = [close[k] for k in keys if k not in ("wall", "dbt_subprocess")]
        assert abs(sum(parts) - close["wall"]) <= 1.0
