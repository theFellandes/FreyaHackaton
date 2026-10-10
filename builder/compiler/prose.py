"""Node prose: the optional, LLM-drafted extra guidance that the compiler appends to the template-built prompts.

The deterministic templates cover everything the IR holds. Brief nuance that no structured field captured can be added by
the ``draft_prose`` stage (``builder/understand/prose.py``) as a short text per node. The compiler only *places* that
text; it never lets it change structure:

* :class:`NodeProse` - ``system_prompt_extra`` plus ``per_node`` (node name -> text);
* :func:`default_prose` - the template-only fallback used when no LLM is available (empty extras);
* :func:`clean_text` - makes drafted text safe: unknown ``{{variables}}`` are defused (they would be spoken aloud or
  break resolution), length is capped, control characters removed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from builder.policy.ir import PolicyIR, RuleLedger

__all__ = ["NodeProse", "clean_text", "default_prose"]

MAX_NODE_CHARS = 2500
_REF = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_MARK = re.compile("\x01(\\d+)\x01")
_BLANKS = re.compile("\n{3,}")


class NodeProse(BaseModel):
    """Extra prompt text drafted per node.

    ``per_node`` keys are node names of the profile (``opening``, ``third_party``, ``identity``, ``negotiate`` for the
    staged design; ``call`` and ``negotiate`` for the lean one). Missing keys mean "no extra text".
    """

    system_prompt_extra: str = ""
    per_node: dict[str, str] = Field(default_factory=dict)

    def for_node(self, name: str) -> str:
        """The drafted text for a node ("" when none)."""
        return self.per_node.get(name, "")


def default_prose(ir: PolicyIR, ledger: RuleLedger) -> NodeProse:
    """The template-only fallback: no extra text. The templates already carry every IR rule and every uncovered ledger rule."""
    return NodeProse()


def clean_text(text: str, allowed_variables: Iterable[str] = (), max_chars: int = MAX_NODE_CHARS) -> str:
    """Return ``text`` safe to append to a prompt.

    * ``{{name}}`` stays only when ``name`` is in ``allowed_variables`` (or a ``system.`` / ``call.`` reference); any
      other double-brace reference becomes ``<name>``;
    * stray ``{{`` / ``}}`` are removed;
    * control characters are dropped, runs of blank lines collapsed, and the text is cut at ``max_chars``.
    """
    allowed = set(allowed_variables)
    kept: list[str] = []

    def sub(match: re.Match[str]) -> str:
        name = match.group(1).split(":")[0].strip()
        if name in allowed or name.startswith(("system.", "call.")):
            kept.append(match.group(0))
            return f"\x01{len(kept) - 1}\x01"
        return f"<{name}>"

    cleaned = _REF.sub(sub, text or "").replace("{{", "").replace("}}", "")
    cleaned = "".join(ch for ch in cleaned if ch in "\n\t\x01" or ord(ch) >= 32)
    cleaned = _MARK.sub(lambda m: kept[int(m.group(1))], cleaned)
    cleaned = _BLANKS.sub("\n\n", cleaned).strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        cleaned = (cut.rsplit("\n", 1)[0] if "\n" in cut[max_chars // 2 :] else cut.rsplit(" ", 1)[0]).rstrip()
    return cleaned
