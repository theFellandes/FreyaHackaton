"""Compiler: PolicyIR + ToolApi + RuleLedger (+ optional LLM prose) -> Freya workflow JSON.

Pure and deterministic (no I/O, no LLM calls). Modules:

* :mod:`builder.compiler.workflow_model` - typed, tolerant mirror of the workflow JSON.
* :mod:`builder.compiler.tools` - tool-function builders (model-called tools, engine pre-actions, code, end terminals).
* :mod:`builder.compiler.hooks` - safe pre/post Python hooks that shape tool inputs and results.
* :mod:`builder.compiler.codegen` - code-node snippets (date table, account facts, outcome decision) generated from the IR.
* :mod:`builder.compiler.prompts` / :mod:`builder.compiler.prose` - Jinja prompt templates and LLM-drafted node prose.
* :mod:`builder.compiler.profiles` - the agent designs: ``staged`` (validated) and ``lean`` (experiment).
* :mod:`builder.compiler.compile` - :func:`compile_workflow`, the single entry point.

See ``docs/COMPILER.md`` for the node graph, the rule-to-mechanism map and the lint rules.
"""

from __future__ import annotations

from builder.compiler.compile import CompiledWorkflow, compile_workflow

__all__ = ["CompiledWorkflow", "compile_workflow"]
