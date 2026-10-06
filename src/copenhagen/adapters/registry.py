"""Adapter registry: built-in adapters plus plugins a company installs.

Plugins are found two ways:
- the ``copenhagen.adapters`` entry-point group of any installed package, and
- ``COPENHAGEN_ADAPTERS``: comma-separated ``name=module:factory`` pairs.

A factory takes an :class:`AdapterContext` and returns an :class:`Adapter`.
An adapter whose ``production_ready`` attribute is false is refused when ``env`` is
``prod``; development tooling (simulators, fakes) declares itself that way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from typing import cast

from copenhagen.adapters.base import Adapter
from copenhagen.adapters.http import HTTPAdapter
from copenhagen.adapters.human import HumanAdapter

GROUP = "copenhagen.adapters"
NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class AdapterContext:
    env: str
    backends: Mapping[str, str] = field(default_factory=dict[str, str])
    allow_insecure_backends: bool = False


Factory = Callable[[AdapterContext], Adapter]


def _http(ctx: AdapterContext) -> Adapter:
    return HTTPAdapter(dict(ctx.backends), dev_override=ctx.allow_insecure_backends, env=ctx.env)


def _human(ctx: AdapterContext) -> Adapter:
    return HumanAdapter()


BUILTIN: dict[str, Factory] = {"http": _http, "human": _human}


def _resolve(target: str) -> Factory:
    module, sep, attr = target.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"adapter factory must be module:attribute, got {target!r}")
    factory = getattr(import_module(module), attr)
    if not callable(factory):
        raise ValueError(f"adapter factory {target!r} is not callable")
    return cast(Factory, factory)


def parse_spec(spec: str) -> dict[str, str]:
    """Parse ``name=module:factory,...``."""
    out: dict[str, str] = {}
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        name, sep, target = item.partition("=")
        name = name.strip()
        if not sep or not NAME.match(name):
            raise ValueError(f"invalid adapter plugin entry {item!r}")
        if name in out:
            raise ValueError(f"adapter {name!r} listed twice")
        out[name] = target.strip()
    return out


def discover(spec: str = "") -> dict[str, Factory]:
    factories: dict[str, Factory] = dict(BUILTIN)
    for ep in entry_points(group=GROUP):
        if ep.name in factories:
            raise ValueError(f"adapter plugin {ep.name!r} conflicts with an existing adapter")
        factories[ep.name] = cast(Factory, ep.load())
    for name, target in parse_spec(spec).items():
        if name in factories:
            raise ValueError(f"adapter plugin {name!r} conflicts with an existing adapter")
        factories[name] = _resolve(target)
    return factories


def build(ctx: AdapterContext, spec: str = "") -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {}
    for name, factory in discover(spec).items():
        adapter = factory(ctx)
        if ctx.env == "prod" and not getattr(adapter, "production_ready", True):
            raise ValueError(f"adapter {name!r} is development-only and refused in production")
        adapters[name] = adapter
    return adapters
