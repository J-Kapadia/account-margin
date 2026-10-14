"""Path resolution: flag, then environment variable, then default.

`--fixture NAME` is sugar for `--books` (with its truth directory), for the fixture's
price list and config directory where it has its own, and, unless a warehouse is given,
for the default warehouse `warehouse/<NAME>.duckdb`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import EXIT_USAGE, MarginError

# src/margin/paths.py -> the repository root holding dbt/, controls/ and tests/.
ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PRICES = "data/reference/model_prices.csv"
# The committed `mini` books were generated against this price list, so the fixture
# closes against it too, not against the author's real list.
FIXTURE_PRICES = {"mini": "tests/fixtures/model_prices.csv"}
# ...and these config files, whose hashes the committed manifest records.
DEFAULT_CONFIG = "config"
FIXTURE_CONFIG = {"mini": "tests/fixtures/config"}

FIXTURE_BOOKS = {
    "small": ("data/books/small", "data/truth/small"),
    "mini": ("tests/fixtures/books", "tests/fixtures/truth"),
    "scenario": ("data/books/scenario", "data/truth/scenario"),
}


@dataclass(frozen=True)
class Paths:
    books: Path
    truth: Path
    warehouse: Path
    config: Path
    fixture: str | None
    truth_explicit: bool = False  # --truth or MARGIN_TRUTH was given
    prices: Path = Path(DEFAULT_PRICES)  # the pinned model price list

    @property
    def lock(self) -> Path:
        return Path(str(self.warehouse) + ".lock")


def resolve(
    *,
    books: str | None = None,
    truth: str | None = None,
    warehouse: str | None = None,
    config: str | None = None,
    fixture: str | None = None,
) -> Paths:
    env = os.environ
    if books is None and fixture is not None:
        books = FIXTURE_BOOKS[fixture][0]
    books_path = Path(books or env.get("MARGIN_BOOKS") or "data/books")

    if truth is None and fixture is not None and books == FIXTURE_BOOKS[fixture][0]:
        truth = FIXTURE_BOOKS[fixture][1]
    truth_path = Path(truth or env.get("MARGIN_TRUTH") or books_path.parent / "truth")

    default_warehouse = f"warehouse/{fixture}.duckdb" if fixture else "warehouse/margin.duckdb"
    warehouse_path = Path(warehouse or env.get("MARGIN_WAREHOUSE") or default_warehouse)
    config_path = Path(config or env.get("MARGIN_CONFIG_DIR") or FIXTURE_CONFIG.get(fixture or "", DEFAULT_CONFIG))
    explicit = truth is not None or bool(env.get("MARGIN_TRUTH"))
    prices_path = Path(env.get("MARGIN_PRICES") or FIXTURE_PRICES.get(fixture or "", DEFAULT_PRICES))
    return Paths(books_path, truth_path, warehouse_path, config_path, fixture, explicit, prices_path)


def parse_month(value: str) -> str:
    """Return `value` if it is YYYY-MM, else exit 2."""
    if len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit():
        if 1 <= int(value[5:]) <= 12:
            return value
    raise MarginError(EXIT_USAGE, f"invalid month {value!r}: expected YYYY-MM")
