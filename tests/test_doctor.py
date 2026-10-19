"""`margin doctor` on the committed `mini` workspace: exit 0 healthy, 1 warnings, 2 failures."""

from __future__ import annotations

import json
import shutil
import signal

import jsonschema
import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, ROOT, margin
from test_lock import hold_exclusive

ENVELOPE = jsonschema.Draft202012Validator(
    json.loads((ROOT / "src/margin/schemas/envelope.schema.json").read_text())
)


def doctor(warehouse, *extra, env=None):
    return margin("doctor", "--fixture", "mini", "--config", str(MINI_CONFIG), "--warehouse", str(warehouse),
                  "--json", *extra, env=env)


def statuses(result) -> dict[str, str]:
    return {c["id"]: c["status"] for c in result.json["checks"]}


def detail(result, check_id) -> str:
    return next(c["detail"] for c in result.json["checks"] if c["id"] == check_id)


def test_a_closed_workspace_is_healthy_and_the_envelope_validates(mini_closed):
    result = doctor(mini_closed)
    assert result.code == 0, result.stdout
    ENVELOPE.validate(result.json)
    assert result.json["status"] == "ok" and result.json["exit"] == 0
    assert statuses(result) == dict.fromkeys(
        ("python", "dependencies", "books", "signoff", "warehouse", "lock", "truth"), "ok")
    assert all(c["detail"] for c in result.json["checks"])


def test_human_output_has_one_line_per_check(mini_closed):
    result = margin("doctor", "--fixture", "mini", "--config", str(MINI_CONFIG), "--warehouse", str(mini_closed))
    assert result.code == 0, result.stderr
    assert [line.split()[1] for line in result.stdout.splitlines()] == [
        "python:", "dependencies:", "books:", "signoff:", "warehouse:", "lock:", "truth:"]


def test_an_unsigned_config_is_a_failure(mini_closed, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = doctor(mini_closed, env={"MARGIN_SIGNOFF_DIR": str(empty)})
    assert result.code == 2
    ENVELOPE.validate(result.json)
    assert result.json["status"] == "ok" and result.json["exit"] == 2
    assert statuses(result)["signoff"] == "fail"
    assert "MARGIN_SIGNOFF_DIR" in detail(result, "signoff")
    assert {k: v for k, v in statuses(result).items() if k != "signoff"} == dict.fromkeys(
        ("python", "dependencies", "books", "warehouse", "lock", "truth"), "ok")


def test_a_missing_warehouse_is_a_warning_and_creates_nothing(tmp_path):
    warehouse = tmp_path / "nope" / "w.duckdb"
    result = doctor(warehouse)
    assert result.code == 1, result.stdout
    ENVELOPE.validate(result.json)
    assert statuses(result)["warehouse"] == "warn"
    assert statuses(result)["lock"] == "ok"
    assert not warehouse.parent.exists()


def test_a_missing_truth_directory_is_a_warning(mini_closed, tmp_path):
    result = doctor(mini_closed, "--truth", str(tmp_path / "no-truth"))
    assert result.code == 1, result.stdout
    assert statuses(result)["truth"] == "warn"


def test_a_warehouse_that_does_not_open_is_a_failure(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    warehouse.write_bytes(b"this is not a database file" * 100)
    result = doctor(warehouse)
    assert result.code == 2, result.stdout
    assert statuses(result)["warehouse"] == "fail"


def test_a_live_exclusive_holder_is_a_warning_not_exit_70(mini_closed_copy):
    holder = hold_exclusive(str(mini_closed_copy) + ".lock")
    try:
        result = doctor(mini_closed_copy)
    finally:
        holder.kill()
        holder.wait()
    assert result.code == 1, result.stdout
    ENVELOPE.validate(result.json)
    assert statuses(result)["lock"] == "warn"
    assert "held" in detail(result, "lock")


def test_a_lock_file_left_by_a_killed_holder_is_free(mini_closed_copy):
    holder = hold_exclusive(str(mini_closed_copy) + ".lock")
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    assert (mini_closed_copy.parent / (mini_closed_copy.name + ".lock")).exists()
    result = doctor(mini_closed_copy)
    assert result.code == 0, result.stdout
    assert statuses(result)["lock"] == "ok"
    assert "free" in detail(result, "lock")


@pytest.fixture
def books_copy(tmp_path):
    shutil.copytree(MINI_BOOKS, tmp_path / "books")
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", tmp_path / "books.manifest.sha256")
    return tmp_path / "books"


def test_a_manifest_with_one_hash_changed_is_a_failure(mini_closed, books_copy):
    manifest = books_copy.parent / "books.manifest.sha256"
    lines = manifest.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.endswith("  gl.csv"))
    lines[index] = ("0" if lines[index][0] != "0" else "1") + lines[index][1:]
    manifest.write_text("\n".join(lines) + "\n")
    result = doctor(mini_closed, "--books", str(books_copy))
    assert result.code == 2, result.stdout
    assert statuses(result)["books"] == "fail"
    assert "gl.csv" in detail(result, "books")


def test_a_missing_manifest_is_a_failure(mini_closed, books_copy):
    (books_copy.parent / "books.manifest.sha256").unlink()
    result = doctor(mini_closed, "--books", str(books_copy))
    assert result.code == 2, result.stdout
    assert statuses(result)["books"] == "fail"


@pytest.mark.parametrize("damage", ["missing_window", "bad_window", "extra_file"])
def test_inconsistent_books_are_a_failure(mini_closed, books_copy, damage):
    if damage == "missing_window":
        (books_copy / "window.json").unlink()
    elif damage == "bad_window":
        (books_copy / "window.json").write_text('{"first": "2026-04", "last": "2026-02"}')
    else:
        (books_copy / "stray.csv").write_text("x\n")
    result = doctor(mini_closed, "--books", str(books_copy))
    assert result.code == 2, result.stdout
    assert statuses(result)["books"] == "fail"
