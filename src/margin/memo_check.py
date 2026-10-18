"""`memo --check`: the built memo and site, proved traceable.

Four checks run on the files `memo` just wrote:

- `CHK-MEMO-NUM`: every `data-num-id` resolves through `explain` to source rows whose digests re-derive
  (a mismatch is `explain`'s exit 6, naming the ID), and no numeral stands in the prose outside the
  allowlist: years, month names, ISO dates, section numbers, quarter labels, account identifiers, GL codes
  in their column, and `data-literal="cite"` spans whose `ref/` ID has a record.
- `CHK-MEMO-QUOTE`: a `<blockquote>`, or a quoted string of more than six words, must be in
  `data/reference/quotes.yaml`; wording attributed to an outside source (`according to`, `as X put it`,
  `X writes`) fails whether or not a quote is attached.
- `CHK-MEMO-SITE`: `index.html` and the month's `index.html` exist under the output directory.
- `CHK-CRAWL` (with `crawl`): every internal link under the output directory reaches a file, and a fragment
  reaches an anchor in it; a link marked `data-deferred="agreement"` is skipped, because the file it names is
  written by a later step.

A failed check is a result with `status: fail`; a digest or value mismatch is a `MarginError` with exit 6,
raised only when no check failed, because the error contract puts exit 3 before exit 6.
"""

from __future__ import annotations

import html as html_lib
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import duckdb
import yaml

from . import explain as explain_mod
from . import paths, prov
from .errors import EXIT_PROVENANCE, MarginError
from .memo import CITE_ID, NUM_ID

CHK_NUM = "CHK-MEMO-NUM"
CHK_QUOTE = "CHK-MEMO-QUOTE"
CHK_SITE = "CHK-MEMO-SITE"
CHK_CRAWL = "CHK-CRAWL"
QUOTES_FILE = "quotes.yaml"
# A quoted string of more words than this is a quotation that must be registered.
QUOTE_WORDS = 6

NUM_VALUE = re.compile(r'data-num-id="([^"]+)" data-value="([^"]*)"')
CITE_SPAN = re.compile(r'<span data-literal="cite" data-ref-id="([^"]+)">.*?</span>', re.S)
NUM_SPAN = re.compile(r'<span data-num-id="[^"]+" data-value="[^"]*">.*?</span>', re.S)
MONTH_NAMES = "January|February|March|April|May|June|July|August|September|October|November|December"
# Text that may carry digits without being a number: ISO dates and months, month names (with a day or year),
# years, section numbers, quarter labels and account identifiers.
ALLOWED_TEXT = re.compile(
    r"\b\d{4}-\d{2}(?:-\d{2})?\b"
    rf"|\b(?:{MONTH_NAMES})\b(?:\s+\d{{1,2}}\b)?(?:,?\s+\d{{4}}\b)?"
    r"|\b(?:19|20)\d{2}\b"
    r"|(?:§\s*|\b[Ss]ections?\s+)\d+(?:\.\d+)*"
    r"|\bQ[1-4]\b"
    r"|\bacct_\d+\b"
)
BLOCKQUOTE = re.compile(r"<blockquote\b[^>]*>(.*?)</blockquote>", re.S | re.I)
QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')
# A subject that is not a source: a sentence starting `It writes` attributes nothing.
NOT_A_SOURCE = {"It", "This", "That", "The", "These", "Those", "He", "She", "They", "We", "You", "I", "Who", "Which"}
ATTRIBUTION = (
    re.compile(r"\baccording to\b", re.I),
    re.compile(r"\bas (?:[A-Z][\w.'’-]*)(?: [A-Z][\w.'’-]*)* (?:put|puts|said|says|wrote|writes)\b"),
    re.compile(r"\bas (?:\w+ ){1,3}put it\b", re.I),
    re.compile(r"\b([A-Z][\w.'’-]*(?: [A-Z][\w.'’-]*)*) (?:writes|wrote)\b"),
)


def _text(html: str) -> str:
    """The visible text of a page: no style or script, no tags, entities decoded."""
    body = re.sub(r"<(style|script)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", body))


def _words(text: str) -> str:
    return " ".join(text.split())


# ------------------------------------------------------------------ numbers


def _source_rows(node: dict) -> int:
    return len(node.get("rows", [])) + sum(_source_rows(c) for c in node.get("children", []))


def _mart_values(con: duckdb.DuckDBPyConnection, ids: list[str]) -> dict[str, str]:
    """The cents the mart holds for each `cp/` component ID in `ids`, as text."""
    components = {c: f"{c}_cents" for c in (*prov.LEAF_COMPONENTS, "contribution_profit")}
    wanted: dict[str, list[tuple[str, str]]] = {}
    for number_id in ids:
        parts = number_id.split("/")
        if len(parts) == 4 and parts[0] == "cp" and parts[3] in components:
            wanted.setdefault(parts[1], []).append((parts[2], parts[3]))
    out: dict[str, str] = {}
    for month, pairs in wanted.items():
        for component in {c for _, c in pairs}:
            rows = con.execute(
                f"SELECT account_id, {components[component]} FROM mart.fct_contribution_profit WHERE month = ?",
                [prov.month_date(month)],
            ).fetchall()
            out.update({f"cp/{month}/{a}/{component}": str(v) for a, v in rows})
    return out


def check_numbers(con: duckdb.DuckDBPyConnection, html: str) -> tuple[list[str], list[str]]:
    """Resolve every number ID and cite ID of `html` through `explain`: (the IDs that resolved, the problems).

    A number with no record, or whose chain ends in no source row while its value is not zero, is a problem.
    A digest that does not re-derive is `explain`'s exit 6 and propagates, naming the ID; so does a
    component whose `data-value` differs from the mart's cents."""
    values = dict(NUM_VALUE.findall(html))
    ids = sorted(set(NUM_ID.findall(html)) | set(CITE_ID.findall(html)))
    resolved: list[str] = []
    problems: list[str] = []
    for number_id in ids:
        try:
            node = explain_mod.explain(con, number_id)
        except MarginError as err:
            if err.exit_code == EXIT_PROVENANCE:
                raise
            problems.append(f"{number_id}: {err.message}")
            continue
        value = node.get("value_cents") if node.get("value_cents") is not None else node.get("value_text")
        if _source_rows(node) == 0 and value not in (0, "0", "n/a", None):
            problems.append(f"{number_id}: resolves to no source row")
            continue
        resolved.append(number_id)
    for number_id, held in _mart_values(con, ids).items():
        if number_id in values and values[number_id] != held:
            raise MarginError(
                EXIT_PROVENANCE,
                f"value mismatch for {number_id}: the page carries {values[number_id]}, the mart holds {held}",
            )
    return resolved, problems


def naked_numerals(html: str, registry) -> list[str]:
    """The numerals in the page text that no registered number accounts for: each as the snippet around it.

    `registry` holds the IDs that have a record; a cite span passes only when its `ref/` ID is among them.
    The spans of the number registry are removed first, then tags; GL codes pass only as a whole table cell."""
    body = NUM_SPAN.sub(" ", html)
    body = CITE_SPAN.sub(lambda m: " " if m.group(1).startswith("ref/") and m.group(1) in registry else m.group(0), body)
    body = re.sub(rf"<td[^>]*>\s*(?:{'|'.join(prov.ALL_GL_CODES)})\s*</td>", " ", body)
    text = ALLOWED_TEXT.sub(" ", _text(body))
    return [_words(m) for m in re.findall(r".{0,20}\d.{0,20}", text)]


# ------------------------------------------------------------------ quotes


def load_quotes(directory: Path | None = None) -> list[str]:
    """The registered quotation texts: entries that carry `text`, `verified_on` and `source_url`."""
    path = (directory or paths.default_reference()) / QUOTES_FILE
    if not path.is_file():
        return []
    entries = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("quotes") or []
    return [_words(str(e["text"])) for e in entries
            if isinstance(e, dict) and all(str(e.get(k) or "").strip() for k in ("text", "verified_on", "source_url"))]


def unregistered_quotes(html: str, quotes: list[str]) -> list[str]:
    """Every `<blockquote>`, every quoted string of more than six words, and every sentence attributing
    wording to an outside source, that the registry does not hold."""
    registered = {_words(q) for q in quotes}
    found = [_words(_text(m)) for m in BLOCKQUOTE.findall(html)]
    text = _text(BLOCKQUOTE.sub(" ", html))
    for m in QUOTED.finditer(text):
        quoted = _words(m.group(1) or m.group(2))
        if len(quoted.split()) > QUOTE_WORDS:
            found.append(quoted)
    for pattern in ATTRIBUTION:
        for m in pattern.finditer(text):
            if m.groups() and m.group(1) in NOT_A_SOURCE:
                continue
            found.append(_words(text[max(m.start() - 20, 0):m.end() + 20]))
    return [q for q in found if q not in registered]


# ------------------------------------------------------------------ the crawl


class _Page(HTMLParser):
    """The `href`s of a page, except the deferred ones, and the anchors a link may name."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.links: list[str] = []
        self.anchors: set[str] = set()
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        self.anchors.update(found[k] for k in ("id", "name") if found.get(k))
        if found.get("href") is not None and found.get("data-deferred") != "agreement":
            self.links.append(found["href"])


def crawl(site_dir: Path) -> list[str]:
    """Walk the internal links of every page under `site_dir`, offline; return the broken ones as
    `page: href -> path, why`."""
    root = Path(os.path.abspath(site_dir))
    pages: dict[Path, _Page] = {}

    def page(path: Path) -> _Page:
        if path not in pages:
            pages[path] = _Page(path.read_text(encoding="utf-8", errors="replace"))
        return pages[path]

    def rel(path: Path) -> str:
        return os.path.relpath(path, root)

    broken: list[str] = []
    for source in sorted(root.rglob("*.html")):
        for href in page(source).links:
            parts = urlsplit(href)
            if parts.scheme or parts.netloc:
                continue  # an outside link is not the crawl's to follow
            if parts.path.startswith("/"):
                target = root / unquote(parts.path).lstrip("/")
            else:
                target = source.parent / unquote(parts.path) if parts.path else source
            target = Path(os.path.normpath(target))
            if target.is_dir():
                target = target / "index.html"
            where = f"{rel(source)}: {href} -> {rel(target)}"
            if root not in target.parents and target != root:
                broken.append(f"{where}, outside the site")
            elif not target.is_file():
                broken.append(f"{where}, no such file")
            elif parts.fragment and target.suffix == ".html" and unquote(parts.fragment) not in page(target).anchors:
                broken.append(f"{where}, no anchor {parts.fragment}")
    return broken


# ------------------------------------------------------------------ the run


def _result(check_id: str, problems: list[str]) -> dict:
    return {"id": check_id, "status": "fail" if problems else "pass", "violating_rows": len(problems),
            "detail": problems}


def _stored_hash_matches(con: duckdb.DuckDBPyConnection, month: str) -> None:
    """The month's stored records must still hash to the result hash its close stored; a mismatch is exit 6."""
    row = con.execute("SELECT result_hash FROM state.close_state WHERE month = ?",
                      [prov.month_date(month)]).fetchone()
    records = [dict(zip(("number_id", "value_cents", "value_text", "unit", "input_digest"), r)) for r in con.execute(
        "SELECT number_id, value_cents, value_text, unit, input_digest FROM prov.records WHERE month = ?",
        [prov.month_date(month)]).fetchall()]
    if row and row[0] and prov.result_hash(records) != row[0]:
        raise MarginError(
            EXIT_PROVENANCE,
            f"result_hash/{month}: the stored records no longer hash to the result hash the close stored",
            month=month)


def run(con: duckdb.DuckDBPyConnection, month: str, out: Path, crawl_site: bool = False) -> list[dict]:
    """The checks over the memo and site in `out`, as control results (`id`, `status`, `violating_rows`,
    `detail`). Raises the exit 6 `MarginError` of a digest or value mismatch when no check failed."""
    site_problems = [f"{name} is missing" for name in ("index.html", f"{month}/index.html")
                     if not (out / name).is_file()]
    html = (out / month / "index.html").read_text(encoding="utf-8") if not site_problems else ""
    provenance: MarginError | None = None
    resolved: list[str] = []
    problems: list[str] = []
    try:
        resolved, problems = check_numbers(con, html)
        _stored_hash_matches(con, month)
    except MarginError as err:
        if err.exit_code != EXIT_PROVENANCE:
            raise
        provenance = err
    registry = {r[0] for r in con.execute("SELECT number_id FROM prov.records WHERE starts_with(number_id, 'ref/')").fetchall()}
    naked = [f"numeral outside the registry: {s}" for s in naked_numerals(html, registry)]
    results = [
        _result(CHK_NUM, problems + naked),
        _result(CHK_QUOTE, [f"unregistered quotation: {q}" for q in unregistered_quotes(html, load_quotes())]),
        _result(CHK_SITE, site_problems),
    ]
    if crawl_site:
        results.append(_result(CHK_CRAWL, [f"broken link {b}" for b in crawl(out)]))
    if provenance and not any(r["status"] == "fail" for r in results):
        provenance.controls = results
        raise provenance
    if provenance:  # exit 3 wins, and still names the exit 6 failure
        results[0] = _result(CHK_NUM, [*results[0]["detail"], provenance.message])
    return results
