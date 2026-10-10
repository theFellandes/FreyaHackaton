"""Offline, credential-safe views of request traces and Freya engine events."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***"
MAX_TRACE_BYTES = 2 * 1024 * 1024
_ENGINE_EVENTS = frozenset(
    {"node_enter", "function_call_start", "function_call_complete", "route_evaluated", "error"}
)
_SAFE_HEADERS = frozenset(
    {
        "accept",
        "acceptencoding",
        "acceptlanguage",
        "contenttype",
        "contentlength",
        "useragent",
        "host",
        "cachecontrol",
        "pragma",
        "date",
        "connection",
        "xrequestid",
        "traceparent",
    }
)
_TOKEN_METRICS = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "totaltokens",
        "prompttokens",
        "completiontokens",
        "maxtokens",
        "maxoutputtokens",
        "maxcompletiontokens",
        "cachedtokens",
        "reasoningtokens",
        "tokenusage",
        "tokencount",
    }
)
_CREDENTIAL_SHAPES = (
    re.compile(r"\b(?:sk-ant-|sk-|fak_)[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._\-~+/=]+"),
)
_URL_TEXT = re.compile(r"""https?://[^\s<>"']+|(?<![\w])(?:/|\?)[^\s<>"']*""", re.IGNORECASE)
# A "name": value / name: value / name=value pair. The name is either quoted (optionally with backslash-escaped
# quotes, as inside embedded JSON) or a bare token; ``_mask_value`` then finds where the value ends.
_PAIR_START = re.compile(
    r"""(?:(?P<q>\\*["'])(?P<quoted>[^"'\\\r\n]{1,64})(?P=q)|(?<![\w.\-])(?P<bare>[A-Za-z][\w.\-]{0,63}))\s*[:=]\s*"""
)
_HEADER_BLOCK = re.compile(
    r"""(?:(?P<q>\\*["'])[^"'\\\r\n]{0,48}headers(?P=q)|(?<![\w.\-])[\w.\-]{0,48}headers)\s*[:=]\s*(?=\{)""",
    re.IGNORECASE,
)
_QUOTE = re.compile(r"""(\\*)(["'])""")
_AUTH_SCHEME = re.compile(r"(?i)(?:Bearer|Basic|Digest|Token|Negotiate|ApiKey)\s+")
_BARE_VALUE = re.compile(r"""[^\s,;&}\]"']*""")
_KEY_SUFFIX = re.compile(r"[-_.][Kk][Ee][Yy]$|(?<=[a-z0-9])Key$")


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    if normalized in _TOKEN_METRICS:
        return False
    return (
        normalized in {"key", "apikey", "xteamkey", "teamkey", "auth", "authorization", "proxyauthorization"}
        or _KEY_SUFFIX.search(key.strip()) is not None
        or any(part in normalized for part in ("password", "passwd", "secret", "credential", "cookie"))
        or normalized.endswith(
            ("token", "tokens", "apikey", "accesskey", "privatekey", "authkey", "authtoken")
        )
        or normalized.startswith(("apikey", "authorization", "authentication", "token"))
    )


def _bracket_end(text: str, pos: int) -> int:
    """Index just past the bracketed value that starts at ``pos``; the end of the text if it is unbalanced."""
    depth = 0
    in_string = ""
    index = pos
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == in_string:
                in_string = ""
        elif char in "\"'":
            in_string = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _mask_value(text: str, pos: int, *, json_like: bool) -> tuple[str, int]:
    """Replacement for the value that starts at ``pos`` and the index where it ends.

    A value that is cut off (an unterminated string or bracket) is masked up to the end of the text, so
    truncated or malformed documents cannot leak the part that was written.
    """
    quote = _QUOTE.match(text, pos)
    if quote:
        delimiter = quote.group(1) + quote.group(2)
        index = quote.end()
        end = -1
        if quote.group(1):
            end = text.find(delimiter, index)
        else:
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote.group(2):
                    end = index
                    break
                index += 1
        if end < 0:
            return delimiter + REDACTED, len(text)
        return delimiter + REDACTED + delimiter, end + len(delimiter)
    wrapped = f'"{REDACTED}"' if json_like else REDACTED
    if text.startswith(("{", "["), pos):
        return wrapped, _bracket_end(text, pos)
    scheme = _AUTH_SCHEME.match(text, pos)
    run = _BARE_VALUE.match(text, scheme.end() if scheme else pos)
    end = run.end() if run else pos
    if end == pos:
        return "", pos
    return wrapped, end


def _is_member_name(text: str, pos: int) -> bool:
    """True if the text just before ``pos`` is an object start or a comma (so a name there is a member key)."""
    pos -= 1
    while pos >= 0 and text[pos].isspace():
        pos -= 1
    return pos < 0 or text[pos] in "{,"


def _mask_pairs(text: str, is_secret: Callable[[str, bool], bool], *, members_only: bool = False) -> str:
    """Mask the value of every ``name: value`` / ``"name": value`` / ``name=value`` pair that ``is_secret``.

    Works on raw text, so it does not depend on the text being valid JSON. With ``members_only`` a pair counts
    only where it is an object member (after ``{`` or ``,``), not when it appears inside another value.
    """
    out: list[str] = []
    copied = 0
    scan = 0
    while match := _PAIR_START.search(text, scan):
        scan = match.end()
        quoted = match.group("quoted") is not None
        name = match.group("quoted") if quoted else match.group("bare")
        if not is_secret(name, quoted) or (members_only and not _is_member_name(text, match.start())):
            continue
        replacement, end = _mask_value(text, match.end(), json_like=quoted)
        if end == match.end():
            continue
        # Plain prose such as "total tokens: 17" is a usage figure, not a credential.
        if not quoted and text[match.end() : end].isdigit() and _normalized_key(name).endswith("tokens"):
            continue
        out.append(text[copied : match.end()])
        out.append(replacement)
        copied = scan = end
    out.append(text[copied:])
    return "".join(out)


def _credential_name(name: str, quoted: bool) -> bool:
    # A bare "key: value" in prose ("missing key: customer_id") is usually not a credential; a quoted
    # "key" member of a JSON object is.
    return _sensitive_key(name) and (quoted or _normalized_key(name) != "key")


def _custom_header(name: str, quoted: bool) -> bool:
    return _normalized_key(name) not in _SAFE_HEADERS


def _mask_header_blocks(text: str) -> str:
    """Mask every non-allowlisted header inside a ``headers: {...}`` block, even if the block is cut off."""
    out: list[str] = []
    copied = 0
    scan = 0
    while match := _HEADER_BLOCK.search(text, scan):
        start = match.end()
        end = _bracket_end(text, start)
        scan = max(end, start + 1)
        out.append(text[copied:start])
        out.append(_mask_pairs(text[start:end], _custom_header, members_only=True))
        copied = end
    out.append(text[copied:])
    return "".join(out)


def is_failed_attempt(row: Mapping[str, Any]) -> bool:
    """True for a request or model attempt that failed: ``ok`` is false or the HTTP status is 400 or above.

    A legacy record may carry only a status, or only ``ok``. The counters and the "errors only" table filter
    both use this one predicate, so they can never disagree about which attempts failed.
    """
    status = _number(row.get("status"))
    return row.get("ok") is False or (status is not None and status >= 400)


def needs_attention(row: Mapping[str, Any]) -> bool:
    """True for a failed attempt or one that scheduled a retry (the monitor's "errors and retries" filter)."""
    details = row.get("details")
    retry = details.get("retry_scheduled") if isinstance(details, Mapping) else None
    return is_failed_attempt(row) or retry is True or (isinstance(retry, str) and retry.lower() == "true")


def redact_data(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a JSON-safe copy with credentials masked, including inside JSON strings.

    Customer and workflow identifiers remain visible for diagnosis. Header values are kept only for an
    allowlist of ordinary HTTP metadata: arbitrary custom headers may contain unknown credentials.

    Every string is masked as raw text first: known secret values, key-shaped tokens, ``name: value`` pairs
    whose name looks like a credential, and the members of ``headers`` blocks. Only then is a JSON-looking
    string parsed for structure-aware masking, so a document that cannot be parsed (truncated, malformed) is
    still masked.
    """
    known = sorted(
        {secret for secret in secrets if isinstance(secret, str) and secret}, key=len, reverse=True
    )

    def url_text(match: re.Match[str]) -> str:
        value = match.group()
        try:
            parts = urlsplit(value)
            query = parse_qsl(parts.query, keep_blank_values=True)
            if any(_sensitive_key(key) for key, _ in query) or "@" in parts.netloc:
                query = [(key, REDACTED if _sensitive_key(key) else item) for key, item in query]
                netloc = parts.netloc.rsplit("@", 1)[-1]
                value = urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))
        except ValueError:
            pass
        return value

    def raw(value: str) -> str:
        """Mask credentials in text that may be truncated or malformed. Valid JSON stays valid JSON."""
        for secret in known:
            value = value.replace(secret, REDACTED)
        for pattern in _CREDENTIAL_SHAPES:
            value = pattern.sub(REDACTED, value)
        return _mask_pairs(_mask_header_blocks(value), _credential_name)

    def text(value: str) -> str:
        # Uploaded/legacy errors may embed URLs in prose, rather than storing a separate URL field.
        return _URL_TEXT.sub(url_text, raw(value))

    def clean(value: Any, depth: int = 0, *, headers: bool = False) -> Any:
        if depth > 32:
            return "<nested too deep>"
        if isinstance(value, Mapping):
            return {
                text(str(key)): (
                    REDACTED
                    if _sensitive_key(str(key))
                    or (headers and _normalized_key(str(key)) not in _SAFE_HEADERS)
                    else clean(item, depth + 1, headers=_normalized_key(str(key)).endswith("headers"))
                )
                for key, item in value.items()
            }
        if isinstance(value, str):
            masked = raw(value)
            stripped = masked.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    # Not parseable (truncated or malformed): the raw masking above is all that can be applied.
                    return REDACTED if headers else _URL_TEXT.sub(url_text, masked)
                except (ValueError, RecursionError):
                    # Resource-limit failures leave a JSON document that cannot be safely inspected.
                    return "<unreadable JSON>"
                else:
                    return json.dumps(clean(parsed, depth + 1, headers=headers), ensure_ascii=False)
            return REDACTED if headers else _URL_TEXT.sub(url_text, masked)
        if isinstance(value, list | tuple):
            return [clean(item, depth + 1, headers=headers) for item in value]
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, bytes | bytearray):
            return f"<{len(value)} bytes>"
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return clean(dump(mode="json"), depth + 1, headers=headers)
        return text(str(value))

    return clean(value)


def _object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def events_from_call(call: Mapping[str, Any], secrets: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Normalize engine events and conversation snapshots without fetching anything.

    ``details`` retains routes, request arguments, tool results, and variable snapshots after redaction.
    Invalid messages are skipped, so one unusual event cannot hide the rest of a call.
    """
    if not isinstance(call, Mapping):
        return []
    payload = call
    for _ in range(3):
        if isinstance(payload.get("messages"), list):
            break
        nested = payload.get("call", payload.get("data"))
        if not isinstance(nested, Mapping):
            return []
        payload = nested
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    rows: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        elapsed = _number(message.get("secondsFromStart", message.get("seconds_from_start")))
        metadata = _object(message.get("metadata"))
        node = message.get("nodeId", message.get("node_id"))
        role = str(message.get("role", "")).lower()
        if role == "system":
            event = _object(message.get("message", message.get("content")))
            name = event.get("event", event.get("type", event.get("kind")))
            if not isinstance(name, str) or name not in _ENGINE_EVENTS:
                continue
            data = _object(event.get("data"))
            function = event.get("function", data.get("function"))
            if isinstance(function, Mapping):
                function = function.get("name", function.get("id"))
            details = dict(event)
            rows.append(
                {
                    "elapsed_s": elapsed,
                    "node": node or event.get("node") or data.get("node") or event.get("from_node"),
                    "event": name,
                    "function": function,
                    "details": details,
                }
            )
        elif role in {"user", "assistant"}:
            snapshot = _object(message.get("workflowSnapshot", metadata.get("workflowSnapshot")))
            if snapshot:
                rows.append(
                    {
                        "elapsed_s": elapsed,
                        "node": node or snapshot.get("currentNode"),
                        "event": "workflow_snapshot",
                        "function": None,
                        "details": {"role": role, "workflowSnapshot": dict(snapshot)},
                    }
                )
    return redact_data(rows, secrets)


@dataclass
class TraceRead:
    """Bounded JSONL read, with safe diagnostics that never echo malformed source text."""

    records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    partial_line: bool = False
    truncated: bool = False
    bytes_read: int = 0


def _incomplete_json(exc: json.JSONDecodeError, value: str) -> bool:
    return (
        exc.pos >= len(value.rstrip())
        or exc.msg.startswith("Unterminated string")
        or (
            not value.rstrip().endswith(("}", "]"))
            and value[exc.pos :].strip() in {"t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul"}
        )
    )


def read_trace(path: Path | str, max_bytes: int = MAX_TRACE_BYTES, secrets: Iterable[str] = ()) -> TraceRead:
    """Read at most ``max_bytes`` from a trace, tolerating an incomplete last write.

    Complete malformed lines are reported by line number. Reading a live trace never requires the writer's
    lock. A size limit keeps the playground responsive and reports when later records were omitted.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    # Iterators must apply to every line, rather than being consumed by the first redaction.
    secrets = tuple(secrets)
    result = TraceRead()
    try:
        with Path(path).open("rb") as handle:
            content = handle.read(max_bytes + 1)
    except OSError as exc:
        result.errors.append(f"Could not read trace ({type(exc).__name__}).")
        return result
    result.truncated = len(content) > max_bytes
    content = content[:max_bytes]
    result.bytes_read = len(content)
    lines = content.splitlines(keepends=True)
    for index, raw_line in enumerate(lines, start=1):
        terminated = raw_line.endswith((b"\n", b"\r"))
        final = index == len(lines)
        if result.truncated and final and not terminated:
            result.partial_line = True
            break
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError:
            if final and not terminated:
                result.partial_line = True
            else:
                result.errors.append(f"Line {index}: invalid UTF-8.")
            continue
        if index == 1:
            line = line.lstrip("\ufeff")
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if final and not terminated and _incomplete_json(exc, line):
                result.partial_line = True
            else:
                result.errors.append(f"Line {index}: invalid JSON.")
            continue
        except ValueError:
            # Python's integer-digit limit can reject valid JSON before a record is constructed.
            result.errors.append(f"Line {index}: invalid JSON.")
            continue
        except RecursionError:
            result.errors.append(f"Line {index}: JSON nesting is too deep.")
            continue
        if not isinstance(record, dict):
            result.errors.append(f"Line {index}: expected a JSON object.")
            continue
        result.records.append(redact_data(record, secrets))
    return result


def _records(records: Iterable[Mapping[str, Any]] | TraceRead) -> Iterable[Mapping[str, Any]]:
    return records.records if isinstance(records, TraceRead) else records


def trace_rows(
    records: Iterable[Mapping[str, Any]] | TraceRead, secrets: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Flatten request and model attempts for the monitor's table."""
    rows: list[dict[str, Any]] = []
    for record in _records(records):
        if not isinstance(record, Mapping):
            continue
        kind = record.get("kind")
        data = _object(record.get("data"))
        # Older Freya-only trace sinks wrap request metadata in a freya event.
        if kind == "freya" and data.get("action") == "request":
            kind = "request"
        if not isinstance(kind, str) or kind not in {"request", "model_call"}:
            continue
        latency = _number(data.get("latency_s"))
        if latency is None:
            duration_ms = _number(data.get("duration_ms"))
            latency = duration_ms / 1000 if duration_ms is not None else _number(data.get("duration_s"))
        rows.append(
            {
                "timestamp": record.get("ts"),
                "kind": kind,
                "service": data.get("service", data.get("provider")),
                "scope": data.get("scope"),
                "action": data.get("operation", data.get("action", data.get("purpose"))),
                "method": data.get("method"),
                "path": data.get("path"),
                "model": data.get("model"),
                "ok": data.get("ok"),
                "status": data.get("status"),
                "latency_s": latency,
                "attempt": data.get("attempt"),
                "request_id": data.get("request_id"),
                "workflow_id": data.get("workflow_id"),
                "call_id": data.get("call_id"),
                "error": data.get("error", data.get("error_kind", data.get("error_type"))),
                "input_tokens": data.get("input_tokens"),
                "output_tokens": data.get("output_tokens"),
                "details": dict(data),
            }
        )
    return redact_data(rows, secrets)


def trace_summary(records: Iterable[Mapping[str, Any]] | TraceRead) -> dict[str, int | float]:
    """Count attempts, failures, latency, and provider-reported token usage."""
    rows = trace_rows(records)
    requests = [row for row in rows if row["kind"] == "request"]
    models = [row for row in rows if row["kind"] == "model_call"]

    return {
        "requests": len(requests),
        "request_failures": sum(is_failed_attempt(row) for row in requests),
        "model_calls": len(models),
        "model_failures": sum(is_failed_attempt(row) for row in models),
        "input_tokens": sum(int(_number(row["input_tokens"]) or 0) for row in models),
        "output_tokens": sum(int(_number(row["output_tokens"]) or 0) for row in models),
        "latency_s": round(sum(_number(row["latency_s"]) or 0 for row in rows), 3),
    }
