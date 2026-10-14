"""The two-implementations rule: the close path never imports the generator's reference.

`truth_reference.py` applies the contribution formula and the apportionment independently of the
dbt macros, so `close`, `memo` and `agreement` must not reach it, directly or through another
module. A positive control shows the generator does reach it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "margin"
REFERENCE = "truth_reference"
FORBIDDEN_FROM = ("close", "memo", "agreement")


def imports_of(module: str) -> set[str]:
    """The sibling modules of the `margin` package that `module` imports."""
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    siblings = {p.stem for p in SRC.glob("*.py")}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:  # from . import x, y   /   from .x import y
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif node.module and node.module.split(".")[0] == "margin":
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
                else:
                    found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "margin" and len(parts) > 1:
                    found.add(parts[1])
    return found & siblings


def reachable(start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        module = todo.pop()
        for dep in imports_of(module):
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return seen


@pytest.mark.parametrize("module", FORBIDDEN_FROM)
def test_the_close_path_never_reaches_the_truth_reference(module):
    if not (SRC / f"{module}.py").exists():
        pytest.skip(f"{module}.py has not landed yet")
    assert REFERENCE not in reachable(module), f"{module} must not import {REFERENCE}.py"


def test_the_close_and_memo_modules_exist_so_the_guard_is_not_vacuous():
    assert (SRC / "close.py").exists() and (SRC / "memo.py").exists()


def test_the_reference_imports_nothing_else_from_the_package():
    assert imports_of(REFERENCE) == set()


def test_the_generator_does_reach_the_reference():
    assert REFERENCE in reachable("gen")
    assert REFERENCE in imports_of("gen_books")


def test_the_import_scanner_sees_relative_and_absolute_forms(tmp_path, monkeypatch):
    fake = tmp_path / "margin"
    fake.mkdir()
    for name, body in {
        "a": "from . import b\n", "b": "from .c import x\n", "c": "import margin.d\n",
        "d": "from margin import e\n", "e": "x = 1\n", "f": "import os\n",
    }.items():
        (fake / f"{name}.py").write_text(body)
    monkeypatch.setattr(sys.modules[__name__], "SRC", fake)
    assert imports_of("a") == {"b"}
    assert reachable("a") == {"b", "c", "d", "e"}
    assert reachable("f") == set()
