"""The `margin` command line: gen, close, explain, memo.

Every command takes the path flags and `--json`; an error becomes a failure
envelope and the exit code of the error contract (see errors.py).
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import click

from . import check as check_mod
from . import close as close_mod
from . import envelope, explain as explain_mod, gen as gen_mod, gen_config, gen_plan, load, memo as memo_mod
from . import inject as inject_mod
from . import signoff as signoff_mod
from .errors import (
    EXIT_INTERNAL, EXIT_PRECONDITION, EXIT_USAGE, MarginError,
)
from .lock import warehouse_lock
from .paths import Paths, parse_month, resolve
from .warehouse import connect


def common_options(fn):
    """The global flags, accepted by every command."""
    options = [
        click.option("--books", help="Books directory (MARGIN_BOOKS)."),
        click.option("--truth", help="Truth directory (MARGIN_TRUTH)."),
        click.option("--warehouse", help="Warehouse file (MARGIN_WAREHOUSE)."),
        click.option("--config", help="Config directory (MARGIN_CONFIG_DIR)."),
        click.option("--fixture", type=click.Choice(["small", "mini", "scenario"]),
                     help="Fixture preset: sets books, truth and the default warehouse."),
        click.option("--json", "as_json", is_flag=True, help="Machine output on stdout."),
    ]
    for option in reversed(options):
        fn = option(fn)

    @functools.wraps(fn)
    def wrapper(*args, books, truth, warehouse, config, fixture, as_json, **kwargs):
        paths = resolve(books=books, truth=truth, warehouse=warehouse, config=config, fixture=fixture)
        return _guard(as_json, lambda: fn(*args, paths=paths, **kwargs))

    return wrapper


def _guard(as_json: bool, action) -> None:
    """Run `action() -> (envelope fields, human lines)`; map errors to exit codes."""
    try:
        fields, lines = action()
        env = envelope.ok(**fields)
    except MarginError as err:
        envelope.emit(envelope.failure(err), as_json=as_json)
        sys.exit(err.exit_code)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - last line of defence: exit 70, never a traceback
        err = MarginError(EXIT_INTERNAL, f"internal error: {type(exc).__name__}: {exc}")
        envelope.emit(envelope.failure(err), as_json=as_json)
        sys.exit(err.exit_code)
    envelope.emit(env, as_json=as_json, extra_lines=lines)


def _month_option(value: str) -> str:
    try:
        return parse_month(value)
    except MarginError as err:
        raise click.BadParameter(err.message) from None


@click.group()
@click.version_option(package_name="account-margin")
def main() -> None:
    """Contribution profit by customer account, with controls."""


@main.command()
@click.option("--seed", type=int, default=gen_mod.DEFAULT_SEED, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), help="Books directory to write.")
@click.option("--accounts", type=click.IntRange(min=1), default=None, help="Account count (300).")
@click.option("--months", type=click.IntRange(min=1), default=None, help="Month count (12).")
@click.option("--first-month", default=None, help="First month, YYYY-MM (2025-10).")
@click.option("--prices", "prices_path", type=click.Path(path_type=Path), default=None,
              help="Model price list (MARGIN_PRICES; the fixture's with --fixture mini).")
@click.option("--verify", is_flag=True, help="Check the manifest, the config hashes and a regeneration.")
@common_options
def gen(paths: Paths, seed: int, out: Path | None, accounts: int | None, months: int | None,
        first_month: str | None, prices_path: Path | None, verify: bool):
    """Generate the books and the answer key, or verify them against the manifest."""
    books = out or paths.books
    truth = paths.truth if out is None or paths.truth_explicit else out.parent / "truth"
    if paths.fixture == "small":
        if verify:
            raise MarginError(EXIT_USAGE, "--verify does not apply to --fixture small")
        with warehouse_lock(paths.lock, exclusive=True):
            summary = gen_mod.generate_small(books, seed)
        return summary, [f"wrote {summary['accounts']} accounts, window "
                         f"{summary['window']['first']}..{summary['window']['last']}, to {books}"]
    preset = paths.fixture
    if preset and (accounts or months or first_month):
        raise MarginError(
            EXIT_USAGE, f"--fixture {preset} fixes the account count and the window: "
            "drop --accounts, --months and --first-month"
        )
    if first_month:
        first_month = parse_month(first_month)
    config_dir = paths.config
    prices_path = prices_path or paths.prices
    if verify:
        with warehouse_lock(paths.lock, exclusive=False):
            summary = gen_mod.verify(books=books, truth=truth, config_dir=config_dir, prices_path=prices_path)
        return summary, [f"books match the manifest ({summary['files']} files)"]
    config = gen_config.load_config(config_dir)
    prices = gen_config.load_prices(prices_path)
    preset_args = gen_plan.PRESETS.get(preset or "", {})
    with warehouse_lock(paths.lock, exclusive=True):
        summary = gen_mod.generate(
            books=books, truth=truth, config=config, prices=prices, seed=seed, preset=preset,
            n_accounts=preset_args.get("accounts", accounts or gen_plan.DEFAULT_ACCOUNTS),
            n_months=preset_args.get("months", months or gen_plan.DEFAULT_MONTHS),
            first_month=preset_args.get("first_month", first_month or gen_plan.DEFAULT_FIRST_MONTH),
        )
    return summary, [f"wrote {summary['accounts']} accounts, window "
                     f"{summary['window']['first']}..{summary['window']['last']}, to {books}"]


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to close, YYYY-MM.")
@click.option("--check", is_flag=True, help="Compare with the answer key (truth/).")
@common_options
def close(paths: Paths, month: str, check: bool):
    """Compute, control and record a month, closing earlier months first."""
    if check:
        check_mod.require_truth(paths.truth, month=month)  # before the lock and before any write
    with warehouse_lock(paths.lock, exclusive=True):
        result = close_mod.close_month(paths, month, check=check)
    return {"month": result["month"], "controls": result["controls"],
            "result_hash": result["result_hash"]}, [f"closed {month}" + (" (checked against the answer key)" if check else "")]


def _require_warehouse(paths: Paths) -> None:
    """Fail before the reader lock: a missing warehouse must leave no directory or lock file."""
    if not paths.warehouse.is_file():
        raise MarginError(
            EXIT_PRECONDITION, f"no warehouse at {paths.warehouse}: run margin close first"
        )


@main.command()
@click.argument("number_id")
@click.option("--depth", type=click.IntRange(min=0), default=None,
              help="Walk composite numbers N levels toward their leaves (default: to the leaves).")
@common_options
def explain(paths: Paths, number_id: str, depth: int | None):
    """Print the provenance chain of NUMBER_ID, ending at source rows."""
    _require_warehouse(paths)
    with warehouse_lock(paths.lock, exclusive=False):
        con = connect(paths.warehouse, read_only=True)
        try:
            node = explain_mod.explain(con, number_id, depth)
        finally:
            con.close()
    fields = {"month": node["month"]} if node["month"] else {}  # a month-less ID has none
    return {**fields, "number_id": number_id, "digest_verified": node["digest_verified"],
            "chain": node}, explain_mod.render(node)


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to publish, YYYY-MM.")
@click.option("--out", type=click.Path(path_type=Path), default=Path("site"), show_default=True)
@common_options
def memo(paths: Paths, month: str, out: Path):
    """Build the one-page memo for a closed month."""
    _require_warehouse(paths)
    with warehouse_lock(paths.lock, exclusive=False):
        con = connect(paths.warehouse, read_only=True)
        try:
            explain_mod.require_closed(con, month)
            files = memo_mod.build(con, month, out)
        finally:
            con.close()
    return {"month": month, "out": str(out), "files": [str(f) for f in files]}, [
        f"wrote {f}" for f in files
    ]


@main.command()
@click.option("--error", "error", required=True, type=click.Choice(inject_mod.ERRORS),
              help="The error to inject into 2026-03.")
@click.option("--out", required=True, type=click.Path(path_type=Path), help="New books directory to write.")
@click.option("--seed", type=int, default=gen_mod.DEFAULT_SEED, show_default=True,
              help="Seed of the row choice, recorded in the copy's manifest header.")
@common_options
def inject(paths: Paths, error: str, out: Path, seed: int):
    """Copy the books with one seeded error, to test that a control names it."""
    result = inject_mod.inject(paths.books, out, error, seed)
    return result, [f"wrote {out}: {error} in {result['target']['file']} ({result['target']['key']})"]


@main.command()
@click.option("--signer", required=True, help="Name of the signer.")
@click.option("--role", required=True, help="Role of the signer, for example finance_owner.")
@common_options
def signoff(paths: Paths, signer: str, role: str):
    """Sign the allocation and policy configs. Human-only: needs a terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise MarginError(
            EXIT_PRECONDITION,
            "signoff needs a terminal: a person signs, a script or an agent does not",
        )
    path = signoff_mod.write_for(paths.config, signer=signer, role=role)
    rule, policy = signoff_mod.config_hashes(paths.config)
    return {"signoff": str(path), "rule_sha256": rule, "policy_sha256": policy}, [f"wrote {path}"]
