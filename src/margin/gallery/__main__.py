"""`python -m margin.gallery capture [ERROR ...]` rewrites the committed captures;
`python -m margin.gallery build --out DIR` renders the gallery alone."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from ..errors import EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, MarginError
from ..memo import REPOSITORY
from . import ERRORS, build, capture, write_capture


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m margin.gallery", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture", help="re-run the close and rewrite captures/<error>.json")
    cap.add_argument("errors", nargs="*", metavar="ERROR", help=f"default: all of {', '.join(ERRORS)}")
    bld = commands.add_parser("build", help="render the gallery from the committed captures")
    bld.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            for path in build(args.out, REPOSITORY):
                print(path)
            return EXIT_OK
        unknown = [e for e in args.errors if e not in ERRORS]
        if unknown:
            print(f"unknown error {unknown[0]!r}: valid errors are {', '.join(ERRORS)}", file=sys.stderr)
            return EXIT_USAGE
        for error in args.errors or ERRORS:
            with tempfile.TemporaryDirectory(prefix="margin-gallery-") as work:
                print(write_capture(error, capture(error, Path(work))))
    except MarginError as err:
        print(f"error (exit {err.exit_code}): {err.message}", file=sys.stderr)
        return EXIT_INTERNAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
