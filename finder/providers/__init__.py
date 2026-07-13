"""Provider registry with auto-discovery.

Every .py module in this package (other than base/__init__) is imported; any
that exposes a module-level `PROVIDER` implementing the Provider protocol is
registered under its `name`. That means adding a source -- yours or a shipped
one -- is just dropping a file here and adding its name to
config.PROVIDERS_ENABLED. A module that fails to import (missing optional dep,
API drift) is skipped with a note instead of breaking the whole feature.
"""

import importlib
import pkgutil

from .. import config
from .base import BaseProvider, Candidate, Provider  # re-exported

_registry: dict[str, object] | None = None


def _discover(log=None) -> dict[str, object]:
    found: dict[str, object] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name in ("base",) or mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{__name__}.{mod.name}")
            prov = getattr(m, "PROVIDER", None)
            if prov is not None and getattr(prov, "name", None):
                found[prov.name] = prov
        except Exception as exc:  # optional dep missing / adapter broken
            if log:
                log(f"provider '{mod.name}' unavailable: "
                    f"{type(exc).__name__}: {exc}")
    return found


def registry(log=None, refresh: bool = False) -> dict[str, object]:
    global _registry
    if _registry is None or refresh:
        _registry = _discover(log)
    return _registry


def enabled_providers(names: list[str] | None = None, log=None) -> list[object]:
    """Return live provider objects for `names` (default config order),
    skipping any that didn't load. Order follows `names`."""
    reg = registry(log)
    names = names if names is not None else config.PROVIDERS_ENABLED
    out = []
    for n in names:
        p = reg.get(n)
        if p is not None:
            out.append(p)
        elif log:
            log(f"provider '{n}' enabled but not loaded -- skipping")
    return out


__all__ = ["Candidate", "Provider", "BaseProvider",
           "registry", "enabled_providers"]
