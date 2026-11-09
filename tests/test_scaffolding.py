"""Guards the repository scaffolding itself.

These exist so every CI gate is green from the first commit (ADR-0027) rather
than failing on an empty test suite, and so tooling pins that must agree cannot
drift apart silently. Replace with real tests as code lands, but keep
`test_ruff_pin_matches_pre_commit` — it catches a genuine footgun.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def test_package_imports() -> None:
    import kb

    assert kb.__doc__


def test_ruff_pin_matches_pre_commit() -> None:
    """A skew here makes `ruff format` flip-flop between local runs and CI."""
    dev: list[str] = _pyproject()["dependency-groups"]["dev"]
    pinned = next(d.split("==")[1] for d in dev if d.startswith("ruff=="))

    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    match = re.search(r"ruff-pre-commit\s*\n\s*rev:\s*(\S+)", config)
    assert match is not None, "could not find the ruff-pre-commit rev"

    assert match.group(1) == f"v{pinned}", (
        f"pyproject pins ruff {pinned} but the pre-commit hook is {match.group(1)}"
    )
