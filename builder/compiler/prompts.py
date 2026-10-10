"""Jinja2 environment for the agent prompt templates in ``builder/prompts/agent/``.

Templates are named ``<name>.v1.j2`` and rendered with :func:`render`. Two details matter:

* **Custom delimiters.** Freya prompts contain literal ``{{variable}}`` references that must reach the platform
  untouched, so Jinja uses ``<<expr>>``, ``<% block %>`` and ``<# comment #>`` instead of the usual braces.
* **Strict.** ``StrictUndefined`` makes a template that needs a value the compiler did not pass fail loudly instead of
  silently rendering an empty string. ``trim_blocks`` / ``lstrip_blocks`` keep the control lines out of the output.

The rendered text is normalised (:func:`tidy`): trailing spaces removed and runs of blank lines collapsed.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import jinja2

from builder.paths import PROMPTS_DIR

__all__ = ["AGENT_PROMPTS_DIR", "TEMPLATES", "environment", "render", "tidy"]

AGENT_PROMPTS_DIR = PROMPTS_DIR / "agent"
VERSION = "v1"
TEMPLATES = ("system_prompt", "opening", "third_party", "identity", "negotiate", "lean")


_TAG = re.compile(r"<%.*?%>|<<.*?>>|<#.*?#>")


class _TemplateLoader(jinja2.FileSystemLoader):
    """Loader that keeps the line break after an inline block tag.

    ``trim_blocks`` removes the newline after every block tag, which is right for a line that holds only a tag but eats the
    line break of ``- text<% endif %>``. This loader appends one newline to every line that ends with a block tag *and*
    has text outside tags, so the output keeps one line break there.
    """

    def get_source(self, environment: jinja2.Environment, template: str) -> tuple[str, str, Any]:
        source, filename, uptodate = super().get_source(environment, template)
        lines = []
        for line in source.split("\n"):
            stripped = line.rstrip()
            if stripped.endswith("%>") and _TAG.sub("", stripped).strip():
                line = stripped + "\n"
            lines.append(line)
        return "\n".join(lines), filename, uptodate


@lru_cache(maxsize=1)
def environment() -> jinja2.Environment:
    """The shared Jinja environment (cached)."""
    return jinja2.Environment(
        loader=_TemplateLoader(str(AGENT_PROMPTS_DIR)),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        comment_start_string="<#",
        comment_end_string="#>",
    )


def tidy(text: str) -> str:
    """Strip trailing spaces and collapse three or more newlines to one blank line."""
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def render(name: str, **context: Any) -> str:
    """Render the template ``<name>.v1.j2`` (or a full file name) with ``context`` and return normalised text."""
    file_name = name if name.endswith(".j2") else f"{name}.{VERSION}.j2"
    return tidy(environment().get_template(file_name).render(**context))
