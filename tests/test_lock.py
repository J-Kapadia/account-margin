import fcntl
import os
import signal
import subprocess
import sys
import time

from conftest import margin

HOLDER = (
    "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
    "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.readline()"
)


def hold_exclusive(lock_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_lock_path_follows_the_warehouse(tmp_path):
    from margin.paths import resolve

    assert resolve(warehouse=str(tmp_path / "w.duckdb")).lock == tmp_path / "w.duckdb.lock"
    assert str(resolve(fixture="small").lock) == "warehouse/small.duckdb.lock"
    assert str(resolve().lock) == "warehouse/margin.duckdb.lock"


def test_writer_exits_70_while_another_process_holds_the_warehouse(closed, tmp_path):
    holder = hold_exclusive(str(closed.warehouse) + ".lock")
    try:
        result = margin("close", "--month", "2026-03", "--json", *closed.flags())
        assert result.code == 70
        assert result.json["message"] == "another margin process holds the warehouse"
        reader = margin("explain", "cp/2026-03/acct_0001/revenue", "--json", *closed.flags())
        assert reader.code == 70  # a reader cannot run beside a writer
    finally:
        holder.kill()
        holder.wait()


def test_killed_holder_needs_no_cleanup(closed):
    holder = hold_exclusive(str(closed.warehouse) + ".lock")
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    result = margin("explain", "cp/2026-03/acct_0001/revenue", "--json", *closed.flags())
    assert result.code == 0, result.stderr


def test_readers_share_the_lock(closed):
    fd = os.open(str(closed.warehouse) + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        result = margin("explain", "rollup/2026-03/revenue", "--json", *closed.flags())
        assert result.code == 0, result.stderr
        writer = margin("close", "--month", "2026-03", "--json", *closed.flags())
        assert writer.code == 70
    finally:
        os.close(fd)
