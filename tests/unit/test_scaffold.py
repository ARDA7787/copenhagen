"""First tests: the package imports and the repo guard scripts behave."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from scripts import check_pth, detect_private_key

pytestmark = pytest.mark.unit

PACKAGES = [
    "core",
    "registry",
    "validator",
    "policy",
    "engine",
    "adapters",
    "secrets",
    "audit",
    "db",
    "api",
    "compiler",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_subpackages_import(name: str) -> None:
    importlib.import_module(f"copenhagen.{name}")


def test_check_pth_flags_unknown_files(tmp_path: Path) -> None:
    (tmp_path / "_virtualenv.pth").write_text("import _virtualenv\n")
    (tmp_path / "evil.pth").write_text("import os; os.system('true')\n")
    bad = check_pth.unexpected_pth_files([tmp_path, tmp_path / "missing"])
    assert [p.name for p in bad] == ["evil.pth"]


def test_detect_private_key(tmp_path: Path) -> None:
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing here\n")
    dirty = tmp_path / "id_rsa"
    dirty.write_text("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n")
    assert detect_private_key.files_with_keys([str(clean), str(dirty)]) == [str(dirty)]
    assert detect_private_key.main([str(clean)]) == 0
