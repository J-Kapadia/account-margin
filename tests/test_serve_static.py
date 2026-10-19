"""`margin serve --static`: the page rendered once into a directory, its recorded close, one panel
per planted error, the memos beside it, and every link relative so the directory works at any path."""

from __future__ import annotations

import functools
import hashlib
import html
import http.server
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import duckdb
import pytest

from conftest import MINI_CONFIG, margin
from margin import gallery, inject

MONTH = "2026-03"
RECORDED = "2026-04"
BUDGET_SECONDS = 180
PREFIX = "/some/prefix/"


@pytest.fixture(scope="module")
def served_root(tmp_path_factory, mini_closed_march) -> Path:
    """A served root: `mini` closed through 2026-03 with its scenario and memo, and the pristine copies."""
    root = tmp_path_factory.mktemp("static-root")
    warehouse = root / "serve.duckdb"
    shutil.copy(mini_closed_march, warehouse)
    scenario = margin("scenario", "--fixture", "mini", "--month", MONTH, "--warehouse", str(warehouse),
                      "--config", str(MINI_CONFIG), "--json")
    assert scenario.code == 0, scenario.stderr
    memo = margin("memo", "--month", MONTH, "--fixture", "mini", "--warehouse", str(warehouse),
                  "--out", str(root / "site"), "--json")
    assert memo.code == 0, memo.stderr
    (root / "pristine").mkdir()
    shutil.copy(warehouse, root / "pristine" / "serve.duckdb")
    shutil.copytree(root / "site", root / "pristine" / "site")
    return root


def close_state(root: Path) -> bytes:
    con = duckdb.connect(str(root / "serve.duckdb"), read_only=True)
    try:
        rows = con.execute("SELECT * FROM state.close_state ORDER BY month").fetchall()
    finally:
        con.close()
    return repr(rows).encode()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def exported(served_root, tmp_path_factory):
    """`margin serve --static` once over the served root: the output directory, the run's seconds, and the
    served root's state before and after."""
    out = tmp_path_factory.mktemp("static-export") / "out"
    before = (close_state(served_root), tree_digest(served_root))
    started = time.monotonic()
    result = margin("serve", "--static", str(out), env={"MARGIN_SERVE_ROOT": str(served_root)})
    seconds = time.monotonic() - started
    assert result.code == 0, result.stderr
    after = (close_state(served_root), tree_digest(served_root))
    return {"out": out, "seconds": seconds, "before": before, "after": after, "result": result}


@pytest.fixture(scope="module")
def page(exported) -> str:
    return (exported["out"] / "index.html").read_text(encoding="utf-8")


def months_after_export(served_root: Path) -> list[dict]:
    result = margin("status", "--fixture", "mini", "--warehouse", str(served_root / "serve.duckdb"), "--json")
    assert result.code == 0, result.stderr
    return result.json["months"]


def month_rows(html: str, table: str) -> list[tuple[str, str, str]]:
    body = re.search(rf'<table id="{table}">(.*?)</table>', html, re.S).group(1)
    return re.findall(r'<td data-field="month">([^<]*)</td><td data-field="status">([^<]*)</td>'
                      r'<td class="hash" data-field="result_hash">([^<]*)</td>', body)


# ------------------------------------------------------------------ the page


def test_the_months_table_lists_the_served_months_with_the_hashes_status_reports(exported, page, served_root):
    months = months_after_export(served_root)
    assert [m["month"] for m in months] == ["2026-02", MONTH]
    assert month_rows(page, "months") == [(m["month"], m["status"], m["result_hash"]) for m in months]


def test_the_transcript_ends_with_the_hash_of_the_recorded_close(exported, page):
    transcript = re.search(r'<pre id="transcript">(.*?)</pre>', page, re.S).group(1).splitlines()
    assert transcript[0] == f"closing {RECORDED} (a mini close takes about forty seconds)"
    assert f"running the scenario for {RECORDED}" in transcript
    assert f"building the memo for {RECORDED}" in transcript
    assert "margin-static" not in "\n".join(transcript)  # the scratch directory is not shown
    done = re.fullmatch(rf"done {RECORDED} closed ([0-9a-f]+)", transcript[-1])
    assert done, transcript[-1]
    after = month_rows(page, "months-after")
    assert (RECORDED, "closed", done.group(1)) in after
    assert [m for m, _, _ in after] == ["2026-02", MONTH, RECORDED]


def test_one_panel_per_planted_error_names_the_control_of_its_capture(page):
    names = re.findall(r'<section class="panel" id="error-[^"]*" data-error="([^"]+)">', page)
    assert names == list(inject.ERRORS)
    for name in inject.ERRORS:
        case = gallery.case(name)
        control = case["capture"]["close"]["failed_controls"][0]["id"]
        panel = re.search(rf'data-error="{name}">(.*?)</section>', page, re.S).group(1)
        assert f"caught {control}" in panel
        text = html.unescape(panel)
        assert case["caught"] in text
        for step in case["remediation"]:
            assert step in text


def test_the_memos_of_the_served_month_and_the_recorded_month_are_in_the_export(exported, page):
    out = exported["out"]
    for month in (MONTH, RECORDED):
        assert (out / "site" / month / "index.html").is_file()
        assert f'href="site/{month}/index.html"' in page
    assert "2026-02" not in re.search(r'<section id="memos">(.*?)</section>', page, re.S).group(1)
    assert any((out / "site" / "explain").iterdir())
    index = (out / "site" / "index.html").read_text(encoding="utf-8")
    assert f'href="{MONTH}/"' in index and f'href="{RECORDED}/"' in index
    llms = (out / "site" / "llms.txt").read_text(encoding="utf-8")
    assert f"({MONTH}/values.csv)" in llms and f"({RECORDED}/values.csv)" in llms


def test_the_page_has_no_script(page):
    assert "<script" not in page.lower()


# ------------------------------------------------------------------ the served root and the budget


def test_the_served_root_is_unchanged(exported):
    assert exported["after"][0] == exported["before"][0]
    assert exported["after"][1] == exported["before"][1]


def test_the_export_finishes_within_its_budget(exported):
    assert exported["seconds"] <= BUDGET_SECONDS


def test_an_unbuilt_root_exits_2(tmp_path):
    result = margin("serve", "--static", str(tmp_path / "out"), env={"MARGIN_SERVE_ROOT": str(tmp_path / "none")})
    assert result.code == 2
    assert "serve root not built" in result.stderr
    assert not (tmp_path / "out").exists()


# ------------------------------------------------------------------ every link


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        self.found += [value for name, value in attrs if name in ("href", "src") and value is not None]


def links_of(path: Path) -> list[str]:
    parser = Links()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.found


def is_citation(page: Path, out: Path, link: str) -> bool:
    """The memo cites its sources with absolute links to the web; they are references, not files of
    the export. It also links `agreement.json`, a file only `agreement --write` makes and the served
    site does not have either."""
    if page.relative_to(out).parts[0] != "site":
        return False
    return urlsplit(link).scheme in ("http", "https") or urlsplit(link).path == "agreement.json"


def html_files(out: Path) -> list[Path]:
    return sorted(out.rglob("*.html"))


def local_target(page: Path, link: str, out: Path) -> Path:
    """The file a relative link on `page` names; a directory link names its `index.html`."""
    target = (page.parent / unquote(urlsplit(link).path)).resolve()
    assert target.is_relative_to(out.resolve()), f"{page}: {link} leaves the export"
    return target / "index.html" if target.is_dir() else target


def test_the_page_itself_links_to_nothing_outside_the_export(page, exported):
    links = links_of(exported["out"] / "index.html")
    assert links and all(not urlsplit(link).scheme and not link.startswith("/") for link in links)


def test_every_link_is_relative_and_resolves_to_a_file_in_the_export(exported):
    out = exported["out"]
    checked = 0
    for page in html_files(out):
        for link in links_of(page):
            parts = urlsplit(link)
            if not parts.path and parts.fragment:
                continue  # a link within the page
            if is_citation(page, out, link):
                continue
            assert not parts.scheme and not parts.netloc, f"{page.relative_to(out)}: {link} is not relative"
            assert not link.startswith("/"), f"{page.relative_to(out)}: {link} is absolute"
            assert local_target(page, link, out).is_file(), f"{page.relative_to(out)}: {link} resolves to no file"
            checked += 1
    assert checked > 100


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def test_every_link_returns_200_when_the_export_is_served_under_a_prefix(exported):
    out = exported["out"]

    class Prefixed(Quiet):
        def translate_path(self, path: str) -> str:
            path = urlsplit(path).path
            if not path.startswith(PREFIX):
                return str(out / "__outside_the_prefix__")
            return super().translate_path("/" + path[len(PREFIX):])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Prefixed, directory=str(out)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        seen: set[str] = set()
        for page in html_files(out):
            here = PREFIX + page.relative_to(out).as_posix()
            for link in links_of(page):
                parts = urlsplit(link)
                if not parts.path or is_citation(page, out, link):
                    continue
                url = base + urlsplit(_join(here, parts.path)).path
                if url in seen:
                    continue
                seen.add(url)
                try:
                    with urllib.request.urlopen(url, timeout=30) as res:
                        status = res.status
                except urllib.error.HTTPError as err:
                    status = err.code
                assert status == 200, f"{page.relative_to(out)}: {link} -> {url} is {status}"
        assert len(seen) > 50
    finally:
        server.shutdown()
        server.server_close()


def _join(here: str, link: str) -> str:
    from urllib.parse import urljoin
    return urljoin("http://x" + here, link)[len("http://x"):]
