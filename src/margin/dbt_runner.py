"""Run dbt as a subprocess: `margin` holds no DuckDB connection meanwhile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import EXIT_INTERNAL, MarginError
from .gen_config import load_config
from .paths import ROOT
from .warehouse import MEMORY_LIMIT_VAR, THREADS_VAR, memory_limit, threads

DBT_DIR = ROOT / "dbt"
_DBT_MAIN = "import sys; from dbt.cli.main import cli; sys.exit(cli())"


def dbt_vars(config_dir: Path) -> dict:
    """What the models read from the configuration: the allowed rules, the rule in force, the
    floor and the reason-code drivers in tie order. Decimals travel as strings."""
    config = load_config(config_dir)
    return {
        "rules": list(config.allocation.rules),
        "signed_rule": config.allocation.recommended,
        "floor_pct": str(config.policy.floor_pct),
        "drivers": [
            {"code": d.code, "median_floor": str(d.median_floor)} for d in config.policy.drivers
        ],
    }


def env_for(warehouse: Path) -> dict[str, str]:
    """The dbt subprocess's environment: the warehouse path and the DuckDB memory and thread
    caps, which the profile reads so dbt's connection is capped like the product's own."""
    return dict(
        os.environ,
        MARGIN_WAREHOUSE_PATH=str(warehouse.resolve()),
        DBT_SEND_ANONYMOUS_USAGE_STATS="false",
        **{MEMORY_LIMIT_VAR: memory_limit(), THREADS_VAR: str(threads())},
    )


def work_dir(warehouse: Path) -> Path:
    """Where dbt keeps its target and log directories for `warehouse`: beside the file, so a
    second close of the same warehouse finds the parsed project and skips the full parse."""
    return warehouse.resolve().with_name(warehouse.name + ".dbt")


def location_flags(warehouse: Path, env: dict[str, str]) -> list[str]:
    """`--target-path` and `--log-path` under the warehouse's work directory, each unless the
    environment already names one (`DBT_TARGET_PATH`, `DBT_LOG_PATH`): a caller that shares one
    parsed project across several warehouses sets those and keeps that."""
    work = work_dir(warehouse)
    flags = []
    if "DBT_TARGET_PATH" not in env:
        flags += ["--target-path", str(work / "target")]
    if "DBT_LOG_PATH" not in env:
        flags += ["--log-path", str(work / "logs")]
    return flags


def _run(warehouse: Path, config_dir: Path, *selection: str, extra_vars: dict | None = None) -> None:
    """One `dbt build` of `selection` against `warehouse`, with the configuration."""
    env = env_for(warehouse)
    cmd = [
        sys.executable, "-c", _DBT_MAIN, "build",
        "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
        "--vars", json.dumps({**dbt_vars(config_dir), **(extra_vars or {})}),
        *selection,
        *location_flags(warehouse, env),
        # Nothing reads dbt's JSON artifacts or its debug log: skip writing them. The partial-parse
        # file is separate and still written.
        "--no-write-json", "--log-level-file", "warn",
        "--no-use-colors", "--quiet",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise MarginError(EXIT_INTERNAL, f"dbt build failed:\n{tail}")


def build(warehouse: Path, config_dir: Path) -> None:
    """Full-refresh every model and run its tests against `warehouse`, with the configuration,
    except the scenario's: it needs `scenario.inputs`, and a close must not erase its results."""
    _run(warehouse, config_dir, "--exclude", "tag:scenario")


def build_scenario(warehouse: Path, config_dir: Path) -> None:
    """Build only the scenario model from the rows in `scenario.inputs`, then run its tests."""
    _run(warehouse, config_dir, "--select", "tag:scenario", extra_vars={"scenario_built": True})
