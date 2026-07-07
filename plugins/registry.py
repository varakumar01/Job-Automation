"""Auto-discovery registry for job-source plugins. See PLAN.md §4.

Imports every ``*.py`` in this folder (except base/registry/dunder/underscore)
and collects concrete ``JobSourcePlugin`` subclasses. Adding a portal needs no
edit here — just drop a new ``<site>.py`` in that does
``from base import Job, JobSourcePlugin``.

Import convention: this module forces the plugins folder onto ``sys.path`` and
loads every plugin (and ``base``) by *bare* module name, so there is exactly one
``JobSourcePlugin`` identity whether the registry is run as a script
(``python3 registry.py``) or imported as ``plugins.registry``.

Usage::

    import sys; sys.path.insert(0, "<repo-root>")  # plugins/ lives at the repo root
    from plugins.registry import discover_plugins, get_plugin
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import Iterator

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

from base import JobSourcePlugin  # noqa: E402  — path is set just above

_SKIP = {"base", "registry", "__init__"}


def _iter_plugin_classes() -> Iterator[type[JobSourcePlugin]]:
    seen: set[type] = set()
    for mod_info in pkgutil.iter_modules([str(_PKG_DIR)]):
        name = mod_info.name
        if name in _SKIP or name.startswith("_"):
            continue
        module = importlib.import_module(name)  # bare name; _PKG_DIR is on sys.path
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, JobSourcePlugin)
                and obj is not JobSourcePlugin
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__  # defined here, not imported
                and obj not in seen
            ):
                seen.add(obj)
                yield obj


def discover_plugins() -> list[JobSourcePlugin]:
    """Instantiate every discovered plugin; duplicate ``name``s raise."""
    plugins: dict[str, JobSourcePlugin] = {}
    for cls in _iter_plugin_classes():
        instance = cls()
        if instance.name in plugins:
            raise RuntimeError(
                f"duplicate plugin name {instance.name!r} "
                f"({cls.__name__} vs {type(plugins[instance.name]).__name__})"
            )
        plugins[instance.name] = instance
    return list(plugins.values())


def get_plugin(name: str) -> JobSourcePlugin:
    """Return the plugin registered under ``name`` (KeyError if absent)."""
    for plugin in discover_plugins():
        if plugin.name == name:
            return plugin
    raise KeyError(f"no plugin named {name!r}")


if __name__ == "__main__":
    found = discover_plugins()
    if not found:
        print("(no plugins discovered yet — add a <site>.py implementing JobSourcePlugin)")
    for p in found:
        state = "available" if p.is_available() else "unavailable (check creds)"
        print(f"- {p.name}: {state}")
