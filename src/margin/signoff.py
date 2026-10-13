"""Sign-off: a committed JSON file binding the SHA-256 of the allocation config
and of the policy config to a named signer role.

The file is named by `combined_sha256 = sha256(rule_sha256 + "\\n" + policy_sha256)`, so the
file in force is the one named by the hash of the current configs and every other file is
history. `write_for` is the pure writer that tests call on temporary copies; the human-only
`margin signoff` command wraps it with a terminal requirement. Nothing here checks a sign-off:
the gate that refuses to run without one is a separate step.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

from .gen_config import ALLOCATION_FILE, POLICY_FILE
from .errors import EXIT_USAGE, MarginError

SIGNOFF_DIR_ENV = "MARGIN_SIGNOFF_DIR"


def _sha256(path: Path, what: str) -> str:
    if not path.is_file():
        raise MarginError(EXIT_USAGE, f"{what} file not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_hashes(config_dir: Path) -> tuple[str, str]:
    """`(rule_sha256, policy_sha256)` of the allocation and policy files in `config_dir`."""
    return (
        _sha256(config_dir / ALLOCATION_FILE, "allocation"),
        _sha256(config_dir / POLICY_FILE, "policy"),
    )


def combined_sha256(rule_sha256: str, policy_sha256: str) -> str:
    return hashlib.sha256(f"{rule_sha256}\n{policy_sha256}".encode("utf-8")).hexdigest()


def signoff_dir(config_dir: Path) -> Path:
    """`MARGIN_SIGNOFF_DIR`, else `<config dir>/signoff`."""
    return Path(os.environ.get(SIGNOFF_DIR_ENV) or config_dir / "signoff")


def write_for(
    config_dir: Path,
    *,
    signer: str,
    role: str,
    signoff_to: Path | None = None,
    signed_on: str | None = None,
) -> Path:
    """Write `<combined_sha256>.json` for the configs in `config_dir`; return its path.

    The directory defaults to `signoff_dir(config_dir)`. `signed_on` (YYYY-MM-DD) defaults to
    today. Signing the same configs again replaces the file with the same name.
    """
    signer, role = signer.strip(), role.strip()
    if not signer or not role:
        raise MarginError(EXIT_USAGE, "a sign-off needs a signer and a role")
    rule, policy = config_hashes(config_dir)
    record = {
        "rule_sha256": rule,
        "policy_sha256": policy,
        "signer": signer,
        "role": role,
        "signed_on": signed_on or datetime.date.today().isoformat(),
    }
    target_dir = signoff_to or signoff_dir(config_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{combined_sha256(rule, policy)}.json"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path
