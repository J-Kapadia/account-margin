"""The `margin` command line (plan §14.1): gen, close, explain, memo.

Every command takes the path flags and `--json`; an error becomes a failure
envelope and the exit code of plan §14.2.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import click

from . import close as close_mod
from . import envelope, explain as explain_mod, gen as gen_mod, load, memo as memo_mod
from .errors import (
    EXIT_INTERNAL, EXIT_PRECONDITION, EXIT_USAGE, MarginError,
)
from .lock import warehouse_lock
from .paths import Paths, parse_month, resolve
from .warehouse import connect


def common_options(fn):
    """The global flags of plan §14.1, accepted by every command."""
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
@common_options
def gen(paths: Paths, seed: int, out: Path | None):
    """Generate the books (Phase 0: --fixture small only)."""
    if paths.fixture != "small":
        raise MarginError(
            EXIT_USAGE, "only --fixture small is available yet: the full books and the "
            "other presets land with the generator proper"
        )
    target = out or paths.books
    with warehouse_lock(paths.lock, exclusive=True):
        summary = gen_mod.generate_small(target, seed)
    return summary, [f"wrote {summary['accounts']} accounts, window "
                     f"{summary['window']['first']}..{summary['window']['last']}, to {target}"]


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to close, YYYY-MM.")
@click.option("--check", is_flag=True, help="Compare with the answer key (truth/).")
@common_options
def close(paths: Paths, month: str, check: bool):
    """Compute, control and record a month, closing earlier months first."""
    if check:
        if not paths.truth.is_dir():
            raise MarginError(
                EXIT_PRECONDITION, "truth/ not found: --check needs the answer key", month=month
            )
        raise MarginError(EXIT_USAGE, "--check is not available yet", month=month)
    with warehouse_lock(paths.lock, exclusive=True):
        result = close_mod.close_month(paths, month)
    return {"month": result["month"], "controls": result["controls"],
            "result_hash": result["result_hash"]}, [f"closed {month}"]


def _open_reader(paths: Paths):
    if not paths.warehouse.is_file():
        raise MarginError(
            EXIT_PRECONDITION, f"no warehouse at {paths.warehouse}: run margin close first"
        )
    return connect(paths.warehouse, read_only=True)


@main.command()
@click.argument("number_id")
@click.option("--depth", type=click.IntRange(min=0), default=None,
              help="Walk composite numbers N levels toward their leaves (default: to the leaves).")
@common_options
def explain(paths: Paths, number_id: str, depth: int | None):
    """Print the provenance chain of NUMBER_ID, ending at source rows."""
    with warehouse_lock(paths.lock, exclusive=False):
        con = _open_reader(paths)
        try:
            node = explain_mod.explain(con, number_id, depth)
        finally:
            con.close()
    return {"month": node["month"], "number_id": number_id, "chain": node}, explain_mod.render(node)


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to publish, YYYY-MM.")
@click.option("--out", type=click.Path(path_type=Path), default=Path("site"), show_default=True)
@common_options
def memo(paths: Paths, month: str, out: Path):
    """Build the one-page memo for a closed month."""
    with warehouse_lock(paths.lock, exclusive=False):
        con = _open_reader(paths)
        try:
            explain_mod.require_closed(con, month)
            files = memo_mod.build(con, month, out)
        finally:
            con.close()
    return {"month": month, "out": str(out), "files": [str(f) for f in files]}, [
        f"wrote {f}" for f in files
    ]
