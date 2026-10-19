"""`margin doctor`: a read-only health report.

Each check is `{id, status, detail}` with status `ok`, `warn` or `fail`. The exit code is 2 when
any check fails, 1 when only warnings remain and 0 otherwise. Nothing here writes a file, takes a
lock that outlives the command, or reads the contents of `truth/`.
"""

from __future__ import annotations

import fcntl
import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from . import books as books_mod
from . import signoff as signoff_mod
from .errors import MarginError
from .paths import Paths
from .warehouse import connect

SUPPORTED_PYTHON = (3, 12)
# import name -> distribution name, for the dependencies the lockfile pins
DEPENDENCIES = {"dbt": "dbt-core", "duckdb": "duckdb", "jinja2": "jinja2"}
MAX_LISTED = 3  # files named in a detail line


def _check(id_: str, status: str, detail: str) -> dict:
    return {"id": id_, "status": status, "detail": detail}


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_LISTED])
    return shown + (f" and {len(names) - MAX_LISTED} more" if len(names) > MAX_LISTED else "")


def check_python() -> dict:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] != SUPPORTED_PYTHON:
        return _check("python", "fail", f"Python {version}: this project needs "
                      f"{SUPPORTED_PYTHON[0]}.{SUPPORTED_PYTHON[1]}")
    return _check("python", "ok", f"Python {version}")


def check_dependencies() -> dict:
    found, missing = [], []
    for module, dist in DEPENDENCIES.items():
        try:
            importlib.import_module(module)
            found.append(f"{dist} {importlib.metadata.version(dist)}")
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            missing.append(f"{dist} ({type(exc).__name__})")
    if missing:
        return _check("dependencies", "fail", "cannot import " + ", ".join(missing) + ": run uv sync --frozen")
    return _check("dependencies", "ok", ", ".join(found))


def check_books(books: Path) -> dict:
    """The manifest lists exactly the files of the books directory, with their hashes, and
    `window.json` is one of them and holds a valid range."""
    if not books.is_dir():
        return _check("books", "fail", f"no books directory at {books}: run margin gen")
    try:
        _, lines = books_mod.read_manifest(books)
    except MarginError as err:
        return _check("books", "fail", err.message)
    listed = {}
    for line in lines:
        digest, _, name = line.partition("  ")
        listed[name] = digest
    changed = [n for n, digest in listed.items()
               if not (books / n).is_file() or books_mod.file_sha256(books / n) != digest]
    extra = [p.name for p in books_mod.books_files(books) if p.name not in listed]
    problems = []
    if changed:
        problems.append("differ from the manifest: " + _listed(sorted(changed)))
    if extra:
        problems.append("not in the manifest: " + _listed(extra))
    if "window.json" not in listed:
        problems.append("window.json is not in the manifest")
    window = None
    try:
        window = books_mod.read_window(books)
    except (MarginError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"window.json unreadable ({exc})")
    if window is not None:
        try:
            months = books_mod.months_between(*window)
        except (ValueError, TypeError, AttributeError):
            months = []
        if not months:
            problems.append(f"window.json holds no valid range ({window[0]}..{window[1]})")
    if problems:
        return _check("books", "fail", f"books at {books}: " + "; ".join(problems) + ": run margin gen --verify")
    return _check("books", "ok", f"{len(listed)} files match the manifest, window {window[0]}..{window[1]}")


def check_signoff(config_dir: Path) -> dict:
    """The gate `close` applies, reported and not enforced."""
    try:
        sign = signoff_mod.verify(config_dir)
    except MarginError as err:
        if err.controls is None:  # a config file is missing: the error already says which
            return _check("signoff", "fail", err.message)
        return _check("signoff", "fail", f"no valid sign-off for the configs in {config_dir}; "
                      f"searched {signoff_mod.signoff_dir(config_dir)} (set {signoff_mod.SIGNOFF_DIR_ENV} "
                      "to look elsewhere)")
    return _check("signoff", "ok", f"signed by {sign.role} on {sign.signed_on}")


def _holder(lock_path: Path) -> str:
    """The PID the last exclusive holder wrote, for the message only."""
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if text.isdigit() else ""


def lock_held(lock_path: Path) -> bool | None:
    """Whether a process holds the exclusive lock now; None when there is no lock file. A shared
    lock is attempted without blocking and released at once, and creates nothing."""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def check_lock(lock_path: Path, held: bool | None) -> dict:
    if held is None:
        return _check("lock", "ok", f"free: no lock file at {lock_path}")
    pid = _holder(lock_path)
    if held:
        who = f" by pid {pid}" if pid else ""
        return _check("lock", "warn", f"held{who}: a writer is running; commands that write will exit 70")
    # The kernel releases the lock when its holder dies, so a file naming a dead PID is free.
    return _check("lock", "ok", "free: no process holds it" + (f" (last holder pid {pid})" if pid else ""))


def check_warehouse(warehouse: Path, held: bool | None) -> dict:
    if not warehouse.is_file():
        return _check("warehouse", "warn", f"no warehouse at {warehouse}: run margin close")
    if held:
        return _check("warehouse", "warn", f"{warehouse} not opened: another process holds the lock")
    try:
        con = connect(warehouse, read_only=True)
        try:
            con.execute("SELECT count(*) FROM information_schema.tables").fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - any failure to open is the finding
        return _check("warehouse", "fail", f"{warehouse} does not open read-only: {type(exc).__name__}: {exc}")
    return _check("warehouse", "ok", f"{warehouse} opens read-only")


def check_truth(truth: Path) -> dict:
    if truth.is_dir():
        return _check("truth", "ok", f"present at {truth}")
    return _check("truth", "warn", f"no truth directory at {truth}: only close --check and the tests need it")


def run(paths: Paths) -> tuple[list[dict], int]:
    held = lock_held(paths.lock)
    checks = [
        check_python(),
        check_dependencies(),
        check_books(paths.books),
        check_signoff(paths.config),
        check_warehouse(paths.warehouse, held),
        check_lock(paths.lock, held),
        check_truth(paths.truth),
    ]
    statuses = {c["status"] for c in checks}
    return checks, 2 if "fail" in statuses else 1 if "warn" in statuses else 0


def render(checks: list[dict]) -> list[str]:
    return [f"{c['status'].upper():4} {c['id']}: {c['detail']}" for c in checks]
