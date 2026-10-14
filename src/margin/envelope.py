"""The versioned JSON envelope and the human view of it."""

from __future__ import annotations

import json
import sys

from . import SCHEMA_VERSION
from .errors import EXIT_OK, MarginError


def ok(**fields) -> dict:
    return {"schema_version": SCHEMA_VERSION, "status": "ok", "exit": EXIT_OK, **fields}


def failure(err: MarginError) -> dict:
    env: dict = {"schema_version": SCHEMA_VERSION, "status": "fail", "exit": err.exit_code}
    if err.month:
        env["month"] = err.month
    if err.controls is not None:
        env["controls"] = err.controls
    if err.failed_controls is not None:
        env["failed_controls"] = err.failed_controls
    env["message"] = err.message
    return env


def human(env: dict, extra_lines: list[str] | None = None) -> str:
    """One line per control, failures first, then the message or the extra lines."""
    lines = []
    controls = sorted(env.get("controls", []), key=lambda c: (c["status"] != "fail", c["id"]))
    for c in controls:
        lines.append(f"{c['status'].upper():4} {c['id']} ({c['violating_rows']} violating rows)")
    for fc in env.get("failed_controls", []):
        lines.extend(f"     {d}" for d in fc.get("detail", []))
    if env["status"] == "fail":
        lines.append(f"error (exit {env['exit']}): {env['message']}")
    else:
        lines.extend(extra_lines or [])
    return "\n".join(lines)


def emit(env: dict, *, as_json: bool, extra_lines: list[str] | None = None) -> None:
    """Machine output on stdout and human text on stderr with --json; human text on stdout otherwise."""
    if as_json:
        sys.stdout.write(json.dumps(env, sort_keys=False) + "\n")
        text = human(env, extra_lines)
        if text:
            sys.stderr.write(text + "\n")
    else:
        text = human(env, extra_lines)
        if text:
            (sys.stderr if env["status"] == "fail" else sys.stdout).write(text + "\n")
