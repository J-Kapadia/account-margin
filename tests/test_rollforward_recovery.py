"""A `rollforward` killed inside its transaction needs no cleanup, on the committed `mini` books.

The process is held between loading April and committing, killed there with `SIGKILL`, and run
again. Locking March, loading April and recording it are one transaction, so the partial load
rolls back, and the kernel drops the advisory lock with the process, so the re-run takes it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

from conftest import FIXTURE_SIGNOFF, MINI_CONFIG, ROOT, close_mini, default_paths, margin

RAW_TABLES = ("accounts", "invoices", "usage_monthly", "provider_invoices", "cloud_focus",
              "support_tickets", "gl")
PAUSE_ENV = "MARGIN_PAUSE_BEFORE_COMMIT"
WAIT_SECONDS = 60


def _args(warehouse: Path, month: str) -> list[str]:
    return ["rollforward", "--fixture", "mini", "--month", month, "--json",
            "--config", str(MINI_CONFIG), "--warehouse", str(warehouse)]


def rollforward(warehouse: Path, month: str):
    return margin(*_args(warehouse, month))


def _state(warehouse: Path) -> list[tuple]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return con.execute(
            "SELECT strftime(month, '%Y-%m'), status, input_digest, result_hash "
            "FROM state.close_state ORDER BY month"
        ).fetchall()
    finally:
        con.close()


def _raw_counts(warehouse: Path) -> dict[str, list[tuple]]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return {
            t: con.execute(
                f"SELECT strftime(_month, '%Y-%m'), count(*) FROM raw.{t} GROUP BY 1 ORDER BY 1 NULLS FIRST"
            ).fetchall()
            for t in RAW_TABLES
        }
    finally:
        con.close()


def _killed_inside_the_transaction(warehouse: Path, marker: Path) -> None:
    """Start `rollforward 2026-04` held before its commit, kill it there, and wait for it to exit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MARGIN_")}
    env.update(default_paths())
    env["MARGIN_SIGNOFF_DIR"] = str(FIXTURE_SIGNOFF)
    env[PAUSE_ENV] = str(marker)
    proc = subprocess.Popen(
        [sys.executable, "-m", "margin", *_args(warehouse, "2026-04")], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while not marker.exists():
            if proc.poll() is not None:
                pytest.fail(f"rollforward exited {proc.returncode} before the pause: {proc.stderr.read()}")
            if time.monotonic() > deadline:
                pytest.fail("rollforward never reached the pause before commit")
            time.sleep(0.05)
        proc.send_signal(signal.SIGKILL)
        assert proc.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


@pytest.fixture
def reference(tmp_path):
    """An uninterrupted run: March closed, then rolled forward to April."""
    warehouse = tmp_path / "reference.duckdb"
    assert close_mini(warehouse, "2026-03").code == 0
    assert rollforward(warehouse, "2026-04").code == 0
    return _state(warehouse), _raw_counts(warehouse)


def test_a_rollforward_killed_before_its_commit_recovers_on_a_rerun(tmp_path, reference):
    ref_state, ref_counts = reference
    warehouse, marker = tmp_path / "w.duckdb", tmp_path / "paused"
    assert close_mini(warehouse, "2026-03").code == 0
    before = _state(warehouse)

    _killed_inside_the_transaction(warehouse, marker)

    # The transaction rolled back: no April rows, March still closed and unchanged.
    assert all(month != "2026-04" for rows in _raw_counts(warehouse).values() for month, _ in rows)
    assert _state(warehouse) == before
    assert [s[:2] for s in before if s[0] == "2026-03"] == [("2026-03", "closed")]

    # The lock file outlives the process, and means nothing: the kernel released the lock.
    assert Path(str(warehouse) + ".lock").exists()
    rerun = rollforward(warehouse, "2026-04")
    assert rerun.code == 0, rerun.stderr
    assert rerun.json["locked"] == ["2026-02", "2026-03"] and rerun.json["loaded"] is True

    # Same state hashes and the same raw rows as an uninterrupted run: nothing duplicated.
    assert _state(warehouse) == ref_state
    assert _raw_counts(warehouse) == ref_counts
    march = {s[0]: s for s in _state(warehouse)}["2026-03"]
    assert march[1] == "locked" and march[2] == {s[0]: s for s in ref_state}["2026-03"][2]

    checked = close_mini(warehouse, "2026-04", "--check")
    assert checked.code == 0, checked.stderr
