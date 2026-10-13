"""Books-directory conventions shared by the generator and the loader (plan §7.2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import EXIT_PRECONDITION, EXIT_USAGE, MarginError


def manifest_path(books: Path) -> Path:
    """The manifest sits beside the books directory: `<books dir>.manifest.sha256`."""
    return books.parent / (books.name + ".manifest.sha256")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(books: Path) -> str:
    """Write the manifest of every file under `books`; return the books id."""
    lines = [
        f"{file_sha256(p)}  {p.relative_to(books).as_posix()}"
        for p in sorted(books.rglob("*"))
        if p.is_file()
    ]
    manifest_path(books).write_text(
        "# account-margin books manifest: sha256, two spaces, path\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return books_id_of(lines)


def books_id_of(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def read_books_id(books: Path) -> str:
    """`state.books.books_id`: the SHA-256 of the manifest's file-hash lines."""
    path = manifest_path(books)
    if not path.is_file():
        raise MarginError(
            EXIT_PRECONDITION, f"no manifest for books {books}: run margin gen first"
        )
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and ln[0] != "#"]
    return books_id_of(lines)


def read_window(books: Path) -> tuple[str, str]:
    path = books / "window.json"
    if not path.is_file():
        raise MarginError(EXIT_PRECONDITION, f"no books at {books}: run margin gen first")
    window = json.loads(path.read_text(encoding="utf-8"))
    return window["first"], window["last"]


def months_between(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:])
    ly, lm = int(last[:4]), int(last[5:])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def check_in_window(books: Path, month: str) -> tuple[str, str]:
    first, last = read_window(books)
    if not first <= month <= last:
        raise MarginError(
            EXIT_USAGE, f"month {month} is outside the books window: valid range {first}..{last}"
        )
    return first, last
