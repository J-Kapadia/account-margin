"""`margin serve --static DIR`: the served page rendered once into a directory.

The export shows what the live page shows. The months come from a read-only `status` read of the
served warehouse. The close is run once, on a scratch copy of that warehouse, as the live action
runs it (`margin` subprocesses, the same flags), and its lines and result are recorded. Each
planted error's result is the committed capture of its close plus the gallery's prose, so no
injection and no close runs for them. The site is the served site with the memo of the recorded
month laid over it. Every link is relative and the page has no script, so the directory works at
any path of a static host.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import books as books_mod
from . import gallery
from . import inject as inject_mod
from . import memo as memo_mod
from . import serve
from . import status as status_mod
from .errors import EXIT_PRECONDITION, MarginError
from .warehouse import connect

TEMPLATE = "serve_static.html.j2"

_env = Environment(loader=FileSystemLoader(serve.TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                   undefined=StrictUndefined)


def read_months(warehouse: Path) -> list[dict]:
    """`status` over a read-only connection that is closed before this returns."""
    con = connect(warehouse, read_only=True)
    try:
        return status_mod.status(con)
    finally:
        con.close()


def served_months(root: Path) -> list[dict]:
    """The months of the served warehouse, read under the shared lock like a page request."""
    try:
        return serve.read_months(root)
    except serve.Unreadable as err:
        raise MarginError(EXIT_PRECONDITION, f"cannot read the served warehouse: {err}") from None


def window_months() -> list[str]:
    return books_mod.months_between(*books_mod.read_window(serve.SERVED_BOOKS))


def record_close(root: Path, months: list[dict], scratch: Path) -> dict | None:
    """Close the first unclosed month on a copy of the served warehouse in `scratch` and return the
    transcript lines and the months of the copy afterwards; None when every month is closed."""
    month = serve.next_month(months)
    if month is None:
        return None
    shutil.copy(root / serve.WAREHOUSE, scratch / serve.WAREHOUSE)
    lines = [f"closing {month} (a mini close takes about forty seconds)"]
    # the memo reports the files it wrote under the scratch directory: show them relative to the site
    serve.run_close(scratch, month, lambda line: lines.append(line.replace(f"{scratch}/", "")))
    return {"month": month, "lines": lines, "months": read_months(scratch / serve.WAREHOUSE)}


def panels() -> tuple[list[dict], list[str]]:
    """One panel per planted error whose module and capture have landed, and a line for each that has not."""
    found, skipped = [], []
    for name in inject_mod.ERRORS:
        case = gallery.case(name)
        if case is None:
            skipped.append(f"{name}: not captured yet, skipped")
            continue
        failed = case["capture"]["close"]["failed_controls"]
        found.append({"error": name, "title": case["title"], "control": failed[0]["id"] if failed else None,
                      "planted": case["planted"], "caught": case["caught"], "remediation": case["remediation"]})
    return found, skipped


def copy_site(root: Path, scratch: Path, out: Path) -> None:
    """The served site, then the scratch copy's site over it, then the root index and `llms.txt`
    for the months present."""
    site = out / "site"
    for source in (root / "site", scratch / "site"):
        if source.is_dir():
            shutil.copytree(source, site, dirs_exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    present = memo_months(site)
    (site / "llms.txt").write_text(memo_mod.llms_txt(present), encoding="utf-8")
    (site / "index.html").write_text(memo_mod.index_html(present), encoding="utf-8")


def memo_months(site: Path) -> list[str]:
    return sorted(d.name for d in site.iterdir() if serve.SITE_MONTH.fullmatch(d.name) and (d / "index.html").is_file())


def render(months: list[dict], recorded: dict | None, found: list[dict], skipped: list[str],
           memos: list[str]) -> str:
    return _env.get_template(TEMPLATE).render(
        months=months, recorded=recorded, transcript="\n".join(recorded["lines"]) if recorded else "",
        all_closed_text=serve.ALL_CLOSED, panels=found, skipped=skipped, memos=memos)


def export(root: Path, out: Path) -> dict:
    """Render the page for the served root `root` into `out` (created; existing files are overwritten)."""
    serve.require_built(root)
    root = root.resolve()
    months = served_months(root)
    scratch = Path(tempfile.mkdtemp(prefix="margin-static-"))
    try:
        recorded = record_close(root, months, scratch)
        found, skipped = panels()
        out.mkdir(parents=True, exist_ok=True)
        copy_site(root, scratch, out)
        memos = memo_months(out / "site")
        (out / "index.html").write_text(render(months, recorded, found, skipped, memos), encoding="utf-8")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"out": str(out), "months": [m["month"] for m in months], "recorded": recorded["month"] if recorded else None,
            "panels": [p["error"] for p in found], "memos": memos}
