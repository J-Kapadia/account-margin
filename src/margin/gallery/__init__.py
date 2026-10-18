"""The planted-errors gallery: one page per error `margin inject` knows, and an index.

Each error is its own module here (`ERROR`, `TITLE`, `CONTROL`, `PLANTED`, `CAUGHT`, `REMEDIATION`)
beside a committed capture, `captures/<error>.json`: what was planted in a copy of the `mini` books and
what the close answered. The gallery renders from those captures and closes nothing, because a close of
`mini` takes about forty seconds; `capture` re-runs the close to write or refresh one. This module
imports `inject`, `paths`, `errors` and Jinja2 only, so the memo can build the gallery without reaching
the close or the generator's reference.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .. import inject
from ..errors import EXIT_CONTROL, EXIT_INTERNAL, MarginError
from ..paths import ROOT

ERRORS = inject.ERRORS
CAPTURES = Path(__file__).parent / "captures"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
MODULE_FIELDS = ("ERROR", "TITLE", "CONTROL", "PLANTED", "CAUGHT", "REMEDIATION")
MONTH = "2026-03"


def case(error: str) -> dict | None:
    """The error's page data, or None while its module or its capture has not landed."""
    name = f"{__name__}.{error}"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as err:
        if err.name == name:
            return None
        raise
    capture_path = CAPTURES / f"{error}.json"
    if not capture_path.is_file():
        return None
    source = Path(module.__file__).name
    missing = [f for f in MODULE_FIELDS if not hasattr(module, f)]
    if missing:
        raise MarginError(EXIT_INTERNAL, f"{source} lacks {', '.join(missing)}")
    capture_json = json.loads(capture_path.read_text(encoding="utf-8"))
    if capture_json.get("error") != module.ERROR:
        raise MarginError(EXIT_INTERNAL, f"{capture_path.name} is the capture of {capture_json.get('error')!r}, "
                                         f"not {module.ERROR!r}")
    return {"error": module.ERROR, "title": module.TITLE, "control": module.CONTROL, "planted": module.PLANTED,
            "caught": module.CAUGHT, "remediation": list(module.REMEDIATION), "capture": capture_json}


def cases() -> list[dict]:
    """The cases that have landed, in the order of the index."""
    return [c for c in (case(e) for e in ERRORS) if c is not None]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                       undefined=StrictUndefined)


def render_index(cases: list[dict], repository: str) -> str:
    return _env().get_template("errors.html.j2").render(cases=cases, repository=repository)


def render_case(case: dict, repository: str) -> str:
    return _env().get_template("error.html.j2").render(case=case, repository=repository)


def build(out: Path, repository: str) -> list[Path]:
    """Write `out/errors/index.html` and one page per case; return the paths written."""
    found = cases()
    pages = {"index": render_index(found, repository)}
    pages.update({c["error"]: render_case(c, repository) for c in found})
    written = []
    for name, html in pages.items():
        path = out / "errors" / f"{name}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        written.append(path)
    return written


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "margin", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, check=False)


def capture(error: str, work: Path, *, books: Path = ROOT / "tests/fixtures/books") -> dict:
    """Plant `error` in a copy of `books` (the committed `mini` books) under `work`, close the copy in a
    warehouse of its own and return the capture. A close that does not fail with exit 3 is an internal error."""
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MARGIN_SIGNOFF_DIR": str(ROOT / "tests/fixtures/signoff"),
           "DBT_TARGET_PATH": str(work / "target"), "DBT_LOG_PATH": str(work / "logs")}
    planted = _run(["inject", "--fixture", "mini", "--books", str(books), "--error", error, "--out", str(work / "books"),
                     "--json"], env)
    if planted.returncode != 0:
        raise MarginError(EXIT_INTERNAL, f"inject {error} exited {planted.returncode}: {planted.stderr.strip()}")
    target = json.loads(planted.stdout)
    closed = _run(["close", "--fixture", "mini", "--books", str(work / "books"), "--warehouse",
                   str(work / "w.duckdb"), "--month", MONTH, "--json"], env)
    if closed.returncode != EXIT_CONTROL:
        raise MarginError(EXIT_INTERNAL, f"expected exit 3 for {error}, got {closed.returncode}")
    envelope = json.loads(closed.stdout)
    try:
        shown = str(books.relative_to(ROOT))
    except ValueError:
        shown = str(books)
    return {"error": error, "seed": target["seed"], "month": MONTH, "books": shown, "target": target["target"],
            "close": {"exit": envelope["exit"], "message": envelope["message"], "controls": envelope["controls"],
                      "failed_controls": envelope["failed_controls"]}}


def write_capture(error: str, data: dict) -> Path:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    path = CAPTURES / f"{error}.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
