"""`margin serve`: the page, `/health`, the served site, the 404 and 409 rules and the port resolution,
over the closed `mini` books with the scenario run and the 2026-03 memo built."""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import pytest

from conftest import MINI_CONFIG, margin
from margin import inject, serve
from margin.errors import MarginError
from margin.lock import warehouse_lock

MONTH = "2026-03"


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode()

    @property
    def json(self) -> dict:
        return json.loads(self.body)


class Served:
    """A server in a daemon thread over a served root, and a client that checks, after every
    request, that the serving process holds no advisory lock."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.server = serve.make_server(("127.0.0.1", 0), root)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, path: str, method: str = "GET", *, probe: bool = True) -> Response:
        req = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                out = Response(res.status, dict(res.headers), res.read())
        except urllib.error.HTTPError as err:
            out = Response(err.code, dict(err.headers), err.read())
        if probe:
            self.assert_lock_free()
        return out

    def get(self, path: str) -> Response:
        return self.request(path)

    def post(self, path: str) -> Response:
        return self.request(path, "POST")

    def assert_lock_free(self) -> None:
        fd = os.open(str(self.root / "serve.duckdb.lock"), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if the server holds it
        finally:
            os.close(fd)


@pytest.fixture(scope="module")
def served_root(tmp_path_factory, mini_closed_march) -> Path:
    """A served root: the `mini` warehouse closed through 2026-03 with its scenario and memo, and the
    pristine copies of both."""
    root = tmp_path_factory.mktemp("serve-root")
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


@pytest.fixture(scope="module")
def served(served_root):
    server = Served(served_root)
    yield server
    server.close()


@pytest.fixture
def private_server(served_root, tmp_path):
    """A server over a copy of the served root, for tests that break the warehouse."""
    root = tmp_path / "root"
    shutil.copytree(served_root, root)
    server = Served(root)
    yield server
    server.close()


def cli_months(root: Path) -> list[dict]:
    result = margin("status", "--fixture", "mini", "--warehouse", str(root / "serve.duckdb"), "--json")
    assert result.code == 0, result.stderr
    return [{k: m[k] for k in ("month", "status", "result_hash")} for m in result.json["months"]]


def tag(page: str, element: str, id_: str) -> str:
    """The opening tag of `<element id=id_>`."""
    found = re.search(rf"<{element}\b[^>]*\bid=\"{id_}\"[^>]*>", page)
    assert found, f"no {element}#{id_} on the page"
    return found.group(0)


# ------------------------------------------------------------------ /health and the page


def test_health_lists_the_months_of_status(served, served_root):
    res = served.get("/health")
    assert res.status == 200
    assert res.headers["Content-Type"] == "application/json"
    assert res.json == {"status": "ok", "months": cli_months(served_root)}
    assert [m["month"] for m in res.json["months"]] == ["2026-02", MONTH]


def test_index_lists_months_the_next_close_the_dropdown_and_the_memo(served, served_root):
    res = served.get("/")
    assert res.status == 200
    assert res.headers["Content-Type"].startswith("text/html")
    page = res.text
    for month in cli_months(served_root):
        row = re.search(rf'<tr><td data-field="month">{month["month"]}</td>'
                        rf'<td data-field="status">{month["status"]}</td>'
                        rf'<td class="hash" data-field="result_hash">{month["result_hash"]}</td></tr>', page)
        assert row, month
    button = re.search(r'<button[^>]*id="close-button"([^>]*)>(.*?)</button>', page, re.S)
    assert "2026-04" in button.group(2) and "disabled" not in button.group(1)
    assert 'data-post="/close"' in button.group(1)
    select = re.search(r'<select id="error"[^>]*>(.*?)</select>', page, re.S).group(1)
    assert re.findall(r'<option value="([^"]+)">', select) == list(inject.ERRORS)
    assert 'data-post="/reset"' in tag(page, "button", "reset-button")
    assert f'href="/site/{MONTH}/index.html"' in page
    assert '<pre id="log"' in page and "fetch(" in page
    assert "http://" not in page and "https://" not in page  # nothing is loaded from outside


def test_head_index_has_no_body(served):
    res = served.request("/", "HEAD")
    assert res.status == 200
    assert res.body == b""
    assert int(res.headers["Content-Length"]) == len(served.get("/").body)


# ------------------------------------------------------------------ the served site


def test_the_memo_page_and_its_first_explain_link_are_served(served):
    memo = served.get(f"/site/{MONTH}/index.html")
    assert memo.status == 200
    assert memo.headers["Content-Type"].startswith("text/html")
    href = re.search(r'<a href="([^"]+)"><span data-num-id=', memo.text).group(1)
    explain = served.get(urljoin(f"/site/{MONTH}/index.html", href))
    assert explain.status == 200
    assert 'data-field="verified">yes' in explain.text


def test_a_directory_serves_its_index_and_the_other_files_their_types(served):
    assert served.get(f"/site/{MONTH}/").text == served.get(f"/site/{MONTH}/index.html").text
    assert served.get(f"/site/{MONTH}/values.csv").headers["Content-Type"].startswith("text/csv")
    assert served.get("/site/llms.txt").headers["Content-Type"].startswith("text/plain")


def test_a_directory_without_an_index_is_not_listed(served):
    assert served.get("/site/explain/").status == 404


@pytest.mark.parametrize("path", [
    "/site/../serve.duckdb",
    "/site/%2e%2e/serve.duckdb",
    "/site/%2e%2e%2fserve.duckdb",
    "/site//etc/passwd",
])
def test_a_path_outside_the_site_is_404(served, path):
    res = served.get(path)
    assert res.status == 404
    assert res.json == {"status": "fail", "message": "not found"}


def test_an_unknown_path_is_404(served):
    for path in ("/nothing", "/site/nothing.html", "/health/x"):
        res = served.get(path)
        assert res.status == 404
        assert res.json == {"status": "fail", "message": "not found"}
    assert served.post("/nothing").status == 404


# ------------------------------------------------------------------ the action routes and their lock


def test_a_post_while_the_action_lock_is_held_is_409(served):
    assert serve.ACTION_LOCK.acquire(blocking=False)
    try:
        res = served.post("/close")
        assert res.status == 409
        assert res.json == {"status": "busy", "message": "a close is running"}
        assert served.post("/reset").status == 409
        assert served.post("/inject/sign_flip").status == 409
        assert served.post("/inject/no_such_error").status == 404  # before the lock is looked at
    finally:
        serve.ACTION_LOCK.release()


def test_the_other_action_routes_answer_501_until_they_are_implemented(served):
    for path in ("/reset", "/inject/sign_flip"):
        res = served.post(path)
        assert res.status == 501
        assert res.json == {"status": "fail", "message": "not implemented"}
    assert not serve.ACTION_LOCK.locked()


def test_an_error_name_outside_the_list_is_404(served):
    res = served.post("/inject/no_such_error")
    assert res.status == 404
    assert not serve.ACTION_LOCK.locked()


@pytest.mark.parametrize("path", ["/close", "/reset", "/inject/sign_flip"])
def test_get_on_an_action_path_is_405(served, path):
    assert served.get(path).status == 405


def test_other_methods_are_405(served):
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        res = served.request("/", method)
        assert res.status == 405
        assert res.headers["Allow"] == "GET, HEAD, POST"


# ------------------------------------------------------------------ the close stream


def post_stream(server: Served, path: str = "/close", first_line_within: float = 10.0) -> tuple[http.client.HTTPResponse, list[str]]:
    """POST and read the response one line at a time; the first line must arrive within the deadline,
    far shorter than a `mini` close."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=300)
    start = time.monotonic()
    conn.request("POST", path, body=b"")
    res = conn.getresponse()
    first = res.readline().decode().rstrip("\n")
    assert time.monotonic() - start < first_line_within, first
    lines = [first, *(raw.decode().rstrip("\n") for raw in res)]
    conn.close()
    return res, lines


def test_a_close_streams_closes_the_next_month_and_builds_its_memo(private_server):
    server = private_server
    before = cli_months(server.root)
    assert [m["month"] for m in before] == ["2026-02", MONTH]
    res, lines = post_stream(server)
    assert res.status == 200, lines
    assert res.getheader("Transfer-Encoding") == "chunked"
    assert res.getheader("Content-Type") == "text/plain; charset=utf-8"
    assert lines[0] == "closing 2026-04 (a mini close takes about forty seconds)"
    assert "running the scenario for 2026-04" in lines
    assert lines.index("running the scenario for 2026-04") < lines.index("building the memo for 2026-04")
    assert any(line.startswith("PASS ") for line in lines)  # the close's control lines stream through
    server.assert_lock_free()

    after = {m["month"]: m for m in cli_months(server.root)}
    assert after["2026-04"]["status"] == "closed"
    assert lines[-1] == f"done 2026-04 closed {after['2026-04']['result_hash']}"

    memo = server.get("/site/2026-04/index.html")
    assert memo.status == 200
    href = re.search(r'<a href="([^"]+)"><span data-num-id=', memo.text).group(1)
    assert server.get(urljoin("/site/2026-04/index.html", href)).status == 200

    page = server.get("/").text
    assert re.search(r'<td data-field="month">2026-04</td><td data-field="status">closed</td>', page)
    assert 'href="/site/2026-04/index.html"' in page
    assert "disabled" in tag(page, "button", "close-button")
    assert "every month of the books is closed" in page

    again = server.post("/close")
    assert again.status == 409
    assert again.json == {"status": "busy", "message": "every month of the books is closed"}
    assert not serve.ACTION_LOCK.locked()

    state = margin("status", "--fixture", "mini", "--warehouse", str(server.root / "serve.duckdb"), "--json").json
    for path in ("/", "/health", "/site/2026-04/index.html"):
        server.get(path)
    assert margin("status", "--fixture", "mini", "--warehouse", str(server.root / "serve.duckdb"), "--json").json == state


def test_a_close_that_fails_reports_the_exit_and_leaves_the_month_open(private_server, tmp_path, monkeypatch):
    unsigned = tmp_path / "no-signoff"
    unsigned.mkdir()
    monkeypatch.setattr(serve, "SIGNOFF", str(unsigned))
    before = cli_months(private_server.root)
    res, lines = post_stream(private_server)
    assert res.status == 200, lines
    assert lines[0].startswith("closing 2026-04 ")
    assert re.fullmatch(r"failed exit 4: .+", lines[-1])
    assert not any(line.startswith("running the scenario") for line in lines)
    assert cli_months(private_server.root) == before
    assert not serve.ACTION_LOCK.locked()
    private_server.assert_lock_free()


# ------------------------------------------------------------------ a warehouse that cannot be read


def test_a_missing_warehouse_is_503_and_the_page_disables_the_actions(private_server):
    (private_server.root / "serve.duckdb").unlink()
    health = private_server.get("/health")
    assert health.status == 503
    assert health.json == {"status": "fail", "message": "warehouse not readable"}
    page = private_server.get("/").text
    assert "warehouse not readable" in page
    for button in ("close-button", "reset-button", "inject-button"):
        assert "disabled" in tag(page, "button", button)
    assert f'href="/site/{MONTH}/index.html"' in page
    assert private_server.get(f"/site/{MONTH}/index.html").status == 200
    assert not (private_server.root / "serve.duckdb").exists()  # reading it did not create one


def test_a_warehouse_held_by_a_close_is_reported_as_running(private_server):
    with warehouse_lock(private_server.root / "serve.duckdb.lock", exclusive=True):
        health = private_server.request("/health", probe=False)
        page = private_server.request("/", probe=False).text
    assert health.status == 503
    assert health.json == {"status": "fail", "message": "warehouse not readable"}
    assert "a close is running" in page
    assert "disabled" in tag(page, "button", "close-button")
    assert f'href="/site/{MONTH}/index.html"' in page


def test_every_month_closed_disables_the_button(tmp_path, mini_closed):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(mini_closed, root / "serve.duckdb")
    server = Served(root)
    try:
        page = server.get("/").text
    finally:
        server.close()
    assert "disabled" in tag(page, "button", "close-button")
    assert "every month of the books is closed" in page


# ------------------------------------------------------------------ the port and the command


def test_resolve_port():
    assert serve.resolve_port(None, {}) == 8000
    assert serve.resolve_port(9000, {"PORT": "1234"}) == 9000
    assert serve.resolve_port(None, {"PORT": "1234"}) == 1234
    assert serve.resolve_port(0, {}) == 0


@pytest.mark.parametrize("value", ["abc", "", "0", "65536", "-1", "80.5", " 80"])
def test_a_malformed_port_is_exit_2(value):
    with pytest.raises(MarginError) as err:
        serve.resolve_port(None, {"PORT": value})
    assert err.value.exit_code == 2
    assert err.value.message == "PORT must be an integer in 1..65535"


def test_serve_with_an_unbuilt_root_exits_2(tmp_path):
    result = margin("serve", "--port", "0", "--json", env={"MARGIN_SERVE_ROOT": str(tmp_path)})
    assert result.code == 2
    assert result.json["status"] == "fail"
    assert result.json["message"] == "serve root not built: run the image build steps"


def test_serve_with_a_malformed_port_exits_2(tmp_path):
    result = margin("serve", "--json", env={"MARGIN_SERVE_ROOT": str(tmp_path), "PORT": "http"})
    assert result.code == 2
    assert result.json["message"] == "PORT must be an integer in 1..65535"
