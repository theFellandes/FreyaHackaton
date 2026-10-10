"""Errors raised by the compiler."""

from __future__ import annotations

__all__ = ["CompileError"]


class CompileError(ValueError):
    """The IR and the tool API cannot be compiled into a workflow (for example a required tool is missing).

    The message names the problem so the repair stage (or the user) can fix the IR or the inputs.
    """
