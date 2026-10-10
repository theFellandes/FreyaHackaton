"""Load the organisers' reference client ``freya.py`` (the "kit") exactly once, with the right environment.

The hackathon kit ships ``freya.py`` at the repository root. It is the reference, battle-tested HTTP client for
the Freya API (create/update/publish workflows, simulations, calls) and we wrap it instead of re-implementing it
(see :mod:`builder.freya.client`). Two properties of that file shape this loader:

* It reads ``FREYA_API_URL``, ``FREYA_API_KEY``, ``FREYA_WORKSPACE_ID`` (and the organisers' model overrides
  ``FREYA_AGENT_PROVIDER`` / ``FREYA_AGENT_MODEL``) **at import time**. The builder's settings (``.env`` and the
  process environment) must therefore be copied into ``os.environ`` *before* the module executes. That is the job
  of :func:`builder.settings.apply_kit_env`, which never touches the two model-override variables.
* Its name, ``freya``, collides with this package (``builder/freya``). When ``builder/build.py`` runs as a script,
  ``sys.path[0]`` is ``builder/`` and a plain ``import freya`` would pick up *this* package. The loader therefore
  executes the kit by file path (``REPO_ROOT/freya.py``) and verifies what it loaded.

The module is cached: every :class:`~builder.freya.client.FreyaClient` shares one kit instance (and so one cached
workspace id). ``AGENT_LLM`` -- the event's fixed agent model -- is never modified here.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType

from builder import paths
from builder.settings import apply_kit_env, get_settings

__all__ = ["KIT_MODULE_NAME", "KitError", "kit_file", "load_kit", "repo_root"]

logger = logging.getLogger(__name__)

#: Name under which the kit module is registered in ``sys.modules`` (the name the kit's own tools import).
KIT_MODULE_NAME = "freya"

_lock = threading.Lock()
_KIT: ModuleType | None = None


class KitError(RuntimeError):
    """The kit's ``freya.py`` is missing or could not be loaded."""


def repo_root() -> Path:
    """Return the repository root, read from ``builder.paths.REPO_ROOT`` at call time (tests may patch it)."""
    return Path(paths.REPO_ROOT)


def kit_file(root: Path | None = None) -> Path:
    """Return the path of the kit's ``freya.py`` under ``root`` (default: :func:`repo_root`)."""
    return (root or repo_root()) / "freya.py"


def load_kit(*, reload: bool = False) -> ModuleType:
    """Return the kit's ``freya`` module, importing it on first use.

    Steps on the first call (or when ``reload`` is true):

    1. Copy the builder settings into ``os.environ`` (:func:`builder.settings.apply_kit_env`), because the kit
       reads its URL, key and workspace at import time.
    2. Put the repository root on ``sys.path`` (the kit's tools import ``freya`` and ``score`` from there).
    3. Execute ``REPO_ROOT/freya.py`` by file path and register it as ``sys.modules["freya"]`` unless another,
       unrelated module already owns that name (it is then left alone).

    Args:
        reload: Re-execute the kit even if it was loaded before, e.g. after the environment changed. The kit's
            cached workspace id is reset by this.

    Returns:
        The executed kit module. It exposes ``api``, ``workspace_id``, ``save_workflow``, ``update_workflow``,
        ``validate_workflow``, ``export_workflow``, ``get_call``, ``AGENT_LLM`` and the simulation helpers.

    Raises:
        KitError: ``freya.py`` is missing or failed to execute.
    """
    global _KIT
    with _lock:
        if _KIT is not None and not reload:
            return _KIT
        _apply_env()
        root = repo_root()
        path = kit_file(root)
        if not path.is_file():
            raise KitError(
                f"the kit client {path} was not found; the builder must run from the kit repository"
            )
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = _execute(path)
        _register(module, path)
        _KIT = module
        logger.debug("loaded the Freya kit client from %s", path)
        return module


def _apply_env() -> None:
    """Copy the Freya settings into ``os.environ`` for the kit (variable names are logged, never values)."""
    exported = apply_kit_env(get_settings())
    logger.debug("exported %s for the Freya kit client", ", ".join(exported or []) or "nothing")


def _execute(path: Path) -> ModuleType:
    """Execute ``path`` as a fresh module named :data:`KIT_MODULE_NAME`."""
    spec = importlib.util.spec_from_file_location(KIT_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise KitError(f"cannot create an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise KitError(f"executing the kit client {path} failed: {exc!r}") from exc
    for name in ("api", "save_workflow", "validate_workflow", "export_workflow", "workspace_id"):
        if not callable(getattr(module, name, None)):
            raise KitError(f"{path} does not look like the Freya kit client (no callable {name!r})")
    return module


def _register(module: ModuleType, path: Path) -> None:
    """Make ``import freya`` elsewhere in this process return the same, correctly configured module."""
    current = sys.modules.get(KIT_MODULE_NAME)
    if current is None or _is_kit_module(current, path):
        sys.modules[KIT_MODULE_NAME] = module
        return
    logger.warning(
        "sys.modules[%r] is %r, not the kit client; leaving it in place and using the kit privately",
        KIT_MODULE_NAME,
        getattr(current, "__file__", current),
    )


def _is_kit_module(module: ModuleType, path: Path) -> bool:
    """True when ``module`` was loaded from the kit file ``path``."""
    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    try:
        return Path(origin).resolve() == path.resolve()
    except OSError:  # pragma: no cover - defensive
        return False
