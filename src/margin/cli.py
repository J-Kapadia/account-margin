"""The `margin` command line: gen, close, rollforward, status, explain, memo, agreement, doctor, import-focus, serve.

Every command takes the path flags and `--json`; an error becomes a failure
envelope and the exit code of the error contract (see errors.py).
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import click

from . import agreement as agreement_mod
from . import check as check_mod
from . import close as close_mod
from . import doctor as doctor_mod
from . import envelope, explain as explain_mod, focus_import as focus_import_mod, gen as gen_mod, gen_config, gen_plan
from . import memo as memo_mod
from . import inject as inject_mod
from . import memo_check as memo_check_mod
from . import rollforward as rollforward_mod
from . import scenario_check as scenario_check_mod
from . import scenario_run as scenario_mod
from . import serve as serve_mod
from . import serve_static as serve_static_mod
from . import signoff as signoff_mod
from . import status as status_mod
from .errors import (
    EXIT_CONTROL, EXIT_INTERNAL, EXIT_OK, EXIT_PRECONDITION, EXIT_USAGE, MarginError,
)
from .lock import warehouse_lock
from .paths import Paths, parse_month, resolve
from .warehouse import connect


_PATH_OPTIONS = {
    "books": "Books directory (MARGIN_BOOKS).",
    "truth": "Truth directory (MARGIN_TRUTH).",
    "warehouse": "Warehouse file (MARGIN_WAREHOUSE).",
    "config": "Config directory (MARGIN_CONFIG_DIR).",
}


def common_options(*used: str):
    """`--json`, `--fixture` and the path flags in `used`: only the paths a command reads."""
    unknown = set(used) - set(_PATH_OPTIONS)
    if unknown:
        raise ValueError(f"unknown path options: {sorted(unknown)}")

    def decorate(fn):
        options = [click.option(f"--{name}", help=_PATH_OPTIONS[name]) for name in _PATH_OPTIONS if name in used]
        options += [
            click.option("--fixture", type=click.Choice(["small", "mini", "scenario"]),
                         help="Fixture preset: sets books, truth and the default warehouse."),
            click.option("--json", "as_json", is_flag=True, help="Machine output on stdout."),
        ]
        for option in reversed(options):
            fn = option(fn)

        @functools.wraps(fn)
        def wrapper(*args, fixture, as_json, **kwargs):
            flags = {name: kwargs.pop(name) for name in used}
            paths = resolve(**flags, fixture=fixture)
            return _guard(as_json, lambda: fn(*args, paths=paths, **kwargs))

        return wrapper

    return decorate


def _guard(as_json: bool, action) -> None:
    """Run `action() -> (envelope fields, human lines)`; map errors to exit codes.

    A command whose result is not an error but still has a non-zero exit (doctor's warnings and
    failures) puts it in the fields as `exit`: the ok envelope carries it and the process exits with it."""
    try:
        fields, lines = action()
        fields = dict(fields)
        code = fields.pop("exit", EXIT_OK)
        env = envelope.ok(**fields)
        env["exit"] = code
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
    if code:
        sys.exit(code)


def _month_option(value: str) -> str:
    try:
        return parse_month(value)
    except MarginError as err:
        raise click.BadParameter(err.message) from None


class _Group(click.Group):
    """A usage error under --json prints the failure envelope on stdout; Click still prints its text on stderr."""

    def invoke(self, ctx: click.Context):
        args = [*ctx.protected_args, *ctx.args]
        wants_json = "--json" in (args[: args.index("--")] if "--" in args else args)
        try:
            return super().invoke(ctx)
        except click.UsageError as err:
            if wants_json:
                envelope.emit_json(envelope.failure(MarginError(EXIT_USAGE, err.format_message())))
            raise


@click.group(cls=_Group)
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
@common_options("books", "truth", "warehouse", "config")
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
@click.option("--recompute", is_flag=True, help="Re-close a closed (not locked) month whose model code "
              "was fixed: store the new result hash and the old one as restated_from.")
@common_options("books", "truth", "warehouse", "config")
def close(paths: Paths, month: str, check: bool, recompute: bool):
    """Compute, control and record a month, closing earlier months first."""
    if check:
        check_mod.require_truth(paths.truth, month=month)  # before the lock and before any write
    with warehouse_lock(paths.lock, exclusive=True):
        result = close_mod.close_month(paths, month, check=check, recompute=recompute)
    fields = {"month": result["month"], "controls": result["controls"], "result_hash": result["result_hash"]}
    if recompute:
        fields["restated_from"] = result["restated_from"]
    return fields, [f"closed {month}" + (" (recomputed)" if recompute else "")
                    + (" (checked against the answer key)" if check else "")]


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to load, YYYY-MM.")
@common_options("books", "truth", "warehouse", "config")
def rollforward(paths: Paths, month: str):
    """Lock the earlier closed months and load a month's actuals, without closing it."""
    with warehouse_lock(paths.lock, exclusive=True):
        result = rollforward_mod.rollforward_month(paths, month)
    locked = ", ".join(result["locked"]) or "none"
    return result, [f"rolled forward to {month}: locked {locked}"]


def _require_warehouse(paths: Paths) -> None:
    """Fail before the reader lock: a missing warehouse must leave no directory or lock file."""
    if not paths.warehouse.is_file():
        raise MarginError(
            EXIT_PRECONDITION, f"no warehouse at {paths.warehouse}: run margin close first"
        )


@main.command()
@common_options("warehouse")
def status(paths: Paths):
    """List the months of the warehouse with their state and result hash."""
    _require_warehouse(paths)
    with warehouse_lock(paths.lock, exclusive=False):
        con = connect(paths.warehouse, read_only=True)
        try:
            months = status_mod.status(con)
        finally:
            con.close()
    return {"months": months}, status_mod.render(months)


@main.command()
@click.argument("number_id")
@click.option("--depth", type=click.IntRange(min=0), default=None,
              help="Walk composite numbers N levels toward their leaves (default: to the leaves).")
@common_options("warehouse")
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
@click.option("--check", is_flag=True, help="After the build, prove the memo traceable: CHK-MEMO-NUM and "
              "CHK-MEMO-QUOTE (exit 3), a digest or value mismatch (exit 6).")
@click.option("--crawl", is_flag=True, help="With --check (implied): walk the internal links of the output "
              "directory offline; CHK-CRAWL (exit 3) on a broken one.")
@common_options("warehouse")
def memo(paths: Paths, month: str, out: Path, check: bool, crawl: bool):
    """Build the one-page memo for a closed month, and with --check prove it traceable."""
    _require_warehouse(paths)
    controls: list[dict] = []
    with warehouse_lock(paths.lock, exclusive=False):
        con = connect(paths.warehouse, read_only=True)
        try:
            explain_mod.require_closed(con, month)
            files = memo_mod.build(con, month, out)
            if check or crawl:
                controls = memo_check_mod.run(con, month, out, crawl)
        finally:
            con.close()
    failed = [c for c in controls if c["status"] == "fail"]
    if failed:
        raise MarginError(
            EXIT_CONTROL, "memo check failed: " + ", ".join(c["id"] for c in failed), month=month,
            controls=[{k: c[k] for k in ("id", "status", "violating_rows")} for c in controls],
            failed_controls=[{k: c[k] for k in ("id", "violating_rows", "detail")} for c in failed])
    fields = {"month": month, "out": str(out), "files": [str(f) for f in files]}
    if controls:
        fields["controls"] = [{k: c[k] for k in ("id", "status", "violating_rows")} for c in controls]
    return fields, [f"wrote {f}" for f in files]


@main.command()
@click.option("--month", required=True, callback=lambda c, p, v: _month_option(v),
              help="Month to check, YYYY-MM (closed or locked, memo built).")
@click.option("--check", is_flag=True, help="Fail CHK-AGREEMENT (exit 3) on any disagreement; "
              "without it the rate is only reported.")
@click.option("--write", is_flag=True, help="Store the result in <site>/<month>/agreement.json.")
@click.option("--site", type=click.Path(path_type=Path), default=Path("site"), show_default=True,
              help="The built site to read the memo and llms.txt from.")
@click.option("--values", "values_path", type=click.Path(path_type=Path), default=None,
              help="Read the cached values from this file instead of the one llms.txt links.")
@common_options("warehouse")
def agreement(paths: Paths, month: str, check: bool, write: bool, site: Path, values_path: Path | None):
    """Ask every number of a month through the CLI, SQL, the memo and the cached values."""
    _require_warehouse(paths)
    with warehouse_lock(paths.lock, exclusive=False):
        con = connect(paths.warehouse, read_only=True)
        try:
            explain_mod.require_closed(con, month)
        finally:
            con.close()
        if not (site / month / "index.html").is_file():
            raise MarginError(EXIT_PRECONDITION, f"memo not built for month {month}: run margin memo",
                              month=month)
        result = agreement_mod.run(paths, month, site, values_path, write,
                                   explain_command=explain, check=check)
    return {"month": month, **result}, [
        f"{month}: {result['ids_checked']} IDs through {', '.join(result['interfaces'])}; "
        f"disagreement rate {result['disagreement_rate']}"
    ]


@main.command()
@click.option("--month", default=None, callback=lambda c, p, v: v and _month_option(v),
              help="Base month, YYYY-MM (closed or locked).")
@click.option("--check", is_flag=True, help="Prove the scenario against the hand-worked reference "
              "(CHK-SCENARIO, exit 3 on any difference); runs on its own generated preset.")
@click.option("--reference", type=click.Path(path_type=Path), default=scenario_check_mod.REFERENCE,
              show_default=True, help="With --check: the directory holding expected.csv and summary.json.")
@common_options("warehouse", "config")
def scenario(paths: Paths, month: str | None, check: bool, reference: Path):
    """Run the token-price scenario for a closed month, or check it against its reference."""
    if check == (month is not None):
        raise MarginError(EXIT_USAGE, "give exactly one of --month and --check")
    if check:
        result = scenario_check_mod.check(paths, reference)
        return result, [f"{c['id']} {c['status']}: {result['accounts']} accounts, "
                        f"computed change {result['computed_change']}" for c in result["checks"]]
    _require_warehouse(paths)
    with warehouse_lock(paths.lock, exclusive=True):
        con = connect(paths.warehouse, read_only=True)
        try:
            explain_mod.require_closed(con, month)
        finally:
            con.close()
        sign = signoff_mod.verify(paths.config, month=month)
        result = scenario_mod.run_month(paths, month, sign)
    return result, [
        f"scenario for {month}: {result['accounts']} accounts, {result['records']} records written"
    ]


@main.command()
@common_options("books", "truth", "warehouse", "config")
def doctor(paths: Paths):
    """Report the health of the install and of the paths: exit 0 healthy, 1 warnings, 2 failures."""
    checks, code = doctor_mod.run(paths)
    return {"exit": code, "checks": checks}, doctor_mod.render(checks)


@main.command("import-focus")
@click.option("--in", "inputs", required=True, multiple=True, type=click.Path(path_type=Path),
              help="FOCUS 1.0 CSV export, or a directory of them; repeatable.")
@click.option("--out", required=True, type=click.Path(path_type=Path),
              help="The books directory's cloud_focus.csv to write, whole.")
@click.option("--json", "as_json", is_flag=True, help="Machine output on stdout.")
def import_focus(inputs: tuple[Path, ...], out: Path, as_json: bool):
    """Map AWS Data Exports FOCUS 1.0 CSV files to cloud_focus.csv; reads no warehouse, takes no lock."""
    def run():
        summary = focus_import_mod.import_focus(list(inputs), out)
        dropped = summary["columns_dropped"]["count"]
        return summary, [f"wrote {summary['rows_out']} rows ({summary['residual_rows']} rounding rows) to {out}; "
                         f"{dropped} columns dropped"]

    return _guard(as_json, run)


@main.command()
@click.option("--error", "error", required=True, type=click.Choice(inject_mod.ERRORS),
              help="The error to inject into 2026-03.")
@click.option("--out", required=True, type=click.Path(path_type=Path), help="New books directory to write.")
@click.option("--seed", type=int, default=gen_mod.DEFAULT_SEED, show_default=True,
              help="Seed of the row choice, recorded in the copy's manifest header.")
@common_options("books")
def inject(paths: Paths, error: str, out: Path, seed: int):
    """Copy the books with one seeded error, to test that a control names it."""
    result = inject_mod.inject(paths.books, out, error, seed)
    return result, [f"wrote {out}: {error} in {result['target']['file']} ({result['target']['key']})"]


@main.command()
@click.option("--signer", required=True, help="Name of the signer.")
@click.option("--role", required=True, help="Role of the signer, for example finance_owner.")
@common_options("config")
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


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Address to listen on.")
@click.option("--port", type=click.IntRange(0, 65535), default=None,
              help="Port to listen on (PORT, else 8000).")
@click.option("--static", "static_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Render the page once into this directory and exit, instead of serving.")
@click.option("--json", "as_json", is_flag=True, help="Machine output on stdout.")
def serve(host: str, port: int | None, static_dir: Path | None, as_json: bool):
    """Serve one page over the pre-closed mini books (MARGIN_SERVE_ROOT): run the close, plant an error."""
    def run():
        root = serve_mod.serve_root()
        if static_dir is not None:
            serve_mod.require_built(root)
            result = serve_static_mod.export(root, static_dir)
            return result, [f"wrote {static_dir}"]
        number = serve_mod.resolve_port(port)
        serve_mod.require_built(root)
        serve_mod.run(host, number, root)
        return {}, []

    return _guard(as_json, run)
