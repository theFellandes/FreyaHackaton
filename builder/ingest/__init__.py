"""Reading the build inputs into typed objects.

* :mod:`builder.ingest.brief_loader` - brief material (Markdown, text, e-mail, CSV, JSON, PDF, HTML, anything else as
  text) into a :class:`~builder.ingest.brief_loader.BriefBundle` whose ``combined_text()`` is what the LLM stages read.
* :mod:`builder.ingest.tool_api` - the bank's OpenAPI file into a :class:`~builder.ingest.tool_api.ToolApi` of tool
  contracts (URLs, request/response fields, enums) that the compiler and the policy validator rely on.

Both loaders are deterministic, make no network calls and never truncate their input.
"""

from __future__ import annotations

from builder.ingest.brief_loader import BriefBundle, BriefDocument, load_brief, load_briefs
from builder.ingest.tool_api import (
    EXPECTED_TOOL_NAMES,
    FieldSpec,
    ToolApi,
    ToolApiError,
    ToolContract,
    load_tool_api,
)

__all__ = [
    "EXPECTED_TOOL_NAMES",
    "BriefBundle",
    "BriefDocument",
    "FieldSpec",
    "ToolApi",
    "ToolApiError",
    "ToolContract",
    "load_brief",
    "load_briefs",
    "load_tool_api",
]
