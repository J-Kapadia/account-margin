"""Run dbt as a subprocess (plan §6.3): `margin` holds no DuckDB connection meanwhile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import EXIT_INTERNAL, MarginError
from .gen_config import load_config
from .paths import ROOT

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


def build(warehouse: Path, config_dir: Path) -> None:
    """Full-refresh every model and run its tests against `warehouse`, with the configuration."""
    env = dict(
        os.environ,
        MARGIN_WAREHOUSE_PATH=str(warehouse.resolve()),
        DBT_SEND_ANONYMOUS_USAGE_STATS="false",
    )
    cmd = [
        sys.executable, "-c", _DBT_MAIN, "build",
        "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
        "--exclude", "tag:scenario",  # scenario models never run in close (plan §7.1)
        "--vars", json.dumps(dbt_vars(config_dir)),
        "--no-use-colors", "--quiet",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise MarginError(EXIT_INTERNAL, f"dbt build failed:\n{tail}")
