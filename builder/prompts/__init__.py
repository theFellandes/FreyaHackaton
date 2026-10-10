"""Versioned prompt templates of the builder and the helper that renders them.

Templates are Jinja2 files under ``builder/prompts/<stage>/<name>.v<N>.j2`` (``understand/``, ``extract/``,
``repair/``, ``compile/``, ``audit/``; the compiler keeps its agent-prompt templates in ``agent/``). The version in the
file name is bumped when a prompt changes meaning, so traces and results can say which prompt produced them.

A template defines two blocks, ``system`` and ``user``::

    {% block system %}You are ...{% endblock %}
    {% block user %}Documents: {{ text }}{% endblock %}

:func:`render_prompt_parts` renders them separately (what the LLM gateway wants), :func:`render_prompt` renders the
whole template as one string. Rendering is strict: a missing context variable raises
:class:`jinja2.UndefinedError` instead of silently producing an empty prompt. Two filters help with data:
``json_compact`` and ``json_pretty`` (Unicode kept, no HTML escaping).

Prompts never contain brief values; everything brief-specific is passed in as context.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template

from builder import paths

__all__ = ["PromptParts", "prompt_id", "render_prompt", "render_prompt_parts"]


@dataclass(frozen=True)
class PromptParts:
    """A rendered prompt: the system message and the user message."""

    system: str
    user: str


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


@lru_cache(maxsize=8)
def _environment(directory: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        autoescape=False,
    )
    env.filters["json_compact"] = _json_compact
    env.filters["json_pretty"] = _json_pretty
    return env


def _template(name: str) -> Template:
    return _environment(str(Path(paths.PROMPTS_DIR))).get_template(name)


def prompt_id(name: str) -> str:
    """Short id of a template for traces: ``'understand/rule_ledger.v1.j2'`` becomes ``'rule_ledger.v1'``."""
    return re.sub(r"\.j2$", "", name.replace("\\", "/").rsplit("/", 1)[-1])


def render_prompt(name: str, **context: Any) -> str:
    """Render the template ``name`` (path below ``builder/prompts``) with ``context`` as one string."""
    return _template(name).render(**context).strip()


def render_prompt_parts(name: str, **context: Any) -> PromptParts:
    """Render the ``system`` and ``user`` blocks of template ``name`` separately.

    Raises:
        ValueError: The template does not define both blocks.
    """
    template = _template(name)
    missing = [block for block in ("system", "user") if block not in template.blocks]
    if missing:
        raise ValueError(f"prompt template {name!r} lacks block(s): {', '.join(missing)}")
    scope = template.new_context(context)
    return PromptParts(
        system="".join(template.blocks["system"](scope)).strip(),
        user="".join(template.blocks["user"](scope)).strip(),
    )
