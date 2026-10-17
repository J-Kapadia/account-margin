"""DuckDB probe: the three facts the locking design rests on.

Run directly (`python tests/probes/duckdb_probe.py`) or through tests/test_probe.py.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from margin.warehouse import connect

HOLD = "import sys; from pathlib import Path; from margin.warehouse import connect; c = connect(Path(sys.argv[1]), read_only=True); print('ready', flush=True); sys.stdin.readline()"
WRITE = "import sys; from pathlib import Path; from margin.warehouse import connect; connect(Path(sys.argv[1])).execute('INSERT INTO t VALUES (2)')"


def probe(path: Path) -> None:
    con = connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (1)")
    # 1. A second connection does not see uncommitted rows.
    assert connect(path).execute("SELECT count(*) FROM t").fetchone()[0] == 0
    # 2. A subprocess cannot write while this process holds the file.
    other = subprocess.run([sys.executable, "-c", WRITE, str(path)], capture_output=True, text=True)
    assert other.returncode != 0 and "lock" in other.stderr.lower(), other.stderr
    con.execute("COMMIT")
    con.close()
    # 3. Two read-only processes can hold the file open together.
    readers = [
        subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        assert all(r.stdout.readline().strip() == "ready" for r in readers)
    finally:
        for r in readers:
            r.communicate("\n")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        probe(Path(tmp) / "probe.duckdb")
    print("duckdb probe: ok")
