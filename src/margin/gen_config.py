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
KNOWN_DRIVERS = ("tokens", "revenue", "usage_accounts", "tickets")
KNOWN_SHARED_COSTS = ("cloud_untagged", "cloud_unused_commitment", "support_shared", "platform_fees")
KNOWN_RULES = ("by_tokens", "by_revenue", "equal_usage", "by_driver")
# What each preset maps when a config does not spell it out: the first three put every shared
# cost under one driver, `by_driver` sends each cost to the driver that describes it.
BUILTIN_PRESETS: dict[str, dict[str, str]] = {
    "by_tokens": {cost: "tokens" for cost in KNOWN_SHARED_COSTS},
    "by_revenue": {cost: "revenue" for cost in KNOWN_SHARED_COSTS},
    "equal_usage": {cost: "usage_accounts" for cost in KNOWN_SHARED_COSTS},
    "by_driver": {
        "cloud_untagged": "tokens",
        "cloud_unused_commitment": "tokens",
        "support_shared": "tickets",
        "platform_fees": "revenue",
    },
}
DRIVER_CODES = ("DEEP_DISCOUNT", "MODEL_MIX", "SUPPORT_LOAD", "DEDICATED_CLOUD")


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
class Preset:
    """A named mapping of every shared cost to a driver."""

    name: str
    drivers: tuple[tuple[str, str], ...]  # (shared cost, driver), in KNOWN_SHARED_COSTS order

    def driver_for(self, shared_cost: str) -> str:
        return dict(self.drivers)[shared_cost]


@dataclass(frozen=True)
class Allocation:
    presets: tuple[Preset, ...]  # the allowed presets, in the order the file lists them
    recommended: str
    sha256: str

    @property
    def rules(self) -> tuple[str, ...]:
        """The allowed preset names: the view callers used when a rule was just a name."""
        return tuple(p.name for p in self.presets)


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


def _preset(path: Path, name: str, entry: object) -> Preset:
    drivers = entry.get("drivers") if isinstance(entry, dict) else None
    if not isinstance(drivers, dict):
        raise MarginError(EXIT_USAGE, f"{path}: preset {name} needs a drivers mapping")
    mapping = {str(k): str(v) for k, v in drivers.items()}
    for cost, driver in mapping.items():
        if cost not in KNOWN_SHARED_COSTS:
            raise MarginError(
                EXIT_USAGE,
                f"{path}: preset {name} names the shared cost {cost}, which is not one of "
                f"{', '.join(KNOWN_SHARED_COSTS)}",
            )
        if driver not in KNOWN_DRIVERS:
            raise MarginError(
                EXIT_USAGE,
                f"{path}: preset {name} maps {cost} to the driver {driver}, which is not one of "
                f"{', '.join(KNOWN_DRIVERS)}",
            )
    missing = [c for c in KNOWN_SHARED_COSTS if c not in mapping]
    if missing:
        raise MarginError(
            EXIT_USAGE, f"{path}: preset {name} maps no driver for the shared cost {', '.join(missing)}"
        )
    return Preset(name=name, drivers=tuple((c, mapping[c]) for c in KNOWN_SHARED_COSTS))


def load_allocation(config_dir: Path) -> Allocation:
    path = config_dir / ALLOCATION_FILE
    data = _read_yaml(path, "allocation")
    defined = data.get("presets") or {}
    if not isinstance(defined, dict):
        raise MarginError(EXIT_USAGE, f"{path}: presets must be a mapping of preset name to drivers")
    # `allowed:` names the presets; a file from before presets lists them under `rules:` instead,
    # and a file with only `presets:` allows the ones it defines.
    allowed = data.get("allowed")
    if allowed is None:
        allowed = list(data.get("rules") or defined)
    names = tuple(str(n) for n in allowed)
    unknown = [n for n in (*names, *map(str, defined)) if n not in KNOWN_RULES]
    if not names or unknown:
        raise MarginError(
            EXIT_USAGE,
            f"{path}: rules must be drawn from {', '.join(KNOWN_RULES)}"
            + (f" (unknown: {', '.join(dict.fromkeys(unknown))})" if unknown else ""),
        )
    # A defined preset is checked even when the file does not allow it; one that is not defined
    # takes its built-in mapping.
    resolved = {str(n): _preset(path, str(n), entry) for n, entry in defined.items()}
    presets = tuple(
        resolved[n] if n in resolved else _preset(path, n, {"drivers": BUILTIN_PRESETS[n]}) for n in names
    )
    recommended = str(data.get("recommended", ""))
    if recommended not in names:
        raise MarginError(EXIT_USAGE, f"{path}: recommended must be one of the allowed rules")
    return Allocation(presets=presets, recommended=recommended, sha256=sha256_file(path))


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
