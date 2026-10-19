"""`margin serve`: one web page over the committed `mini` books, closed ahead of time.

A standard-library `ThreadingHTTPServer`, no login, no upload, no database server. A request that
reads the warehouse opens it `read_only=True` under the shared advisory lock and closes it before
the handler returns, so the serving process holds nothing between requests and a `margin` run
started by an action can take the exclusive lock. The three action routes share `ACTION_LOCK`:
one action at a time, a second one is refused with 409 and starts nothing.

The served root holds `serve.duckdb`, `site/` and a `pristine/` copy of both. The books, the
config and the sign-offs are the `mini` fixture's; no request names a path, a month or a file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import books as books_mod
from . import inject as inject_mod
from . import status as status_mod
from .errors import EXIT_USAGE, MarginError
from .lock import LOCK_MESSAGE, warehouse_lock
from .paths import FIXTURE_BOOKS, ROOT
from .warehouse import connect

TEMPLATES = Path(__file__).resolve().parent / "templates"
SERVED_BOOKS = ROOT / FIXTURE_BOOKS["mini"][0]
DEFAULT_ROOT = "warehouse/serve"
DEFAULT_PORT = 8000
WAREHOUSE = "serve.duckdb"
UNBUILT_MESSAGE = "serve root not built: run the image build steps"
UNREADABLE = "warehouse not readable"
BUSY = "a close is running"
ALL_CLOSED = "every month of the books is closed"

# One action at a time. A POST route takes it without blocking, sets `running` to the kind of
# action it holds it for, and clears that in the `finally` that releases it.
ACTION_LOCK = threading.Lock()
RUNNING_LABELS = {"close": "a close", "inject": "an injection", "reset": "a reset"}
running: str | None = None

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".sql": "text/plain; charset=utf-8", ".json": "application/json",
}
SITE_MONTH = re.compile(r"\d{4}-\d{2}")

_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                   undefined=StrictUndefined)


def resolve_port(flag: int | None, environ: Mapping[str, str] | None = None) -> int:
    """`--port`, else `$PORT`, else 8000; a `$PORT` outside 1..65535 or not a whole number is exit 2."""
    if flag is not None:
        return flag
    raw = (os.environ if environ is None else environ).get("PORT")
    if raw is None:
        return DEFAULT_PORT
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 65535:
        raise MarginError(EXIT_USAGE, "PORT must be an integer in 1..65535")
    return int(raw)


def serve_root(environ: Mapping[str, str] | None = None) -> Path:
    """`MARGIN_SERVE_ROOT`, else `warehouse/serve`."""
    return Path((os.environ if environ is None else environ).get("MARGIN_SERVE_ROOT") or DEFAULT_ROOT)


def require_built(root: Path) -> None:
    """Exit 2 unless `root` holds the served warehouse; serve never builds it."""
    if not (root / WAREHOUSE).is_file():
        raise MarginError(EXIT_USAGE, UNBUILT_MESSAGE)


class Unreadable(Exception):
    """The served warehouse cannot be read now; `str()` is what the page says."""


def read_months(root: Path) -> list[dict]:
    """The months of the served warehouse, read under the shared lock; the connection and the lock
    are gone when this returns."""
    warehouse = root / WAREHOUSE
    if not warehouse.is_file():  # before the lock: a missing warehouse must not create its directory
        raise Unreadable(UNREADABLE)
    try:
        with warehouse_lock(Path(str(warehouse) + ".lock"), exclusive=False):
            con = connect(warehouse, read_only=True)
            try:
                return status_mod.status(con)
            finally:
                con.close()
    except MarginError as err:
        if err.message == LOCK_MESSAGE:
            raise Unreadable(BUSY) from None
        raise Unreadable(UNREADABLE) from None
    except (duckdb.Error, OSError):
        raise Unreadable(UNREADABLE) from None


def next_month(months: list[dict]) -> str | None:
    """The first month of the books' window that is not closed or locked yet."""
    first, last = books_mod.read_window(SERVED_BOOKS)
    done = {m["month"] for m in months if m["status"] in ("closed", "locked")}
    return next((m for m in books_mod.months_between(first, last) if m not in done), None)


def latest_memo(root: Path) -> str | None:
    """The latest month with a page under `site/`."""
    site = root / "site"
    months = [p.name for p in site.iterdir() if SITE_MONTH.fullmatch(p.name) and (p / "index.html").is_file()] \
        if site.is_dir() else []
    return max(months, default=None)


def render_index(root: Path) -> str:
    try:
        months, notice = read_months(root), None
    except Unreadable as err:
        months, notice = [], str(err)
    upcoming = None if notice else next_month(months)
    return _env.get_template("serve.html.j2").render(
        months=months, notice=notice, next_month=upcoming, all_closed=not notice and upcoming is None,
        all_closed_text=ALL_CLOSED, errors=inject_mod.ERRORS, memo_month=latest_memo(root),
    )


def action_of(path: str) -> tuple[str, str | None] | None:
    """The kind and argument of an action route; an `/inject/` name outside `inject.ERRORS` is none."""
    if path == "/close":
        return "close", None
    if path == "/reset":
        return "reset", None
    if path.startswith("/inject/") and path[len("/inject/"):] in inject_mod.ERRORS:
        return "inject", path[len("/inject/"):]
    return None


def site_file(root: Path, rel: str) -> Path | None:
    """The file `rel` names under `<root>/site/`, or None when it is outside, missing or a directory
    without an `index.html`."""
    if "\0" in rel:
        return None
    base = (root / "site").resolve()
    target = (base / rel).resolve()  # an absolute `rel` replaces `base`, and the check below refuses it
    if not target.is_relative_to(base):
        return None
    if target.is_dir():
        target = target / "index.html"
    return target if target.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server: "Server"
    protocol_version = "HTTP/1.1"  # the action beads stream chunked bodies
    server_version = "margin-serve"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class's signature
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    # ------------------------------------------------------------------ responses

    def _send(self, status: int, body: bytes, content_type: str, *, head: bool = False,
              extra: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")  # no keep-alive: a request body we do not read cannot leak into the next
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if not head:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, *, head: bool = False, extra: Mapping[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json", head=head, extra=extra)

    def _not_found(self, head: bool = False) -> None:
        self._json(HTTPStatus.NOT_FOUND, {"status": "fail", "message": "not found"}, head=head)

    def _not_allowed(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"status": "fail", "message": "method not allowed"},
                   extra={"Allow": "GET, HEAD, POST"})

    # ------------------------------------------------------------------ routes

    def _get(self, head: bool) -> None:
        path = urlsplit(self.path).path
        root = self.server.root
        if path == "/":
            self._send(HTTPStatus.OK, render_index(root).encode(), "text/html; charset=utf-8", head=head)
        elif path == "/health":
            try:
                months = [{k: m[k] for k in ("month", "status", "result_hash")} for m in read_months(root)]
            except Unreadable:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "fail", "message": UNREADABLE}, head=head)
            else:
                self._json(HTTPStatus.OK, {"status": "ok", "months": months}, head=head)
        elif path.startswith("/site/"):
            target = site_file(root, unquote(path[len("/site/"):]))
            if target is None:
                self._not_found(head)
                return
            try:
                body = target.read_bytes()
            except OSError:  # removed since the check: a reset replaces the site
                self._not_found(head)
                return
            content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(HTTPStatus.OK, body, content_type, head=head)
        elif action_of(path):
            self._not_allowed()
        else:
            self._not_found(head)

    def do_GET(self) -> None:  # noqa: N802
        self._get(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._get(head=True)

    def do_POST(self) -> None:  # noqa: N802
        global running
        action = action_of(urlsplit(self.path).path)
        if action is None:
            self._not_found()
            return
        if not ACTION_LOCK.acquire(blocking=False):
            label = RUNNING_LABELS.get(running or "close")
            self._json(HTTPStatus.CONFLICT, {"status": "busy", "message": f"{label} is running"})
            return
        running = action[0]
        try:
            self._json(HTTPStatus.NOT_IMPLEMENTED, {"status": "fail", "message": "not implemented"})
        finally:
            running = None
            ACTION_LOCK.release()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path) -> None:
        super().__init__(address, Handler)
        self.root = root


def make_server(address: tuple[str, int], root: Path) -> Server:
    """A server bound to `address` over the served root `root`; the caller runs `serve_forever`."""
    return Server(address, Path(root))


def run(host: str, port: int, root: Path) -> None:
    """Serve until interrupted."""
    server = make_server((host, port), root)
    sys.stderr.write(f"serving {root} on http://{host}:{server.server_address[1]}/\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
