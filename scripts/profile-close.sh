#!/usr/bin/env bash
# Where a second close spends its time: closes the committed `mini` fixture twice in a temporary
# warehouse (2026-04, then 2026-03, on the fixture config and its sign-off) and prints one table
# with, per close, the wall time and its phases. It measures; it has no thresholds and never fails
# on a slow run.
#
#   scripts/profile-close.sh [--json]
#
# Phases, in seconds: `dbt import` is the interpreter's import of dbt inside the dbt subprocess,
# `dbt parse` is dbt's own log from its first line to the end of the project parse (adapter
# registration included, partial parse when the first close left a manifest), `dbt run` is its log
# from the thread count line to the summary, `dbt other` is the rest of the dbt subprocess, and
# `outside dbt` is the rest of the close. The five phases sum to the wall time.
#
# The dbt subprocess is the one `margin close` starts, with two changes that exist only to measure
# it: its file log is raised from warn to info, so the log carries the parse and run markers, and
# PYTHONPROFILEIMPORTTIME=1 makes the interpreter report the import. Both cost a little.
set -euo pipefail
cd "$(dirname "$0")/.."

json=0
case "${1:-}" in
  "") ;;
  --json) json=1 ;;
  *) echo "usage: profile-close.sh [--json]" >&2; exit 2 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

uv run python - "$work" "$json" <<'PY'
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

work, as_json = Path(sys.argv[1]), sys.argv[2] == "1"
warehouse = work / "w.duckdb"
CLOSES = [("first close", "2026-04"), ("second close", "2026-03")]

# Runs `margin close` in a fresh interpreter, as the command line does, timing the dbt subprocess
# and reading the interpreter's import report from its stderr.
CHILD = r"""
import json, os, subprocess, sys, time
from margin import dbt_runner
from margin.cli import main

spent = {"dbt_subprocess": 0.0, "dbt_import": 0.0}
real_run = subprocess.run

def run(cmd, *args, **kwargs):
    if not (isinstance(cmd, list) and cmd[1:3] == ["-c", dbt_runner._DBT_MAIN]):
        return real_run(cmd, *args, **kwargs)
    i = cmd.index("--log-level-file")
    cmd = cmd[: i + 1] + ["info"] + cmd[i + 2:]
    kwargs["env"] = dict(kwargs["env"], PYTHONPROFILEIMPORTTIME="1")
    start = time.perf_counter()
    proc = real_run(cmd, *args, **kwargs)
    spent["dbt_subprocess"] += time.perf_counter() - start
    # The module is reported once per nesting level; the outermost report is the largest.
    for line in proc.stderr.splitlines():
        parts = [p.strip() for p in line.removeprefix("import time:").split("|")]
        if len(parts) == 3 and parts[2] == "dbt.cli.main":
            spent["dbt_import"] = max(spent["dbt_import"], int(parts[1]) / 1e6)
    return proc

subprocess.run = run
code = 0
try:
    main()
except SystemExit as exc:
    code = exc.code or 0
except BaseException as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    code = 1
with open(os.environ["PROFILE_CLOSE_OUT"], "w") as out:
    json.dump(spent, out)
sys.exit(code)
"""


def clock(line: str) -> float:
    t = datetime.strptime(line[:15], "%H:%M:%S.%f")
    return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6


def between(lines: list[str], first: str, last: str) -> float:
    """Seconds from the first log line holding `first` to the first one after it holding `last`."""
    stamped = [ln for ln in lines if re.match(r"\d\d:\d\d:\d\d\.\d{6} ", ln)]
    start = next(i for i, ln in enumerate(stamped) if first in ln)
    end = next(i for i in range(start, len(stamped)) if last in stamped[i])
    span = clock(stamped[end]) - clock(stamped[start])
    return span + 86400 if span < 0 else span  # a run that crosses midnight


env = {k: v for k, v in os.environ.items() if not k.startswith(("MARGIN_", "DBT_"))}
env["MARGIN_SIGNOFF_DIR"] = "tests/fixtures/signoff"
log = Path(str(warehouse) + ".dbt") / "logs" / "dbt.log"

rows = []
for label, month in CLOSES:
    seen = log.stat().st_size if log.exists() else 0
    out = work / f"{month}.json"
    env["PROFILE_CLOSE_OUT"] = str(out)
    argv = ["close", "--fixture", "mini", "--month", month, "--config", "tests/fixtures/config",
            "--warehouse", str(warehouse), "--json"]
    start = time.perf_counter()
    proc = subprocess.run([sys.executable, "-c", CHILD, *argv], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    wall = time.perf_counter() - start
    if proc.returncode != 0:
        sys.exit(f"profile-close: the {label} ({month}) exited {proc.returncode}:\n"
                 + "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:]))
    spent = json.loads(out.read_text())
    with open(log, "rb") as fh:
        fh.seek(seen)
        lines = fh.read().decode().splitlines()
    parse = between(lines, "Running with dbt=", "Found ")
    run = between(lines, "Concurrency:", "Done.")
    sub, imp = spent["dbt_subprocess"], spent["dbt_import"]
    rows.append({
        "close": label, "month": month, "wall": wall, "dbt_subprocess": sub,
        "dbt_import": imp, "dbt_parse": parse, "dbt_run": run,
        "dbt_other": sub - imp - parse - run, "outside_dbt": wall - sub,
    })

PHASES = [("wall", "wall"), ("dbt subprocess", "dbt_subprocess"), ("dbt import", "dbt_import"),
          ("dbt parse", "dbt_parse"), ("dbt run", "dbt_run"), ("dbt other", "dbt_other"),
          ("outside dbt", "outside_dbt")]
NOTE = ("wall = dbt import + dbt parse + dbt run + dbt other + outside dbt; "
        "dbt subprocess = the first four. dbt's file log runs at info for this measurement.")

if as_json:
    print(json.dumps({
        "closes": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
        "note": NOTE,
    }, indent=2))
else:
    heads = [f"{r['close']} ({r['month']})" for r in rows]
    width = max(len(name) for name, _ in PHASES + [("phase", "")])
    print("  ".join(["phase".ljust(width)] + [h.rjust(len(h)) for h in heads]))
    for name, key in PHASES:
        print("  ".join([name.ljust(width)] + [f"{r[key]:.1f}".rjust(len(h)) for r, h in zip(rows, heads)]))
    print(NOTE)
PY
