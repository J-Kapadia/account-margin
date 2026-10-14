"""Generator inputs: the two config files and the model price list.

The generator reads its floor, rules, drivers and guard bands from the config directory
the way `close` does, and writes the SHA-256 of both files into the manifest header.
A missing policy file is a usage error (exit 2); a missing price list means the author
has not supplied a price list (exit 4).
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from .errors import EXIT_PRECONDITION, EXIT_USAGE, MarginError

POLICY_FILE = "policy.yaml"
ALLOCATION_FILE = "allocation.yaml"
KNOWN_RULES = ("by_tokens", "by_revenue", "equal_usage")
DRIVER_CODES = ("DEEP_DISCOUNT", "MODEL_MIX", "SUPPORT_LOAD", "DEDICATED_CLOUD")
NANO_PER_USD = 1_000_000_000


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Driver:
    code: str
    median_floor: Decimal


@dataclass(frozen=True)
class Policy:
    floor_pct: Decimal
    reason_margin: Decimal
    planted_band_pts: Decimal
    unplanted_band_pts: Decimal
    decoy_band_pts: Decimal
    cohort_rule: str
    drivers: tuple[Driver, ...]
    sha256: str


@dataclass(frozen=True)
class Allocation:
    rules: tuple[str, ...]
    recommended: str
    sha256: str


@dataclass(frozen=True)
class Config:
    policy: Policy
    allocation: Allocation


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    source_url: str
    retrieved_on: str

    @property
    def nano_in(self) -> int:
        """Integer nano-USD per input token (USD per million tokens x 1000)."""
        return int(self.input_per_mtok * 1000)

    @property
    def nano_out(self) -> int:
        return int(self.output_per_mtok * 1000)


@dataclass(frozen=True)
class Prices:
    path: Path
    rows: tuple[Price, ...]
    sha256: str


def _read_yaml(path: Path, what: str) -> dict:
    if not path.is_file():
        raise MarginError(EXIT_USAGE, f"{what} file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MarginError(EXIT_USAGE, f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise MarginError(EXIT_USAGE, f"{path}: expected a mapping at the top level")
    return data


def _number(data: dict, key: str, path: Path, *, minimum: Decimal | None = None) -> Decimal:
    if key not in data:
        raise MarginError(EXIT_USAGE, f"{path}: missing key {key!r}")
    try:
        value = Decimal(str(data[key]))
    except InvalidOperation:
        raise MarginError(EXIT_USAGE, f"{path}: {key} must be a number") from None
    if minimum is not None and value < minimum:
        raise MarginError(EXIT_USAGE, f"{path}: {key} must be at least {minimum}")
    return value


def load_policy(config_dir: Path) -> Policy:
    path = config_dir / POLICY_FILE
    data = _read_yaml(path, "policy")
    drivers = []
    for entry in data.get("drivers") or []:
        if not isinstance(entry, dict) or "code" not in entry:
            raise MarginError(EXIT_USAGE, f"{path}: each driver needs a code")
        drivers.append(Driver(str(entry["code"]), _number(entry, "median_floor", path, minimum=Decimal(0))))
    if sorted(d.code for d in drivers) != sorted(DRIVER_CODES):
        raise MarginError(
            EXIT_USAGE, f"{path}: drivers must be exactly {', '.join(DRIVER_CODES)}, in tie order"
        )
    zero = Decimal(0)
    return Policy(
        floor_pct=_number(data, "floor_pct", path, minimum=zero),
        reason_margin=_number(data, "reason_margin", path, minimum=zero),
        planted_band_pts=_number(data, "planted_band_pts", path, minimum=zero),
        unplanted_band_pts=_number(data, "unplanted_band_pts", path, minimum=zero),
        decoy_band_pts=_number(data, "decoy_band_pts", path, minimum=zero),
        cohort_rule=str(data.get("cohort_rule", "")),
        drivers=tuple(drivers),
        sha256=sha256_file(path),
    )


def load_allocation(config_dir: Path) -> Allocation:
    path = config_dir / ALLOCATION_FILE
    data = _read_yaml(path, "allocation")
    rules = tuple(str(k) for k in (data.get("rules") or {}))
    unknown = [r for r in rules if r not in KNOWN_RULES]
    if not rules or unknown:
        raise MarginError(
            EXIT_USAGE,
            f"{path}: rules must be drawn from {', '.join(KNOWN_RULES)}"
            + (f" (unknown: {', '.join(unknown)})" if unknown else ""),
        )
    recommended = str(data.get("recommended", ""))
    if recommended not in rules:
        raise MarginError(EXIT_USAGE, f"{path}: recommended must be one of the allowed rules")
    return Allocation(rules=rules, recommended=recommended, sha256=sha256_file(path))


def load_config(config_dir: Path) -> Config:
    """Policy first: its absence is the documented exit 2."""
    policy = load_policy(config_dir)
    return Config(policy=policy, allocation=load_allocation(config_dir))


def load_prices(path: Path) -> Prices:
    if not path.is_file():
        raise MarginError(
            EXIT_PRECONDITION,
            f"{path} not found: no model price list; pass --prices <file> to use a fixture price list",
        )
    rows: list[Price] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for n, rec in enumerate(csv.DictReader(fh), start=2):
            try:
                price = Price(
                    provider=rec["provider"].strip(),
                    model=rec["model"].strip(),
                    input_per_mtok=Decimal(rec["input_per_mtok"]),
                    output_per_mtok=Decimal(rec["output_per_mtok"]),
                    source_url=rec["source_url"].strip(),
                    retrieved_on=rec["retrieved_on"].strip(),
                )
            except (KeyError, InvalidOperation, AttributeError) as exc:
                raise MarginError(EXIT_USAGE, f"{path}:{n}: bad price row ({exc!r})") from None
            if not (price.provider and price.model and price.source_url and price.retrieved_on):
                raise MarginError(EXIT_USAGE, f"{path}:{n}: every row needs a source and a date")
            for per_mtok in (price.input_per_mtok, price.output_per_mtok):
                if per_mtok <= 0 or per_mtok * 1000 != int(per_mtok * 1000):
                    raise MarginError(
                        EXIT_USAGE,
                        f"{path}:{n}: {price.model}: a price must be positive and a whole "
                        "number of nano-USD per token",
                    )
            rows.append(price)
    models = [p.model for p in rows]
    if not rows or len(set(models)) != len(models):
        raise MarginError(EXIT_USAGE, f"{path}: needs at least one model, each listed once")
    return Prices(path=path, rows=tuple(rows), sha256=sha256_file(path))
