"""Where dbt keeps its parsed project: beside the warehouse it builds, so the next close of the
same warehouse skips the full parse, unless the caller already named a directory."""

from __future__ import annotations

from margin import dbt_runner


def test_target_and_logs_sit_beside_the_warehouse(tmp_path):
    warehouse = tmp_path / "w" / "margin.duckdb"
    flags = dbt_runner.location_flags(warehouse, {})
    work = tmp_path.resolve() / "w" / "margin.duckdb.dbt"
    assert flags == ["--target-path", str(work / "target"), "--log-path", str(work / "logs")]


def test_each_warehouse_has_its_own_directory(tmp_path):
    assert dbt_runner.work_dir(tmp_path / "a.duckdb") != dbt_runner.work_dir(tmp_path / "b.duckdb")


def test_a_directory_named_in_the_environment_is_kept(tmp_path):
    warehouse = tmp_path / "margin.duckdb"
    flags = dbt_runner.location_flags(warehouse, {"DBT_TARGET_PATH": "/shared/target"})
    assert flags == ["--log-path", str(dbt_runner.work_dir(warehouse) / "logs")]
    assert dbt_runner.location_flags(
        warehouse, {"DBT_TARGET_PATH": "/t", "DBT_LOG_PATH": "/l"}
    ) == []
