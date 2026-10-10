"""pytest plugin: make optional packages look uninstalled (``pytest -p tests.hide_optional_packages``).

The runtime extras (``agentic`` -> ``claude_agent_sdk``, ``ingest`` -> ``pypdf``, ``ui`` -> ``streamlit``,
``obs`` -> ``langfuse``) are optional, so the suite must stay green on a machine that has none of them: the tests
that need a package skip themselves, the others stub availability instead of relying on the real install.
``tests/unit/test_optional_extras.py`` runs the affected test modules in a child pytest with this plugin to keep
that true on a development machine that has every extra installed.

The names to hide come from the ``HIDE_PKGS`` environment variable (comma separated). ``sys.modules[name] = None``
makes ``import name`` raise ``ModuleNotFoundError`` and ``importlib.util.find_spec(name)`` return ``None``, which is
exactly what an absent package looks like to the code under test. The plugin must be loaded before anything imports
the package, which ``-p`` guarantees.
"""

from __future__ import annotations

import os
import sys


def hide(names: list[str]) -> list[str]:
    """Make each package (and its already imported submodules) unimportable; returns the names that were hidden."""
    hidden: list[str] = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[module]
        sys.modules[name] = None  # type: ignore[assignment]
        hidden.append(name)
    return hidden


HIDDEN: list[str] = hide(os.environ.get("HIDE_PKGS", "").split(","))
