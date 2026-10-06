"""Adapter registry: built-ins, plugins, and the production refusal of dev-only adapters."""

import sys
import types
from pathlib import Path

import pytest

from copenhagen.adapters.registry import AdapterContext, build, discover, parse_spec


def plugin(name: str, *, production_ready: bool) -> str:
    module = types.ModuleType(name)

    class Plugin:
        kind = "plugin"

        def __init__(self) -> None:
            self.production_ready = production_ready

    module.create = lambda ctx: Plugin()  # type: ignore[attr-defined]
    sys.modules[name] = module
    return f"{name}:create"


def test_builtins_only_by_default():
    assert set(build(AdapterContext(env="prod"))) == {"http", "human"}


def test_plugin_is_loaded_from_spec():
    target = plugin("plugin_ok", production_ready=True)
    adapters = build(AdapterContext(env="prod"), f"broker={target}")
    assert set(adapters) == {"http", "human", "broker"}


def test_dev_only_plugin_refused_in_production():
    target = plugin("plugin_dev", production_ready=False)
    assert "sim" in build(AdapterContext(env="dev"), f"sim={target}")
    with pytest.raises(ValueError, match="development-only"):
        build(AdapterContext(env="prod"), f"sim={target}")


def test_devkit_fake_adapter_refused_in_production():
    with pytest.raises(ValueError, match="development-only"):
        build(AdapterContext(env="prod"), "fake=copenhagen_devkit.fake:create")


@pytest.mark.parametrize(
    "spec",
    ["http=x:y", "a=b:c,a=b:c", "Bad=x:y", "noequals", "a=module_only"],
)
def test_bad_specs_rejected(spec: str):
    with pytest.raises((ValueError, ModuleNotFoundError)):
        discover(spec)


def test_parse_spec_ignores_blanks():
    assert parse_spec(" a=m:f , ,b=n:g") == {"a": "m:f", "b": "n:g"}


def test_shipped_package_never_imports_devkit():
    src = Path(__file__).resolve().parents[2] / "src" / "copenhagen"
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py") if "copenhagen_devkit" in p.read_text()
    ]
    assert offenders == []
