"""Freya collections-agent builder (Build the Builder hackathon).

The builder reads a bank collections brief (one or several files: Markdown, plain text, e-mail, CSV, JSON, PDF or
HTML) plus the bank's tool API file (OpenAPI), and produces a published Freya phone agent, an exported workflow file
and a human-readable trace of the run.

Package map (each module documents itself in detail):

* ``builder.paths`` / ``builder.settings`` - repository locations and typed configuration from the environment.
* ``builder.trace`` - the run trace (``traces/<slot>-<UTC>.log`` + ``.jsonl``) required by the hackathon.
* ``builder.budget`` - the wall-clock budget every stage checks before it starts.
* ``builder.inputs`` / ``builder.ingest`` - command-line inputs: which file is the tool API, which are briefs, and how
  they are read into text (``brief_loader``) and typed tool contracts (``tool_api``).
* ``builder.llm`` - the LLM gateway ("key changer") that rotates keys and providers.
* ``builder.policy`` / ``builder.understand`` - the generic policy model and the LLM stages that fill it from a brief.
* ``builder.compiler`` / ``builder.checks`` - deterministic compilation of the policy into Freya workflow JSON and the
  static checks run on it.
* ``builder.freya`` - the Freya platform client built on the kit's ``freya.py``.
* ``builder.graph`` / ``builder.build`` - the LangGraph that wires the stages together and the CLI entry point.

Nothing in this package is specific to one brief: every bank name, date, amount and outcome comes from the inputs.
"""

from __future__ import annotations

# Must run before anything imports LangChain/LangSmith: on machines whose Application Control policy blocks the
# native ``uuid_utils`` extension it installs a pure-Python fallback (see builder/_compat.py).
from builder._compat import ensure_uuid_utils

ensure_uuid_utils()

__version__ = "0.1.0"

__all__ = ["__version__"]
