"""Shared helpers: run `margin` as a subprocess against a temporary books/warehouse."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import settings

# The example database would otherwise create an untracked .hypothesis/ in the checkout.
settings.register_profile("margin", database=None)
settings.load_profile("margin")

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict:
        return json.loads(self.stdout)


def margin(*args: str, cwd: Path = ROOT, env: dict | None = None) -> Result:
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("MARGIN_")}
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
        return ["--books", str(self.books), "--warehouse", str(self.warehouse)]

    def gen(self, *extra: str) -> Result:
        return margin("gen", "--fixture", "small", "--out", str(self.books), "--json", *extra)

    def close(self, month: str = "2026-03", *extra: str) -> Result:
        return margin("close", "--month", month, "--json", *self.flags(), *extra)


@pytest.fixture(scope="session")
def closed(tmp_path_factory) -> Workspace:
    """`small` books generated and 2026-03 closed once, shared by the read-only tests."""
    ws = Workspace(tmp_path_factory.mktemp("closed"))
    assert ws.gen().code == 0
    result = ws.close()
    assert result.code == 0, result.stderr
    return ws


@pytest.fixture
def closed_copy(closed, tmp_path) -> Workspace:
    """A private copy of the closed workspace for tests that mutate it."""
    ws = Workspace(tmp_path)
    shutil.copytree(closed.books, ws.books)
    shutil.copy(closed.books.parent / "books.manifest.sha256", tmp_path / "books.manifest.sha256")
    shutil.copy(closed.warehouse, ws.warehouse)
    return ws
