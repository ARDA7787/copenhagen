"""copenhagen.core runs inside the Temporal workflow sandbox, so it must be deterministic.

import-linter guards our own packages; this test guards the standard library and third parties:
no clock, randomness, environment, files, network or processes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CORE = Path(__file__).resolve().parents[2] / "src" / "copenhagen" / "core"

FORBIDDEN_MODULES = {
    "asyncio",
    "httpx",
    "io",
    "os",
    "pathlib",
    "random",
    "secrets",
    "shutil",
    "socket",
    "sqlalchemy",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "urllib",
    "uuid",
}
FORBIDDEN_CALLS = {"now", "utcnow", "today", "time", "time_ns", "monotonic", "open", "getenv"}


def _problems(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif node.module and node.level == 0:
                names = [node.module]
            else:
                names = []
            found += [
                f"{path.name}:{node.lineno} imports {n}"
                for n in names
                if n.split(".")[0] in FORBIDDEN_MODULES
            ]
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in FORBIDDEN_CALLS:
                found.append(f"{path.name}:{node.lineno} calls {name}()")
    return found


def test_core_is_deterministic() -> None:
    files = sorted(CORE.rglob("*.py"))
    assert files, "copenhagen.core has no modules"
    problems = [p for f in files for p in _problems(f)]
    assert problems == []


def test_the_check_catches_a_clock(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from datetime import datetime\nimport time\nx = datetime.now()\n")
    assert _problems(bad) == ["bad.py:2 imports time", "bad.py:3 calls now()"]
