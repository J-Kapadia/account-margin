"""How `scenario` and the month state machine leave each other alone, on copies of `mini`.

A scenario run reads a month that is closed or locked and writes only `scenario.*` and the
`scenario/` records: it adds no `close_state` row and never changes a month's result hash. A close
leaves the scenario model out of its dbt run.
"""

from __future__ import annotations

import subprocess
import sys

from conftest import MINI_CONFIG, ROOT, close_mini, connect, margin
from margin import dbt_runner
from test_lock import hold_exclusive
from test_status import status

MONTH = "2026-03"


def scenario(warehouse, month=MONTH):
    return margin("scenario", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                  "--warehouse", str(warehouse))


# The first test closes the month itself, before and after the scenario: its assertion is that the two
# closes agree, so each is a real close of the copy.
def test_a_scenario_between_two_closes_leaves_the_month_and_its_hash_unchanged(mini_closed_copy):
    before = close_mini(mini_closed_copy, MONTH)
    assert before.code == 0, before.stderr
    listed = status(mini_closed_copy)
    assert listed.code == 0, listed.stderr

    ran = scenario(mini_closed_copy)
    assert ran.code == 0, ran.stderr
    assert status(mini_closed_copy).stdout == listed.stdout  # no month gained, lost or changed

    after = close_mini(mini_closed_copy, MONTH)
    assert after.code == 0, after.stderr
    assert after.json["result_hash"] == before.json["result_hash"]
    assert status(mini_closed_copy).stdout == listed.stdout


def test_a_scenario_on_a_locked_month_exits_0_and_changes_no_state(mini_rolled_forward_copy):
    warehouse = mini_rolled_forward_copy  # March closed, then rolled forward: February and March locked, April open
    listed = status(warehouse)
    assert {m["month"]: m["status"] for m in listed.json["months"]} == {
        "2026-02": "locked", "2026-03": "locked", "2026-04": "open"}

    result = scenario(warehouse)
    assert result.code == 0, result.stderr
    assert status(warehouse).stdout == listed.stdout


def test_a_scenario_on_an_open_month_exits_4_and_writes_nothing(mini_rolled_forward_copy):
    warehouse = mini_rolled_forward_copy  # April is loaded and open
    listed = status(warehouse)

    result = scenario(warehouse, "2026-04")
    assert result.code == 4
    assert "month 2026-04 not closed" in result.json["message"]
    assert status(warehouse).stdout == listed.stdout
    with connect(str(warehouse), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM prov.records WHERE number_id LIKE 'scenario/%'").fetchone()[0] == 0


def test_the_scenario_model_is_in_the_project_but_a_close_does_not_build_it(mini_closed, tmp_path):
    work = tmp_path / "w.duckdb"
    env = dbt_runner.env_for(work)
    listed = subprocess.run(
        [sys.executable, "-c", dbt_runner._DBT_MAIN, "ls", "--select", "tag:scenario", "--resource-type", "model",
         "--output", "name", "--project-dir", str(dbt_runner.DBT_DIR), "--profiles-dir", str(dbt_runner.DBT_DIR),
         "--vars", "{}", *dbt_runner.location_flags(work, env), "--no-use-colors"],
        env=env, capture_output=True, text=True, cwd=ROOT, check=False)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "scn_contribution" in listed.stdout.split()

    with connect(str(mini_closed), read_only=True) as con:
        table = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'scenario' AND table_name = 'results'"
        ).fetchone()[0]
        assert table == 0 or con.execute("SELECT count(*) FROM scenario.results").fetchone()[0] == 0


def test_a_scenario_exits_70_while_another_process_holds_the_warehouse(mini_closed_copy):
    holder = hold_exclusive(str(mini_closed_copy) + ".lock")
    try:
        result = scenario(mini_closed_copy)
        assert result.code == 70
        assert result.json["message"] == "another margin process holds the warehouse"
    finally:
        holder.kill()
        holder.wait()
