"""The bank's tool API file (OpenAPI 3 or Swagger 2, JSON or YAML) as typed tool contracts.

The phone agent may call only the bank's tools, and the hidden briefs keep the seven tool names but may change their
fields and allowed values. Everything the builder knows about a tool therefore comes from this file, never from
constants: URLs, request fields (with types, enums, formats, descriptions and the required flag), response fields
(nested objects and arrays of objects included) and documented error responses.

:func:`load_tool_api` returns a :class:`ToolApi`:

* one :class:`ToolContract` per path; the tool name is the last path segment (``/v1/close_call`` -> ``close_call``),
  the URL is ``servers[0].url`` (server variables filled with their defaults) + the path;
* request fields come from the ``application/json`` request body schema (plus query/path parameters, if any);
* response fields come from the 200 response (else the first 2xx, else ``default``); every other documented status
  goes to ``errors``;
* local ``$ref`` pointers (``#/components/...``, ``#/definitions/...``) are resolved, ``allOf`` is merged, and a
  single non-null ``anyOf``/``oneOf`` variant is unwrapped; recursive schemas stop at the first repetition.

The seven expected tool names (:data:`EXPECTED_TOOL_NAMES`) are checked with a warning only: a missing tool is
reported, unknown extra tools are kept.

Nested fields are addressed with dotted paths: ``contract.field("products.type")`` is the ``type`` field of the
objects in the ``products`` array; ``contract.enum("outcome")`` lists the allowed outcome values.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import BaseModel, Field

from builder.inputs import InputError

logger = logging.getLogger(__name__)

#: The seven tools every brief's tool API is expected to provide (names are fixed by the hackathon; their fields and
#: allowed values may change between briefs).
EXPECTED_TOOL_NAMES: tuple[str, ...] = (
    "verify_identity",
    "get_account",
    "create_promise",
    "add_note",
    "schedule_callback",
    "transfer_to_agent",
    "close_call",
)

#: Preferred HTTP method when a path defines several operations.
_METHOD_PREFERENCE = ("post", "put", "patch", "get", "delete")
_PATH_METHODS = frozenset({*_METHOD_PREFERENCE, "head", "options", "trace"})
_PATH_ITEM_FIELDS = _PATH_METHODS | {"$ref", "summary", "description", "parameters", "servers"}
_MAX_SCHEMA_DEPTH = 12
_JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array"})

FieldScope = Literal["any", "request", "response"]


class ToolApiError(InputError):
    """The tool API file is unreadable, not an OpenAPI/Swagger document, or has unresolvable references."""


class FieldSpec(BaseModel):
    """One request or response field of a tool.

    Attributes:
        name: Property name.
        type: JSON-schema type (``string``, ``number``, ``integer``, ``boolean``, ``object``, ``array``).
        enum: Allowed values as strings (for an array of scalars: the allowed item values), or ``None``.
        description: The schema description (empty when none).
        required: Whether the parent schema lists the field as required.
        format: The schema ``format`` (e.g. ``date``), or ``None``.
        items: Sub-fields: the properties of an object, or of the objects in an array; ``None`` for scalars.
        fixed: The value the contract fixes for a scalar field, as text: its ``const``, its only ``enum`` value or
            its ``default`` (``None`` = the contract leaves the value open).
    """

    name: str
    type: str = "string"
    enum: list[str] | None = None
    description: str = ""
    required: bool = False
    format: str | None = None
    items: list[FieldSpec] | None = None
    fixed: str | None = None

    def child(self, name: str) -> FieldSpec | None:
        """The sub-field called ``name`` (see ``items``), or ``None``."""
        return next((item for item in self.items or () if item.name == name), None)


class ToolContract(BaseModel):
    """Everything known about one tool of the bank's API.

    Attributes:
        name: Tool name (last path segment).
        method: HTTP method in upper case.
        url: Absolute URL (``servers[0].url`` + path); just the path when the file declares no server.
        path: The OpenAPI path the contract was read from.
        summary / description: Operation texts.
        request_fields: Fields of the JSON request body (and query/path parameters).
        response_fields: Fields of the success response body.
        response_description: Description of the success response (e.g. how ``ok`` is to be read).
        errors: Status code -> description of every other documented response.
        headers: Header parameters whose value the contract fixes (``const``, an enum of one, a ``default``), sent on
            every call.
        notes: What the contract asks for that the agent cannot send (a credential header, a security scheme).
    """

    name: str
    method: str = "POST"
    url: str
    path: str = ""
    summary: str = ""
    description: str = ""
    request_fields: list[FieldSpec] = Field(default_factory=list)
    response_fields: list[FieldSpec] = Field(default_factory=list)
    response_description: str = ""
    errors: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    request_content_type: str = "application/json"
    notes: list[str] = Field(default_factory=list)

    @property
    def required_fields(self) -> list[str]:
        """Names of the required request fields, in schema order."""
        return [spec.name for spec in self.request_fields if spec.required]

    def get_field(self, name: str, where: FieldScope = "any") -> FieldSpec | None:
        """The field at dotted path ``name`` (request fields first, then response fields), or ``None``.

        Args:
            name: Field name, or a dotted path into nested objects/arrays (``"products.type"``).
            where: Restrict the search to ``"request"`` or ``"response"`` fields.
        """
        head, *rest = name.split(".")
        scopes: list[list[FieldSpec]] = []
        if where in ("any", "request"):
            scopes.append(self.request_fields)
        if where in ("any", "response"):
            scopes.append(self.response_fields)
        for fields in scopes:
            found = next((spec for spec in fields if spec.name == head), None)
            for part in rest:
                found = found.child(part) if found is not None else None
            if found is not None:
                return found
        return None

    def field(self, name: str, where: FieldScope = "any") -> FieldSpec:
        """Like :meth:`get_field` but raises ``KeyError`` (listing the known fields) when the field is missing."""
        found = self.get_field(name, where)
        if found is None:
            known = ", ".join(spec.name for spec in (*self.request_fields, *self.response_fields))
            raise KeyError(f"tool {self.name!r} has no field {name!r} (fields: {known or 'none'})")
        return found

    def enum(self, name: str, where: FieldScope = "any") -> list[str]:
        """Allowed values of a field (``[]`` when the field exists but is unconstrained).

        Raises:
            KeyError: If the field does not exist.
        """
        return list(self.field(name, where).enum or [])

    def describe(self) -> str:
        """A compact plain-text description of the contract, for prompts and traces."""
        lines = [f"- {self.name}  {self.method} {self.url}"]
        for text in (self.summary, self.description):
            if text:
                lines.append(f"  {' '.join(text.split())}")
        lines.append("  request:")
        lines.extend(_describe_fields(self.request_fields, indent=4) or ["    (no fields)"])
        lines.append(
            "  response"
            + (f" ({' '.join(self.response_description.split())})" if self.response_description else "")
            + ":"
        )
        lines.extend(_describe_fields(self.response_fields, indent=4) or ["    (no fields)"])
        for code, text in self.errors.items():
            lines.append(f"  error {code}: {' '.join(text.split())}")
        return "\n".join(lines)


class ToolApi(BaseModel):
    """The parsed tool API.

    Attributes:
        title / version / description: From the document's ``info`` block.
        base_url: ``servers[0].url`` (OpenAPI 3) or ``scheme://host/basePath`` (Swagger 2); may be empty.
        tools: Tool name -> contract, in document order.
        sha256: SHA-256 of the raw file bytes.
    """

    title: str = ""
    version: str = ""
    description: str = ""
    base_url: str = ""
    tools: dict[str, ToolContract]
    sha256: str

    @property
    def tool_names(self) -> list[str]:
        """Tool names in document order."""
        return list(self.tools)

    def require(self, name: str) -> ToolContract:
        """The contract of tool ``name``.

        Raises:
            ToolApiError: If the tool API has no such tool (the message lists the available tools).
        """
        try:
            return self.tools[name]
        except KeyError:
            available = ", ".join(self.tools) or "none"
            raise ToolApiError(f"The tool API has no tool {name!r} (available: {available}).") from None

    def missing_tools(self) -> list[str]:
        """Expected tool names (:data:`EXPECTED_TOOL_NAMES`) that this API lacks."""
        return [name for name in EXPECTED_TOOL_NAMES if name not in self.tools]

    def extra_tools(self) -> list[str]:
        """Tools of this API that are not among :data:`EXPECTED_TOOL_NAMES`."""
        return [name for name in self.tools if name not in EXPECTED_TOOL_NAMES]

    def describe(self) -> str:
        """Plain-text description of every tool (fields, types, enums, errors) for LLM prompts."""
        head = f"Tool API: {self.title or 'untitled'}" + (
            f" (version {self.version})" if self.version else ""
        )
        lines = [head]
        if self.description:
            lines.append(" ".join(self.description.split()))
        if self.base_url:
            lines.append(f"Base URL: {self.base_url}")
        lines.extend(contract.describe() for contract in self.tools.values())
        return "\n".join(lines)


def _describe_fields(fields: Iterable[FieldSpec], indent: int) -> list[str]:
    pad = " " * indent
    lines = []
    for spec in fields:
        kind = spec.type + (f", format {spec.format}" if spec.format else "")
        if spec.type == "array" and spec.items:
            kind = "array of objects"
        text = f"{pad}* {spec.name} ({kind}{', required' if spec.required else ''})"
        if spec.enum:
            text += " one of: " + " | ".join(spec.enum)
        if spec.description:
            text += " - " + " ".join(spec.description.split())
        lines.append(text)
        if spec.items:
            lines.extend(_describe_fields(spec.items, indent + 4))
    return lines


# --- loading -----------------------------------------------------------------------------------------------------------


class _OpenApiYamlLoader(yaml.SafeLoader):
    """SafeLoader without YAML 1.1 surprises: ``yes``/``no``/``on``/``off`` and dates stay strings."""


_OpenApiYamlLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_OpenApiYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_tool_api(path: Path | str) -> ToolApi:
    """Read an OpenAPI 3 / Swagger 2 file (JSON or YAML) into a :class:`ToolApi`.

    Raises:
        ToolApiError: If the file cannot be read or parsed, is not an OpenAPI/Swagger document, defines no paths, or
            contains a ``$ref`` that cannot be resolved inside the file.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolApiError(f"Cannot read the tool API file {path}: {exc.strerror or exc}") from exc
    document = _parse_document(raw, path)
    api = parse_tool_api(document, sha256=hashlib.sha256(raw).hexdigest())
    missing = api.missing_tools()
    if missing:
        logger.warning("tool API %s lacks expected tools: %s", path.name, ", ".join(missing))
    extra = api.extra_tools()
    if extra:
        logger.warning("tool API %s defines tools beyond the expected seven: %s", path.name, ", ".join(extra))
    return api


def read_tool_api_document(path: Path | str) -> dict[str, Any]:
    """The raw parsed OpenAPI/Swagger document of ``path`` (for extensions such as ``x-freya-role``).

    Raises:
        ToolApiError: As :func:`load_tool_api`.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolApiError(f"Cannot read the tool API file {path}: {exc.strerror or exc}") from exc
    return _parse_document(raw, path)


def _parse_document(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolApiError(f"The tool API file {path.name} is not UTF-8 text.") from exc
    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = yaml.load(
                text, Loader=_OpenApiYamlLoader
            )  # a SafeLoader subclass: no arbitrary objects
        except yaml.YAMLError as exc:
            raise ToolApiError(
                f"The tool API file {path.name} is neither valid JSON nor valid YAML: {exc}"
            ) from exc
    if not isinstance(document, dict) or not ({"openapi", "swagger"} & set(document)):
        raise ToolApiError(
            f"The tool API file {path.name} is not an OpenAPI/Swagger document (no top-level 'openapi' or 'swagger')."
        )
    return document


def parse_tool_api(document: Mapping[str, Any], sha256: str = "", *, every_operation: bool = False) -> ToolApi:
    """Build a :class:`ToolApi` from an already parsed OpenAPI/Swagger document (see :func:`load_tool_api`).

    Args:
        document: The parsed document.
        sha256: Hash recorded on the result.
        every_operation: Make every operation a tool (several methods on one path included), named by its
            ``operationId`` (else the last path segment, else method and path) reduced to a safe ASCII identifier and
            made unique. Used for contracts whose operations are mapped to the canonical tool roles afterwards
            (:mod:`builder.understand.tool_roles`); the default keeps the practice behaviour: one tool per path, named
            by its last segment.
    """
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise ToolApiError("The tool API defines no paths, so it has no tools.")
    resolver = _Resolver(document)
    _validate_paths_shape(paths, resolver)
    info = document.get("info") if isinstance(document.get("info"), Mapping) else {}
    base_url = _base_url(document)
    tools: dict[str, ToolContract] = {}
    for raw_path, path_item in paths.items():
        if str(raw_path).startswith("x-"):
            continue
        if every_operation:
            item, _ = resolver.resolve(path_item)
            for method in _METHOD_PREFERENCE:
                if not isinstance(item.get(method), Mapping):
                    continue
                contract = _contract_for(str(raw_path), path_item, base_url, resolver, method=method)
                if contract is not None:
                    name = _unique(safe_tool_name(_operation_name(str(raw_path), method, item[method])), tools)
                    tools[name] = contract.model_copy(update={"name": name})
            continue
        contract = _contract_for(str(raw_path), path_item, base_url, resolver)
        if contract is None:
            continue
        if contract.name in tools:
            logger.warning("tool %s is defined twice (%s); keeping the first", contract.name, raw_path)
            continue
        tools[contract.name] = contract
    if not tools:
        raise ToolApiError("The tool API defines no usable operations.")
    return ToolApi(
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
        description=str(info.get("description") or ""),
        base_url=base_url,
        tools=tools,
        sha256=sha256,
    )


def _validate_paths_shape(paths: Mapping[str, Any], resolver: _Resolver) -> None:
    """Reject malformed nesting instead of silently dropping documented operations."""
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith(("/", "x-")):
            raise ToolApiError(f"Invalid Paths key {path!r}: only '/...' routes and 'x-' extensions belong in paths")
        if path.startswith("x-"):
            continue
        _validate_path_item_shape(path, item, resolver)


def _validate_path_item_shape(
    path: str, item: Any, resolver: _Resolver, seen: frozenset[str] = frozenset(),
) -> None:
    if not isinstance(item, Mapping):
        raise ToolApiError(f"PathItem {path!r} must be an object")
    for key, value in item.items():
        if isinstance(key, str) and key.startswith("x-"):
            continue
        if key not in _PATH_ITEM_FIELDS:
            raise ToolApiError(f"Invalid PathItem field {key!r} at {path!r}; routes belong directly in paths, "
                               "and document metadata belongs at the document root")
        if key in _PATH_METHODS and not isinstance(value, Mapping):
            raise ToolApiError(f"Operation {str(key).upper()} {path} must be an object")
        if key in {"summary", "description", "$ref"} and not isinstance(value, str):
            raise ToolApiError(f"PathItem {path!r} field {key!r} must be a string")
        if key in {"parameters", "servers"} and (
            not isinstance(value, list) or any(not isinstance(entry, Mapping) for entry in value)
        ):
            raise ToolApiError(f"PathItem {path!r} field {key!r} must be an array of objects")
    ref = item.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            raise ToolApiError(f"Recursive PathItem $ref {ref!r} at {path!r}")
        _validate_path_item_shape(path, resolver.pointer(ref), resolver, seen | {ref})


def _operation_name(path: str, method: str, operation: Mapping[str, Any]) -> str:
    """The raw name of one operation: its ``operationId``, else the last static path segment, else method + path."""
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        return operation_id.strip()
    return _tool_name(path, {}) or f"{method}_{path}"


def safe_tool_name(raw: str) -> str:
    """``raw`` as an ASCII identifier (accents folded, other characters ``_``; never empty, never a leading digit)."""
    text = unicodedata.normalize("NFKD", raw.replace("\u0131", "i").replace("\u0130", "I"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    name = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_]+", "_", text)).strip("_")
    if not name:
        return "operation"
    return f"op_{name}" if name[0].isdigit() else name


def _unique(name: str, taken: Mapping[str, Any]) -> str:
    candidate, number = name, 2
    while candidate in taken:
        candidate, number = f"{name}_{number}", number + 1
    return candidate


def with_base_url(api: ToolApi, base_url: str) -> ToolApi:
    """A copy of ``api`` whose every tool URL lies on ``base_url`` (the mock the agent may call); paths and methods stay.

    A base URL with a path (``https://mock.dev/api``) replaces the contract's server URL: tool URL = base URL + operation
    path. A bare origin (``https://mock.dev``) replaces only the origin, so a path prefix of the contract's server
    (``/v1``) is kept: tool URL = origin + server path + operation path.
    """
    base = base_url.strip().rstrip("/")
    # a bare origin keeps the contract's server path prefix, from an absolute (https://bank/v1) or relative (/v1) URL
    explicit_prefix = bool(urlsplit(base).path.strip("/"))
    server = base if explicit_prefix else base + urlsplit(api.base_url).path.rstrip("/")
    tools = {}
    for name, contract in api.tools.items():
        # A bare origin preserves the URL actually resolved for this operation, including its own server override.
        # An explicit mock prefix keeps the legacy behavior: replace every server with that prefix.
        path = (contract.path or urlsplit(contract.url).path) if explicit_prefix else urlsplit(contract.url).path
        url_base = server if explicit_prefix else base
        url = f"{url_base}{path}" if path.startswith("/") else f"{url_base}/{path}"
        tools[name] = contract.model_copy(update={"url": url})
    return api.model_copy(update={"base_url": server, "tools": tools})


def _base_url(document: Mapping[str, Any]) -> str:
    """``servers[0].url`` with variables substituted (OpenAPI 3), or ``scheme://host/basePath`` (Swagger 2)."""
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
        server = servers[0]
        url = str(server.get("url") or "")
        variables = server.get("variables") if isinstance(server.get("variables"), Mapping) else {}
        for name, spec in variables.items():
            default = spec.get("default") if isinstance(spec, Mapping) else None
            if default is not None:
                url = url.replace("{" + str(name) + "}", str(default))
        return url.rstrip("/")
    host = document.get("host")
    base_path = str(document.get("basePath") or "")
    if host:
        schemes = document.get("schemes") or ["https"]
        return f"{schemes[0]}://{host}{base_path}".rstrip("/")
    return base_path.rstrip("/")


def _tool_name(path: str, operation: Mapping[str, Any]) -> str | None:
    """Last non-parameter path segment, else the operationId."""
    segments = [segment for segment in path.strip("/").split("/") if segment and not segment.startswith("{")]
    if segments:
        return segments[-1]
    operation_id = operation.get("operationId")
    return str(operation_id) if operation_id else None


def _contract_for(
    path: str, path_item: Any, base_url: str, resolver: _Resolver, *, method: str | None = None
) -> ToolContract | None:
    item, _ = resolver.resolve(path_item)
    methods = [method for method in _METHOD_PREFERENCE if isinstance(item.get(method), Mapping)]
    if not methods:
        logger.warning("path %s has no operation; skipped", path)
        return None
    if method is None:
        if len(methods) > 1:
            logger.warning("path %s has several operations (%s); using %s", path, ", ".join(methods), methods[0])
        method = methods[0]
    operation: Mapping[str, Any] = item[method]
    if isinstance(operation.get("servers"), list):
        base_url = _base_url(operation)
    elif isinstance(item.get("servers"), list):
        base_url = _base_url(item)
    name = _tool_name(path, operation)
    if name is None:
        logger.warning("path %s yields no tool name; skipped", path)
        return None
    parameters = [*(item.get("parameters") or []), *(operation.get("parameters") or [])]
    request_fields = _request_fields(operation, parameters, resolver)
    headers, notes = _header_parameters(name, parameters, resolver)
    request_content_type = _request_content_type(operation, resolver)
    if request_content_type != "application/json":
        headers.setdefault("Content-Type", request_content_type)
    security = operation.get("security", resolver.security)
    if isinstance(security, list) and any(isinstance(req, Mapping) and req for req in security):
        schemes = sorted({str(key) for req in security if isinstance(req, Mapping) for key in req})
        notes.append(f"{name} declares the security scheme(s) {', '.join(schemes)}; the agent sends no credentials")
    success_code, response_fields, response_description, errors = _responses(operation, resolver)
    logger.debug("tool %s: %s %s (success response %s)", name, method.upper(), path, success_code)
    return ToolContract(
        name=name,
        method=method.upper(),
        url=f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}",
        path=path,
        summary=str(operation.get("summary") or ""),
        description=str(operation.get("description") or ""),
        request_fields=request_fields,
        response_fields=response_fields,
        response_description=response_description,
        errors=errors,
        headers=headers,
        request_content_type=request_content_type,
        notes=notes,
    )


def _header_parameters(tool: str, parameters: list[Any], resolver: _Resolver) -> tuple[dict[str, str], list[str]]:
    """``in: header`` parameters: the fixed values to send, and a note for each required one without a value."""
    headers: dict[str, str] = {}
    notes: list[str] = []
    for raw_parameter in parameters:
        parameter, refs = resolver.resolve(raw_parameter)
        name = str(parameter.get("name") or "")
        if parameter.get("in") != "header" or not name or name.lower() == "content-type":
            continue
        schema = parameter.get("schema") if isinstance(parameter.get("schema"), Mapping) else parameter
        spec = _field(name, schema, resolver, refs, depth=0)
        if spec.fixed is not None:
            headers[name] = spec.fixed
        elif parameter.get("required"):
            notes.append(f"{tool} requires the header {name} and the contract fixes no value for it; it is not sent")
    return headers, notes


def _json_schema_of(content: Any, resolver: _Resolver) -> Any:
    """The schema of the JSON media type of a ``content`` map (``application/json`` preferred)."""
    if not isinstance(content, Mapping) or not content:
        return None
    media_type = _preferred_media_type(content)
    media, _ = resolver.resolve(content[media_type])
    return media.get("schema")


def _preferred_media_type(content: Mapping[str, Any]) -> str:
    return str(next((key for key in content if str(key).split(";")[0].strip() == "application/json"), None)
               or next((key for key in content if "json" in str(key)), None) or next(iter(content)))


def _request_content_type(operation: Mapping[str, Any], resolver: _Resolver) -> str:
    """The actual media type of the request schema selected for this operation."""
    body, _ = resolver.resolve(operation.get("requestBody") or {})
    content = body.get("content")
    if isinstance(content, Mapping) and content:
        return _preferred_media_type(content)
    consumes = operation.get("consumes") or resolver.consumes
    if isinstance(consumes, list) and consumes:
        return str(consumes[0])
    return "application/json"


def _request_fields(
    operation: Mapping[str, Any], parameters: list[Any], resolver: _Resolver
) -> list[FieldSpec]:
    fields: list[FieldSpec] = []
    body_schema: Any = None
    if operation.get("requestBody") is not None:
        body, _ = resolver.resolve(operation["requestBody"])
        body_schema = _json_schema_of(body.get("content"), resolver)
    for raw_parameter in parameters:
        parameter, refs = resolver.resolve(raw_parameter)
        location = parameter.get("in")
        if location == "body":  # Swagger 2
            body_schema = parameter.get("schema")
        elif location in ("query", "path", "formData") and parameter.get("name"):
            schema = parameter.get("schema") if isinstance(parameter.get("schema"), Mapping) else parameter
            spec = _field(str(parameter["name"]), schema, resolver, refs, depth=0)
            spec.required = bool(parameter.get("required") or location == "path")
            if not spec.description and parameter.get("description"):
                spec.description = str(parameter["description"])
            fields.append(spec)
    if body_schema is not None:
        fields = _object_fields(body_schema, resolver, frozenset(), depth=0, fallback_name="body") + fields
    return fields


def _responses(
    operation: Mapping[str, Any], resolver: _Resolver
) -> tuple[str | None, list[FieldSpec], str, dict[str, str]]:
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return None, [], "", {}
    codes = [str(code) for code in responses]
    success = (
        "200" if "200" in codes else next((code for code in sorted(codes) if code.startswith("2")), None)
    )
    if success is None and "default" in codes:
        success = "default"
    fields: list[FieldSpec] = []
    description = ""
    errors: dict[str, str] = {}
    for code, raw_response in responses.items():
        response, _ = resolver.resolve(raw_response)
        text = str(response.get("description") or "")
        if str(code) != success:
            errors[str(code)] = text
            continue
        description = text
        schema = _json_schema_of(response.get("content"), resolver)
        if schema is None:
            schema = response.get("schema")  # Swagger 2
        if schema is not None:
            fields = _object_fields(schema, resolver, frozenset(), depth=0, fallback_name="body")
    return success, fields, description, errors


# --- schema handling ---------------------------------------------------------------------------------------------------


class _Resolver:
    """Resolves local JSON pointers (``#/components/schemas/X``) and flattens ``allOf`` / single-variant unions."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self.security: Any = document.get("security")  # the document-wide security requirement, if any
        self.consumes: Any = document.get("consumes")  # Swagger request media type when the operation inherits it

    def resolve(self, node: Any, seen: frozenset[str] = frozenset()) -> tuple[dict[str, Any], frozenset[str]]:
        """Return ``node`` with ``$ref``/``allOf``/unions resolved at the top level, and the refs followed so far.

        A ``$ref`` already in ``seen`` (a recursive schema) is not followed again; its siblings are returned instead.

        Raises:
            ToolApiError: For a non-local or dangling ``$ref``.
        """
        if not isinstance(node, Mapping):
            return {}, seen
        schema = dict(node)
        for _ in range(_MAX_SCHEMA_DEPTH):
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                break
            siblings = {key: value for key, value in schema.items() if key != "$ref"}
            if ref in seen:  # recursion: keep the target's shape (type, description) but not its sub-fields
                return {**_shallow(self.pointer(ref)), **siblings}, seen
            seen = seen | {ref}
            target = self.pointer(ref)
            schema = {**target, **siblings} if isinstance(target, Mapping) else siblings
        schema, seen = self._merge_all_of(schema, seen)
        schema, seen = self._unwrap_union(schema, seen)
        return schema, seen

    def pointer(self, ref: str) -> Any:
        """Follow a local JSON pointer such as ``#/components/schemas/Product``."""
        if not ref.startswith("#"):
            raise ToolApiError(f"External $ref {ref!r} is not supported; inline the referenced schema.")
        node: Any = self._document
        for raw_part in ref[1:].lstrip("/").split("/") if ref not in ("#", "#/") else []:
            part = unquote(raw_part).replace("~1", "/").replace("~0", "~")
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise ToolApiError(f"Unresolvable $ref {ref!r} in the tool API.")
        return node

    def _merge_all_of(
        self, schema: dict[str, Any], seen: frozenset[str]
    ) -> tuple[dict[str, Any], frozenset[str]]:
        parts = schema.pop("allOf", None)
        if not isinstance(parts, list):
            return schema, seen
        merged: dict[str, Any] = {}
        for part in parts:
            resolved, seen = self.resolve(part, seen)
            merged = _merge_schemas(merged, resolved)
        return _merge_schemas(merged, schema), seen

    def _unwrap_union(
        self, schema: dict[str, Any], seen: frozenset[str]
    ) -> tuple[dict[str, Any], frozenset[str]]:
        for keyword in ("anyOf", "oneOf"):
            variants = schema.get(keyword)
            if not isinstance(variants, list):
                continue
            resolved = []
            for variant in variants:
                value, seen = self.resolve(variant, seen)
                if _schema_type(value) != "null":
                    resolved.append(value)
            rest = {key: value for key, value in schema.items() if key != keyword}
            if len(resolved) == 1:
                return _merge_schemas(resolved[0], rest), seen
            if resolved and all("enum" in variant for variant in resolved):
                values = [value for variant in resolved for value in variant["enum"]]
                return {**resolved[0], "enum": list(dict.fromkeys(values)), **rest}, seen
            if resolved and all(_schema_type(variant) == "object" for variant in resolved):
                merged: dict[str, Any] = {"type": "object", "properties": {}}
                for variant in resolved:
                    merged["properties"].update(variant.get("properties") or {})
                return {**merged, **rest}, seen
        return schema, seen


def _shallow(target: Any) -> dict[str, Any]:
    """The scalar facts of a schema (type, description, format, enum) without properties or items."""
    if not isinstance(target, Mapping):
        return {}
    stub = {key: target[key] for key in ("type", "description", "title", "format", "enum") if key in target}
    stub.setdefault("type", _schema_type(target))
    return stub


def _merge_schemas(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Merge two object schemas: properties and required lists are united, other keys of ``extra`` win."""
    merged = {**base, **extra}
    properties = {**(base.get("properties") or {}), **(extra.get("properties") or {})}
    if properties:
        merged["properties"] = properties
    required = list(dict.fromkeys([*(base.get("required") or []), *(extra.get("required") or [])]))
    if required:
        merged["required"] = required
    return merged


def _schema_type(schema: Mapping[str, Any]) -> str:
    """The JSON type of a schema, inferred when ``type`` is absent (properties -> object, items -> array, ...)."""
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((item for item in declared if item != "null"), "null" if declared else None)
    if isinstance(declared, str) and declared:
        return declared
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    values = [value for value in schema.get("enum") or [] if value is not None]
    if values:
        sample = values[0]
        if isinstance(sample, bool):
            return "boolean"
        if isinstance(sample, int):
            return "integer"
        if isinstance(sample, float):
            return "number"
    return "string"


def _enum_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _field(name: str, raw_schema: Any, resolver: _Resolver, seen: frozenset[str], depth: int) -> FieldSpec:
    """Build the :class:`FieldSpec` of one property (recursing into objects and arrays of objects)."""
    schema, seen = resolver.resolve(raw_schema, seen)
    kind = _schema_type(schema)
    enum = [_enum_text(value) for value in schema.get("enum") or [] if value is not None] or None
    description = str(schema.get("description") or schema.get("title") or "")
    items: list[FieldSpec] | None = None
    fmt = schema.get("format")
    if depth < _MAX_SCHEMA_DEPTH:
        if kind == "object":
            items = _properties(schema, resolver, seen, depth + 1) or None
        elif kind == "array":
            item_schema, item_seen = resolver.resolve(schema.get("items"), seen)
            if _schema_type(item_schema) == "object":
                items = _properties(item_schema, resolver, item_seen, depth + 1) or None
            else:
                enum = enum or [_enum_text(v) for v in item_schema.get("enum") or [] if v is not None] or None
                fmt = fmt or item_schema.get("format")
                description = description or str(item_schema.get("description") or "")
    fixed: str | None = None
    if kind not in ("object", "array"):
        const, default = schema.get("const"), schema.get("default")
        if const is not None and not isinstance(const, (Mapping, list)):
            fixed = _enum_text(const)
            enum = enum or [fixed]
        elif enum and len(enum) == 1:
            fixed = enum[0]
        elif default is not None and not isinstance(default, (Mapping, list)) and str(default).strip():
            fixed = _enum_text(default)
    return FieldSpec(
        name=name,
        type=kind if kind in _JSON_TYPES else "string",
        enum=enum,
        description=description,
        format=str(fmt) if fmt else None,
        items=items,
        fixed=fixed,
    )


def _properties(
    schema: Mapping[str, Any], resolver: _Resolver, seen: frozenset[str], depth: int
) -> list[FieldSpec]:
    """Field specs of an object schema's properties, with ``required`` set from the schema's required list."""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    required = {str(name) for name in schema.get("required") or []}
    fields = []
    for name, property_schema in properties.items():
        spec = _field(str(name), property_schema, resolver, seen, depth)
        spec.required = str(name) in required
        fields.append(spec)
    return fields


def _object_fields(
    raw_schema: Any, resolver: _Resolver, seen: frozenset[str], depth: int, fallback_name: str
) -> list[FieldSpec]:
    """Top-level fields of a request/response body: its properties, or one ``fallback_name`` field otherwise."""
    schema, seen = resolver.resolve(raw_schema, seen)
    if _schema_type(schema) == "object":
        return _properties(schema, resolver, seen, depth)
    return [_field(fallback_name, schema, resolver, seen, depth)]


__all__ = [
    "EXPECTED_TOOL_NAMES",
    "FieldSpec",
    "ToolApi",
    "ToolApiError",
    "ToolContract",
    "load_tool_api",
    "parse_tool_api",
    "read_tool_api_document",
    "safe_tool_name",
    "with_base_url",
]
