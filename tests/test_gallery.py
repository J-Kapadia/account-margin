"""The planted-errors gallery: the renderer, the registry, the capture tool and the memo hook."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MINI_CONFIG, ROOT, margin, shared_dir
from margin import gallery, inject, memo_check
from margin.errors import MarginError

MONTH = "2026-03"
REPOSITORY = "https://example.invalid/repo"
DETAIL = ["tied out: GL 4100 differs by 5", "<script>alert(1)</script> & more"]


def synthetic(error: str = "sign_flip", control: str = "CTL-SIGN") -> dict:
    return {
        "error": error, "title": f"Title of {error}", "control": control,
        "planted": f"Planted {error}.", "caught": f"Caught by {control}.",
        "remediation": [f"first step for {error}", "second <b>step</b>"],
        "capture": {
            "error": error, "seed": 7, "month": MONTH, "books": "tests/fixtures/books",
            "target": {"file": "invoices.csv", "key": "line_9", "ordinal": 12, "account_id": "acct_0003"},
            "close": {
                "exit": 3, "message": f"{control} failed",
                "controls": [{"id": control, "status": "fail", "violating_rows": 2},
                             {"id": "CTL-PRICE-PIN", "status": "pass", "violating_rows": 0}],
                "failed_controls": [{"id": control, "violating_rows": 2, "detail": DETAIL}],
            },
        },
    }


def test_render_case_shows_the_plant_the_close_and_the_fix():
    html = gallery.render_case(synthetic(), REPOSITORY)
    assert "<title>Title of sign_flip</title>" in html
    assert '<a href="index.html">All planted errors</a>' in html and '<a href="../index.html">All memos</a>' in html
    for section in ("planted", "close", "caught", "remediation"):
        assert f'<section id="{section}">' in html
    pre = re.search(r'<pre data-control="CTL-SIGN">(.*?)</pre>', html, re.S)
    assert pre
    assert "tied out: GL 4100 differs by 5" in pre.group(1)
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in pre.group(1)
    assert "<script>" not in html
    assert f'<a href="{REPOSITORY}/blob/main/controls/CTL-SIGN.sql">CTL-SIGN</a>' in html
    assert "<li>first step for sign_flip</li>" in html and "<li>second &lt;b&gt;step&lt;/b&gt;</li>" in html
    assert 'data-field="exit">3<' in html and "CTL-SIGN failed" in html
    assert "<td>CTL-PRICE-PIN</td><td>pass</td><td>0</td>" in html
    assert "acct_0003" in html and "line_9" in html and "seed 7" in html
    assert "tests/test_gallery_sign_flip.py" in html


def test_render_case_names_the_gl_code_when_the_target_has_one():
    case = synthetic("rounding_slip", "CTL-TIE-INF")
    case["capture"]["target"] = {"file": "gl.csv", "key": "gl_4", "ordinal": 3, "gl_code": "5100"}
    html = gallery.render_case(case, REPOSITORY)
    assert "5100" in html and "Account" not in html


def test_render_index_links_every_case_and_the_memos():
    html = gallery.render_index([synthetic("sign_flip", "CTL-SIGN"), synthetic("cutoff_slip", "CTL-CUTOFF")], REPOSITORY)
    assert "<title>Planted errors</title>" in html
    assert '<ul id="cases">' in html
    assert '<li><a href="sign_flip.html">Title of sign_flip</a>: CTL-SIGN</li>' in html
    assert '<li><a href="cutoff_slip.html">Title of cutoff_slip</a>: CTL-CUTOFF</li>' in html
    assert '<a href="../index.html">All memos</a>' in html


def test_render_index_with_no_case_says_nothing_is_captured_yet():
    html = gallery.render_index([], REPOSITORY)
    assert "No error has been captured yet." in html and "cases" not in html
    assert '<a href="../index.html">All memos</a>' in html


def test_an_error_without_a_module_has_no_case():
    assert gallery.case("no_such_error") is None


def test_cases_keep_the_order_of_the_injection_command_and_carry_every_field():
    found = gallery.cases()
    names = [c["error"] for c in found]
    assert names == [e for e in inject.ERRORS if e in names]
    for c in found:
        assert set(c) == {"error", "title", "control", "planted", "caught", "remediation", "capture"}
        assert all(c[f] for f in c) and isinstance(c["remediation"], list)


def test_a_module_missing_a_field_or_a_mismatched_capture_is_an_internal_error(tmp_path, monkeypatch):
    package = tmp_path / "fake"
    package.mkdir()
    (package / "sign_flip.py").write_text("ERROR = 'sign_flip'\n")
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / "sign_flip.json").write_text(json.dumps({"error": "sign_flip"}))
    monkeypatch.setattr(gallery, "__path__", [str(package)])
    monkeypatch.setattr(gallery, "CAPTURES", captures)
    monkeypatch.delitem(sys.modules, "margin.gallery.sign_flip", raising=False)
    with pytest.raises(MarginError) as err:
        gallery.case("sign_flip")
    assert err.value.exit_code == 70 and "sign_flip.py" in err.value.message and "TITLE" in err.value.message
    fields = "".join(f"{f} = 'x'\n" for f in gallery.MODULE_FIELDS if f != "ERROR")
    (package / "sign_flip.py").write_text(f"ERROR = 'other'\n{fields}")
    monkeypatch.delitem(sys.modules, "margin.gallery.sign_flip", raising=False)
    with pytest.raises(MarginError) as err:
        gallery.case("sign_flip")
    assert err.value.exit_code == 70 and "sign_flip.json" in err.value.message
    monkeypatch.delitem(sys.modules, "margin.gallery.sign_flip", raising=False)


def test_build_writes_the_index_even_with_no_case(tmp_path, monkeypatch):
    monkeypatch.setattr(gallery, "cases", lambda: [])
    written = gallery.build(tmp_path, REPOSITORY)
    assert written == [tmp_path / "errors" / "index.html"]
    assert "No error has been captured yet." in written[0].read_text()


def test_build_writes_a_page_per_case(tmp_path, monkeypatch):
    monkeypatch.setattr(gallery, "cases", lambda: [synthetic("sign_flip"), synthetic("cutoff_slip", "CTL-CUTOFF")])
    written = gallery.build(tmp_path, REPOSITORY)
    assert sorted(p.name for p in written) == ["cutoff_slip.html", "index.html", "sign_flip.html"]
    (tmp_path / "index.html").write_text("<html></html>")
    assert memo_check.crawl(tmp_path) == []


def test_the_main_module_builds_the_gallery_and_refuses_an_unknown_error(tmp_path):
    run = lambda *args: subprocess.run([sys.executable, "-m", "margin.gallery", *args], cwd=ROOT,  # noqa: E731
                                       capture_output=True, text=True, check=False)
    built = run("build", "--out", str(tmp_path))
    assert built.returncode == 0, built.stderr
    assert (tmp_path / "errors" / "index.html").is_file()
    unknown = run("capture", "no_such_error")
    assert unknown.returncode == 2 and "no_such_error" in unknown.stderr


def test_capture_of_a_sign_flip_is_the_failure_envelope_of_its_close(tmp_path):
    result = gallery.capture("sign_flip", tmp_path)
    assert list(result) == ["error", "seed", "month", "books", "target", "close"]
    assert result["error"] == "sign_flip" and result["month"] == MONTH and result["books"] == "tests/fixtures/books"
    assert result["target"]["file"] == "invoices.csv"
    assert result["close"]["exit"] == 3
    assert "CTL-SIGN" in [f["id"] for f in result["close"]["failed_controls"]]
    assert all(set(f) >= {"id", "violating_rows", "detail"} for f in result["close"]["failed_controls"])
    assert json.loads(json.dumps(result, indent=2)) == result


@pytest.fixture(scope="module")
def mini_scenario(tmp_path_factory, mini_closed) -> Path:
    """A copy of the closed `mini` warehouse with the scenario run for March; read-only for the tests."""
    def build(directory: Path) -> None:
        shutil.copy(mini_closed, directory / "w.duckdb")
        result = margin("scenario", "--fixture", "mini", "--month", MONTH, "--json", "--config", str(MINI_CONFIG),
                        "--warehouse", str(directory / "w.duckdb"))
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "gallery_scenario", build) / "w.duckdb"


def test_the_memo_writes_the_gallery_and_links_it_from_both_pages(tmp_path, mini_scenario):
    site = tmp_path / "site"
    result = margin("memo", "--month", MONTH, "--out", str(site), "--warehouse", str(mini_scenario))
    assert result.code == 0, result.stderr
    assert (site / "errors" / "index.html").is_file()
    assert 'href="errors/index.html"' in (site / "index.html").read_text()
    memo = (site / MONTH / "index.html").read_text()
    controls = re.search(r'<section id="controls">.*?</section>', memo, re.S).group(0)
    assert 'href="../errors/index.html"' in controls and "<h3>Planted errors</h3>" in controls
    assert memo_check.crawl(site) == []
