"""A pytest run removes the per-process default root it created under TMPDIR."""

from __future__ import annotations

import os
import subprocess

from conftest import ROOT


def test_run_leaves_no_default_root(tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"}
    env["TMPDIR"] = str(tmpdir)
    proc = subprocess.run(
        ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_envelope.py", "-k", "gen"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert list(tmpdir.glob("margin-defaults-*")) == []
