"""Run dbt as a subprocess (plan §6.3): `margin` holds no DuckDB connection meanwhile."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .errors import EXIT_INTERNAL, MarginError
from .paths import ROOT

DBT_DIR = ROOT / "dbt"
_DBT_MAIN = "import sys; from dbt.cli.main import cli; sys.exit(cli())"


def build(warehouse: Path) -> None:
    """Full-refresh every model and run its schema tests against `warehouse`."""
    env = dict(
        os.environ,
        MARGIN_WAREHOUSE_PATH=str(warehouse.resolve()),
        DBT_SEND_ANONYMOUS_USAGE_STATS="false",
    )
    cmd = [
        sys.executable, "-c", _DBT_MAIN, "build",
        "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
        "--exclude", "tag:scenario",  # scenario models never run in close (plan §7.1)
        "--no-use-colors", "--quiet",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise MarginError(EXIT_INTERNAL, f"dbt build failed:\n{tail}")
