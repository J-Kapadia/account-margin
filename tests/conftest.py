"""Shared helpers: run `margin` as a subprocess against a temporary books/warehouse."""

from __future__ import annotations

import fcntl
import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest
from hypothesis import settings

# The example database would otherwise create an untracked .hypothesis/ in the checkout.
settings.register_profile("margin", database=None)
settings.load_profile("margin")

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SIGNOFF = ROOT / "tests" / "fixtures" / "signoff"
FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "config"


# ------------------------------------------------------------------ capped DuckDB connections

_real_connect = duckdb.connect


@functools.wraps(_real_connect)
def connect(*args, **kwargs):
    """`duckdb.connect` with the warehouse caps: the same `SET memory_limit` and `SET threads` as
    `margin.warehouse.connect`, applied after opening because DuckDB refuses a second connection
    to the same file with a different `config=`. Signature and return value are unchanged."""
    from margin import warehouse

    con = _real_connect(*args, **kwargs)
    limit = warehouse.memory_limit().replace("'", "''")
    con.execute(f"SET memory_limit = '{limit}'")
    con.execute(f"SET threads = {warehouse.threads()}")
    return con


@pytest.fixture(scope="session", autouse=True)
def _cap_duckdb_connections():
    """Every connection a test opens, directly or through library code, carries the caps; an
    uncapped one sizes itself to the whole machine."""
    duckdb.connect = connect
    try:
        yield
    finally:
        duckdb.connect = _real_connect


def shared_dir(tmp_path_factory, name: str, build) -> Path:
    """A directory `build(directory)` filled once for the whole run, however many xdist workers
    there are: the first caller builds it under a file lock, the others wait and reuse it. It is
    read-only for the tests; one that changes it takes a copy first."""
    base = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        base = base.parent  # the temp root every worker of this run shares
    directory, done = base / f"shared-{name}", base / f"shared-{name}.done"
    with open(base / f"shared-{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not done.exists():
            directory.mkdir()
            build(directory)
            done.touch()
    return directory


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict:
        return json.loads(self.stdout)


# Where a `margin` run that names no path of its own reads and writes: a directory private to the
# running test, never the checkout's `warehouse/` or `data/`, so parallel workers cannot collide on
# the default warehouse lock or on the default books. Session fixtures run before any test has set
# one, so they fall back to a directory private to the process.
_default_root: Path | None = None
_process_default_root: Path | None = None  # set only by the process that created it


def _remove_process_default_root() -> None:
    """Remove the process's own default root; it is private to this process, so no other worker
    can still be using it."""
    global _process_default_root
    root, _process_default_root = _process_default_root, None
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


def default_paths() -> dict[str, str]:
    global _process_default_root
    if _process_default_root is None:
        _process_default_root = Path(tempfile.mkdtemp(prefix="margin-defaults-"))
        atexit.register(_remove_process_default_root)  # fallback when the session hook never runs
    root = _default_root or _process_default_root
    # dbt's target and log directories are private to the process too: runs on one worker are
    # serial, so they reuse the parsed project, and runs on different workers never share a file.
    return {
        "MARGIN_WAREHOUSE": str(root / "warehouse" / "margin.duckdb"),
        "MARGIN_BOOKS": str(root / "data" / "books"),
        "DBT_TARGET_PATH": str(_process_default_root / "dbt-target"),
        "DBT_LOG_PATH": str(_process_default_root / "dbt-logs"),
    }


def pytest_sessionfinish(session):
    """Each process removes the default root it created, so a run leaves nothing under TMPDIR."""
    _remove_process_default_root()


def _default_xdist_workers() -> int:
    """`PYTEST_XDIST_AUTO_NUM_WORKERS` when it is a whole number, else the CPU count; never above 12."""
    wanted = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "")
    if wanted.isdigit():
        return min(12, int(wanted))
    return min(12, os.cpu_count() or 1)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """Run in parallel by default, whenever the xdist plugin is loaded (`-n 0` still runs serially,
    `-p no:xdist` and `--collect-only` are untouched). The default worker count is the one `-n auto`
    would use: `PYTEST_XDIST_AUTO_NUM_WORKERS` when set, else the CPU count, capped at 12; set it
    to 1 where several suites share one machine. A crashing worker is never respawned: a
    respawn loop outlives its controller and grows without bound, so the run fails on the first
    crash instead. Set here rather than in `addopts`, which would break `-p no:xdist`."""
    opts = config.option
    if hasattr(opts, "numprocesses") and opts.numprocesses is None and not opts.collectonly \
            and "PYTEST_XDIST_WORKER" not in os.environ:
        opts.numprocesses = _default_xdist_workers()
        opts.dist = "load"
        opts.maxworkerrestart = 0


def pytest_collection_modifyitems(items):
    """A test that needs the full-size books is slow: the fast tier runs on the committed `mini` and
    `small` books only."""
    for item in items:
        if "full" in item.fixturenames:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(autouse=True)
def _private_default_paths(tmp_path_factory):
    global _default_root
    _default_root = tmp_path_factory.mktemp("defaults")
    try:
        yield
    finally:
        _default_root = None


def margin(*args: str, cwd: Path = ROOT, env: dict | None = None) -> Result:
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("MARGIN_")}
    full_env.update(default_paths())
    # `close` is gated on a sign-off: unless a test says otherwise, the fixture config is the one signed.
    full_env["MARGIN_SIGNOFF_DIR"] = str(FIXTURE_SIGNOFF)
    full_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "margin", *args],
        cwd=cwd, env=full_env, capture_output=True, text=True, check=False,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class Workspace:
    root: Path

    @property
    def books(self) -> Path:
        return self.root / "books"

    @property
    def warehouse(self) -> Path:
        return self.root / "w.duckdb"

    def flags(self) -> list[str]:
        return ["--books", str(self.books), "--warehouse", str(self.warehouse), "--config", str(FIXTURE_CONFIG)]

    def warehouse_flags(self) -> list[str]:
        """The only path flag explain and memo take."""
        return ["--warehouse", str(self.warehouse)]

    def gen(self, *extra: str) -> Result:
        return margin("gen", "--fixture", "small", "--out", str(self.books), "--json", *extra)

    def close(self, month: str = "2026-03", *extra: str) -> Result:
        return margin("close", "--month", month, "--json", *self.flags(), *extra)


@pytest.fixture(scope="session")
def closed(tmp_path_factory) -> Workspace:
    """`small` books generated and 2026-03 closed once, shared by the read-only tests."""
    def build(directory: Path) -> None:
        ws = Workspace(directory)
        assert ws.gen().code == 0
        result = ws.close()
        assert result.code == 0, result.stderr

    return Workspace(shared_dir(tmp_path_factory, "closed", build))


@pytest.fixture
def closed_copy(closed, tmp_path) -> Workspace:
    """A private copy of the closed workspace for tests that mutate it."""
    ws = Workspace(tmp_path)
    shutil.copytree(closed.books, ws.books)
    shutil.copy(closed.books.parent / "books.manifest.sha256", tmp_path / "books.manifest.sha256")
    shutil.copy(closed.warehouse, ws.warehouse)
    return ws


# ------------------------------------------------------------------ the committed `mini` fixture

FIXTURES = ROOT / "tests" / "fixtures"
MINI_BOOKS = FIXTURES / "books"
MINI_TRUTH = FIXTURES / "truth"
MINI_CONFIG = FIXTURES / "config"
MINI_PRICES = FIXTURES / "model_prices.csv"
MINI_WINDOW = ("2026-02", "2026-04")


def build_mini_warehouse(books: Path, warehouse: Path, config_dir: Path = MINI_CONFIG,
                         window: tuple[str, str] = MINI_WINDOW) -> Path:
    """Load `books` and run the dbt project over them: models and tests, no controls, no
    sign-off. The close pipeline proper (gate, provenance, state) is a separate step."""
    from margin import dbt_runner, load
    from margin.warehouse import connect, init_schemas

    con = connect(warehouse)
    try:
        init_schemas(con)
        load.load_months(con, books, window[0], window[1], "test")
        load.load_reference(con, MINI_PRICES)
    finally:
        con.close()
    dbt_runner.build(warehouse, config_dir)
    return warehouse


@pytest.fixture(scope="session")
def mini_warehouse(tmp_path_factory) -> Path:
    """The `mini` books built once with the fixture config; read-only for the tests."""
    directory = shared_dir(tmp_path_factory, "mini", lambda d: build_mini_warehouse(MINI_BOOKS, d / "mini.duckdb"))
    return directory / "mini.duckdb"


@pytest.fixture
def mini_books_copy(tmp_path) -> Path:
    """A private copy of the `mini` books for tests that tamper with them."""
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    return books


# ------------------------------------------------------------------ closing `mini` through the CLI


def close_mini(warehouse: Path, month: str = "2026-04", *extra: str, env: dict | None = None,
               config: Path = MINI_CONFIG) -> Result:
    """`margin close` on the committed `mini` books, signed config and sign-off directory by default."""
    return margin("close", "--fixture", "mini", "--month", month, "--json", "--config", str(config),
                  "--warehouse", str(warehouse), *extra, env=env)


@pytest.fixture(scope="session")
def mini_closed(tmp_path_factory) -> Path:
    """A warehouse with every month of `mini` closed by the CLI; read-only for the tests."""
    def build(directory: Path) -> None:
        result = close_mini(directory / "w.duckdb")
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "mini_closed", build) / "w.duckdb"


@pytest.fixture
def mini_closed_copy(mini_closed, tmp_path) -> Path:
    """A private copy of the closed `mini` warehouse for tests that change it."""
    copy = tmp_path / "w.duckdb"
    shutil.copy(mini_closed, copy)
    return copy


@pytest.fixture(scope="session")
def mini_closed_march(tmp_path_factory) -> Path:
    """A warehouse with `mini` closed through 2026-03 by the CLI; read-only for the tests."""
    def build(directory: Path) -> None:
        result = close_mini(directory / "w.duckdb", "2026-03")
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "mini_closed_march", build) / "w.duckdb"


@pytest.fixture
def mini_closed_march_copy(mini_closed_march, tmp_path) -> Path:
    """A private copy of the warehouse closed through 2026-03, for tests that change it."""
    copy = tmp_path / "w.duckdb"
    shutil.copy(mini_closed_march, copy)
    return copy


@pytest.fixture(scope="session")
def mini_rolled_forward(tmp_path_factory, mini_closed_march) -> Path:
    """`mini` closed through 2026-03, then rolled forward to 2026-04 (February and March locked, April
    open); read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed_march, directory / "w.duckdb")
        result = margin("rollforward", "--fixture", "mini", "--month", "2026-04", "--json",
                        "--config", str(MINI_CONFIG), "--warehouse", str(directory / "w.duckdb"))
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "mini_rolled_forward", build) / "w.duckdb"


@pytest.fixture
def mini_rolled_forward_copy(mini_rolled_forward, tmp_path) -> Path:
    """A private copy of the rolled-forward warehouse, for tests that change it."""
    copy = tmp_path / "w.duckdb"
    shutil.copy(mini_rolled_forward, copy)
    return copy


# ------------------------------------------------------------------ the scenario and the memo site over `mini`


@pytest.fixture(scope="session")
def scenario_warehouse(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for 2026-03 and 2026-04; read-only
    for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        for month in ("2026-03", "2026-04"):
            result = margin("scenario", "--fixture", "mini", "--month", month, "--json", "--config", str(MINI_CONFIG),
                            "--warehouse", str(directory / "w.duckdb"))
            assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "memo_check_warehouse", build) / "w.duckdb"


@pytest.fixture(scope="session")
def built_site(tmp_path_factory, scenario_warehouse) -> Path:
    """The memo site for 2026-03 and 2026-04, two of the fixture's three months; read-only for the
    tests, which copy it when they change it."""
    def build(directory: Path) -> None:
        for month in ("2026-03", "2026-04"):
            result = margin("memo", "--month", month, "--out", str(directory), "--warehouse", str(scenario_warehouse))
            assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "memo_check_site", build)
