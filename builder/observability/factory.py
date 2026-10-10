"""Build the observer a run uses: the local JSONL log always, Langfuse when it is configured.

``build_observer(settings, trace)`` is called once by ``builder/build.py``. It writes one trace line saying what is on::

    OBS | langfuse enabled (https://cloud.langfuse.com)
    OBS | langfuse disabled (no keys)
    OBS | langfuse disabled (package not installed: pip install ".[obs]")
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from builder.observability.base import CompositeObserver, LLMObserver
from builder.observability.jsonl import JsonlObserver, llm_log_path_for

logger = logging.getLogger(__name__)

__all__ = ["build_observer"]


def _secret(value: Any) -> str:
    """The text of a ``SecretStr`` / ``str`` setting; ``""`` when unset."""
    getter = getattr(value, "get_secret_value", None)
    text = getter() if callable(getter) else value
    return text.strip() if isinstance(text, str) else ""


def _note(trace: Any, message: str, **data: Any) -> None:
    event = getattr(trace, "event", None)
    if not callable(event):
        return
    try:
        event("obs", message, **data)
    except Exception:  # a trace problem must never stop the build
        logger.warning("could not write the observability trace line", exc_info=True)


def build_observer(
    settings: Any,
    trace: Any,
    *,
    langfuse_factory: Callable[..., LLMObserver] | None = None,
) -> LLMObserver:
    """Create the run's observer.

    Args:
        settings: ``builder.settings.Settings`` (uses ``langfuse_public_key``, ``langfuse_secret_key``,
            ``langfuse_host`` and ``observability_log_prompts``; missing attributes mean "off" / defaults).
        trace: The run's ``TraceWriter``: its ``redact`` masks secrets and its ``log_path`` decides where the LLM
            log goes (``<slot>-<ts>.llm.jsonl``). A trace without files (``NullTrace``) gets an in-memory log, so
            tests never write into ``traces/``.
        langfuse_factory: Replacement for ``LangfuseObserver`` (tests).

    Returns:
        A :class:`CompositeObserver` (JSONL always; Langfuse added when both keys are set and the SDK is importable).
    """
    redact = getattr(trace, "redact", None)
    observers: list[LLMObserver] = [JsonlObserver(llm_log_path_for(trace), redact=redact)]

    public_key = _secret(getattr(settings, "langfuse_public_key", None))
    secret_key = _secret(getattr(settings, "langfuse_secret_key", None))
    host = str(getattr(settings, "langfuse_host", None) or "https://cloud.langfuse.com")
    if public_key and secret_key:
        try:
            factory = langfuse_factory
            if factory is None:
                from builder.observability.langfuse_observer import LangfuseObserver

                factory = LangfuseObserver
            observers.append(
                factory(
                    public_key,
                    secret_key,
                    host,
                    log_prompts=bool(getattr(settings, "observability_log_prompts", True)),
                    redact=redact,
                )
            )
        except ImportError:
            _note(trace, 'langfuse disabled (package not installed: pip install ".[obs]")')
        except Exception as exc:
            logger.warning("could not start the Langfuse observer (%s)", type(exc).__name__, exc_info=True)
            _note(trace, f"langfuse disabled ({type(exc).__name__} while starting)")
        else:
            _note(trace, f"langfuse enabled ({host})", host=host)
    else:
        _note(trace, "langfuse disabled (no keys)")
    return CompositeObserver(observers)
