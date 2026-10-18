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


def _siblings() -> set[str]:
    """The modules of the package: a `.py` stem, or a directory holding `__init__.py`."""
    return {p.stem for p in SRC.glob("*.py")} | {p.parent.name for p in SRC.glob("*/__init__.py")}


def _sources(module: str) -> list[Path]:
    """The files of a module: itself, or every `.py` under a package directory."""
    package = SRC / module
    return sorted(package.rglob("*.py")) if (package / "__init__.py").is_file() else [SRC / f"{module}.py"]


def imports_of(module: str) -> set[str]:
    """The sibling modules of the `margin` package that `module` imports; a package's imports are the
    union over its modules."""
    found: set[str] = set()
    for source in _sources(module):
        # `from . import x` in a module inside a package is the package's own; only `..` reaches the siblings.
        depth = len(source.relative_to(SRC).parts) - 1
        found |= _imports_in(ast.parse(source.read_text(encoding="utf-8")), depth)
    return found & _siblings() - {module}


def _imports_in(tree: ast.AST, depth: int) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == depth + 1:  # from . import x, y   /   from .x import y
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif node.level == 0 and node.module and node.module.split(".")[0] == "margin":
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
    return found


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


def test_the_gallery_is_reachable_from_the_memo_and_reaches_neither_the_reference_nor_the_cli():
    assert "gallery" in imports_of("memo")
    for module in ("memo", "gallery"):
        assert not {REFERENCE, "cli"} & reachable(module), module


def test_a_package_imports_the_union_over_its_modules(tmp_path, monkeypatch):
    fake = tmp_path / "margin"
    (fake / "pkg").mkdir(parents=True)
    (fake / "pkg" / "__init__.py").write_text("from .. import b\nfrom . import inner\n")
    (fake / "pkg" / "inner.py").write_text("from ..c import x\n")
    (fake / "b.py").write_text("x = 1\n")
    (fake / "c.py").write_text("x = 1\n")
    (fake / "a.py").write_text("from . import pkg\n")
    monkeypatch.setattr(sys.modules[__name__], "SRC", fake)
    assert imports_of("pkg") == {"b", "c"}
    assert reachable("a") == {"pkg", "b", "c"}
