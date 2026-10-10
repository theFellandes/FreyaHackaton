"""Everything the builder does on the Freya platform.

* :mod:`builder.freya.kit` loads the organisers' reference client ``freya.py`` with the builder's settings.
* :mod:`builder.freya.client` wraps it in :class:`FreyaClient`: dry-run validation, create-or-update-and-publish
  by slot (:class:`DeployInfo`), reading a workflow back, export, chat turns (:class:`ChatTurn`) and calls.
* :mod:`builder.freya.diff` finds fields the platform silently stripped from a stored workflow.
* :mod:`builder.freya.practice` runs the kit's practice harness ``tools/run_cases.py`` for kit briefs.
* :mod:`builder.freya.background` runs blocking platform calls in abandonable daemon threads with a cancel token, so the
  wall-clock guard of ``build.py`` can cut a build off without waiting for (or being surprised by) a blocked call.

Importing this package makes no network call and does not load the kit; :class:`FreyaClient` loads it on first
use.
"""

from __future__ import annotations

from builder.freya.background import CancelToken, PlatformCancelled, run_blocking
from builder.freya.client import (
    ChatTurn,
    DeployInfo,
    FreyaClient,
    FreyaError,
    FreyaValidationError,
    count_nodes,
    count_tools,
    default_state_file,
)
from builder.freya.diff import null_paths, stripped_fields
from builder.freya.kit import KitError, load_kit
from builder.freya.practice import PracticeError, PracticeResult, kit_brief_letter, run_practice

__all__ = [
    "CancelToken",
    "ChatTurn",
    "DeployInfo",
    "FreyaClient",
    "FreyaError",
    "FreyaValidationError",
    "KitError",
    "PlatformCancelled",
    "PracticeError",
    "PracticeResult",
    "count_nodes",
    "count_tools",
    "default_state_file",
    "kit_brief_letter",
    "load_kit",
    "null_paths",
    "run_blocking",
    "run_practice",
    "stripped_fields",
]
