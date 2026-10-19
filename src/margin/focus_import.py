# FOCUS 1.0 column definitions taken from the FinOps Foundation's published specification:
#   https://focus.finops.org/docs/specification/v1-0/
#   (source text: https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/tree/v1.0/specification/columns)
# retrieved 2026-10-19. data/reference/focus_columns.md records the same pin with the columns read.
"""Map an AWS Data Exports FOCUS 1.0 CSV to the books' `cloud_focus.csv`.

A pure, deterministic mapping with the standard library only: it keeps the pinned columns, drops
the rest, lowercases tag keys, numbers the rows, rounds each cost to cents and writes the
rounding as one `Adjustment` row per month so that the month's four cost totals still tie to the
export to the cent. It maps; it does not judge: a row the controls would fail is passed through.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .errors import EXIT_USAGE, MarginError

# The columns of `cloud_focus.csv`, in file order. `line_id` is assigned here, not read.
OUTPUT_COLUMNS = [
    "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency",
    "ServiceName", "ServiceCategory", "ResourceId", "Tags", "line_id",
    "ListCost", "ContractedCost", "ChargeCategory", "CommitmentDiscountCategory",
    "CommitmentDiscountType", "RegionId",
]
PINNED = [c for c in OUTPUT_COLUMNS if c != "line_id"]
MANDATORY = [
    "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency",
    "ServiceName", "ChargeCategory",
]
# The cost columns, rounded per row; the first two are mandatory, the other two may be absent.
COSTS = ["BilledCost", "EffectiveCost", "ListCost", "ContractedCost"]

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_MONTH = re.compile(r"^(\d{4})-(\d{2})")


def import_focus(inputs: list[Path], out: Path) -> dict:
    """Write `out` from the export files in `inputs` (a directory means its `*.csv` files) and
    return the summary the command prints. `out` is written whole or not at all."""
    files = _expand(inputs)
    rows: list[list[str]] = []
    dropped: set[str] = set()
    months: dict[str, Path] = {}  # charge month -> the file it came from
    slugs: dict[str, Path] = {}
    rows_in = residual_rows = 0
    for path in files:
        slug = slugify(path.stem)
        if slug in slugs:
            raise MarginError(EXIT_USAGE, f"{slugs[slug]} and {path} both number their rows {slug}_<n>: rename one")
        slugs[slug] = path
        parsed = _read_file(path, slug)
        for month in parsed.months:
            if month in months:
                raise MarginError(
                    EXIT_USAGE, f"charge month {month} is in both {months[month]} and {path}: "
                    "one export file per billing period"
                )
            months[month] = path
        dropped |= parsed.dropped
        rows.extend(parsed.rows)
        rows_in += parsed.rows_in
        residual_rows += parsed.rows_out - parsed.rows_in
    _write(out, rows)
    return {
        "rows_in": rows_in,
        "rows_out": len(rows),
        "residual_rows": residual_rows,
        "columns_dropped": {"count": len(dropped), "names": sorted(dropped)},
        "months": sorted(months),
    }


def slugify(stem: str) -> str:
    """Lowercase, every run outside `[a-z0-9]` one `_`, trimmed; `export` when nothing is left."""
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "export"


def _expand(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in inputs:
        if path.is_dir():
            found = sorted(p for p in path.glob("*.csv") if p.is_file())
            if not found:
                raise MarginError(EXIT_USAGE, f"no *.csv file in {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            raise MarginError(EXIT_USAGE, f"no such export file or directory: {path}")
    return sorted(files, key=lambda p: (p.name, str(p)))


class _Parsed:
    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self.rows_in = 0
        self.rows_out = 0
        self.months: list[str] = []
        self.dropped: set[str] = set()


def _read_file(path: Path, slug: str) -> _Parsed:
    parsed = _Parsed()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise MarginError(EXIT_USAGE, f"{path.name} is empty: no header row")
        position = _positions(path, header)
        missing = [c for c in MANDATORY if c not in position]
        if missing:
            raise MarginError(EXIT_USAGE, f"{path.name} has no {missing[0]} column, which the import needs")
        parsed.dropped = {c for c in header if c not in position}
        cost_present = [c for c in COSTS if c in position]
        tags_cache: dict[str, str] = {}

        decoded: list[tuple[str, list[str]]] = []  # month, output cells
        exact: dict[str, dict[str, Decimal]] = {}  # month -> column -> exact sum
        rounded: dict[str, dict[str, Decimal]] = {}  # month -> column -> sum of row cents
        last_of: dict[str, int] = {}  # month -> index of its last row
        for ordinal, cells in enumerate(reader, start=1):
            where = f"{path.name} row {ordinal}"
            if len(cells) != len(header):
                raise MarginError(EXIT_USAGE, f"{where} has {len(cells)} fields, the header has {len(header)}")
            get = lambda name: cells[position[name]] if name in position else ""  # noqa: E731
            month = _month_of(get("ChargePeriodStart"), where)
            out_cells = {name: get(name) for name in PINNED}
            costs: dict[str, Decimal] = {}
            for name in cost_present:
                value = _decimal(get(name), name, where, required=name in MANDATORY)
                if value is not None:
                    costs[name] = value
                    out_cells[name] = _fmt(_round(value, name, where))
            out_cells["Tags"] = _tags(get("Tags"), tags_cache, where)
            out_cells["line_id"] = f"{slug}_{ordinal}"
            sums = exact.setdefault(month, {})
            rsums = rounded.setdefault(month, {})
            with localcontext() as ctx:
                ctx.prec = 60
                for name, value in costs.items():
                    sums[name] = sums.get(name, _ZERO) + value
                    rsums[name] = rsums.get(name, _ZERO) + _round(value, name, where)
            last_of[month] = len(decoded)
            decoded.append((month, [out_cells[c] for c in OUTPUT_COLUMNS]))

    parsed.rows_in = len(decoded)
    parsed.months = sorted(last_of)
    for index, (month, cells) in enumerate(decoded):
        parsed.rows.append(cells)
        if last_of[month] == index:
            residual = _residual_row(month, slug, cost_present, exact[month], rounded[month])
            if residual:
                parsed.rows.append(residual)
    parsed.rows_out = len(parsed.rows)
    return parsed


def _positions(path: Path, header: list[str]) -> dict[str, int]:
    position: dict[str, int] = {}
    for index, name in enumerate(header):
        if name in PINNED:
            if name in position:
                raise MarginError(EXIT_USAGE, f"{path.name} has the column {name} twice")
            position[name] = index
    return position


def _month_of(start: str, where: str) -> str:
    match = _MONTH.match(start)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise MarginError(EXIT_USAGE, f"{where}: ChargePeriodStart {start!r} does not begin with YYYY-MM")
    return f"{match.group(1)}-{match.group(2)}"


def _decimal(text: str, column: str, where: str, *, required: bool) -> Decimal | None:
    text = text.strip()
    if text == "":
        if required:
            raise MarginError(EXIT_USAGE, f"{where}: {column} is empty")
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        raise MarginError(EXIT_USAGE, f"{where}: {column} {text!r} is not a number")
    return value


def _round(value: Decimal, column: str, where: str) -> Decimal:
    try:
        cents = value.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise MarginError(EXIT_USAGE, f"{where}: {column} {value} is too large to round to cents") from None
    return cents if cents else _ZERO  # no negative zero


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def _tags(text: str, cache: dict[str, str], where: str) -> str:
    """The tag object with lowercased keys, compact; a collision of different values is an error."""
    if text in cache:
        return cache[text]
    if text == "":
        tags: dict = {}
    else:
        try:
            tags = json.loads(text)
        except ValueError:
            raise MarginError(EXIT_USAGE, f"{where}: Tags is not JSON: {text!r}") from None
        if not isinstance(tags, dict):
            raise MarginError(EXIT_USAGE, f"{where}: Tags is not a JSON object: {text!r}")
    lowered: dict = {}
    for key, value in tags.items():
        key = key.lower()
        if key in lowered and lowered[key] != value:
            raise MarginError(EXIT_USAGE, f"{where}: tag key {key!r} appears twice with different values")
        lowered[key] = value
    cache[text] = json.dumps(lowered, separators=(",", ":"), ensure_ascii=False)
    return cache[text]


def _residual_row(month: str, slug: str, present: list[str], exact: dict, rounded: dict) -> list[str] | None:
    """The `Adjustment` row that makes the month's rounded cents sum to the cent-rounded exact sum
    of each cost column, or None when every column already ties."""
    residual: dict[str, Decimal] = {}
    for name in present:
        total = _round(exact.get(name, _ZERO), name, f"month {month}")
        residual[name] = total - rounded.get(name, _ZERO)
    if not any(residual.values()):
        return None
    year, mon = int(month[:4]), int(month[5:])
    nxt = f"{year + (mon == 12):04d}-{mon % 12 + 1:02d}"
    cells = {
        "ChargePeriodStart": f"{month}-01T00:00:00Z", "ChargePeriodEnd": f"{nxt}-01T00:00:00Z",
        "BillingCurrency": "USD", "ServiceCategory": "Other", "ServiceName": "Import rounding",
        "ResourceId": "", "RegionId": "", "ChargeCategory": "Adjustment",
        "CommitmentDiscountCategory": "", "CommitmentDiscountType": "",
        "Tags": '{"tenant":"shared"}', "line_id": f"{slug}_residual_{month}",
    }
    cells.update({name: _fmt(residual.get(name, _ZERO)) if name in present else "" for name in COSTS})
    return [cells[c] for c in OUTPUT_COLUMNS]


def _write(out: Path, rows: list[list[str]]) -> None:
    """Write the whole file beside `out` and move it into place, so a failure leaves `out` as it was."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(OUTPUT_COLUMNS)
            writer.writerows(rows)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
