import importlib.util
from pathlib import Path


def test_duckdb_probe(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "duckdb_probe", Path(__file__).parent / "probes" / "duckdb_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.probe(tmp_path / "probe.duckdb")
