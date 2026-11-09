"""LLM-visible tools: search_objects, create_object, update_object, get_type.

Property validation for writes happens here, invisible to the LLM's context
(design.md Section 4.2) — an HTTP round-trip inside the handler, not a tool call, so
a bad property key costs latency, not a confusing raw API error surfaced to
the model.
"""

from __future__ import annotations

import json
from typing import Any

from .anytype_client import AnytypeClient


class PropertyValidationError(ValueError):
    """Raised when the LLM-supplied properties don't match the type's schema.

    Handlers should catch this and return the message as a corrective
    tool-result error rather than letting it kill the turn (design.md Section 6).
    """


async def _validate_properties(
    client: AnytypeClient, type_key: str, properties: dict[str, Any]
) -> list[dict[str, Any]]:
    """Look up the type's real property keys and normalize the LLM's input
    into the typed link-value shape the API expects (design.md Section 9).

    Raises PropertyValidationError with a corrective message on the first
    unknown key, naming what's available so the LLM can retry itself.
    """
    type_info = await client.get_type(type_key)
    known = await client.list_properties(type_id=type_info.get("id", type_key))
    by_key = {prop["key"]: prop for prop in known}

    normalized: list[dict[str, Any]] = []
    for key, value in properties.items():
        prop = by_key.get(key)
        if prop is None:
            available = ", ".join(sorted(by_key)) or "(none defined)"
            raise PropertyValidationError(
                f"'{key}' not found on type '{type_key}'. Available: {available}."
            )
        normalized.append(_to_link_value(prop, value))
    return normalized


def _to_link_value(prop: dict[str, Any], value: Any) -> dict[str, Any]:
    """Wrap a raw value in the {key, <format>} shape create-object expects."""
    fmt = prop["format"]
    field_by_format = {
        "text": "text",
        "number": "number",
        "select": "select",
        "multi_select": "multi_select",
        "date": "date",
        "files": "files",
        "checkbox": "checkbox",
        "url": "url",
        "email": "email",
        "phone": "phone",
        "objects": "objects",
    }
    field = field_by_format.get(fmt, fmt)
    return {"key": prop["key"], field: value}


async def search_objects(
    client: AnytypeClient, query: str, types: list[str] | None = None
) -> list[dict[str, Any]]:
    return await client.search(query, types=types)


async def get_type(client: AnytypeClient, type_key: str) -> dict[str, Any]:
    return await client.get_type(type_key)


async def create_object(
    client: AnytypeClient,
    type_key: str,
    name: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = await _validate_properties(client, type_key, properties or {})
    return await client.create_object(type_key=type_key, name=name, properties=normalized)


async def update_object(
    client: AnytypeClient,
    object_id: str,
    type_key: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    normalized = await _validate_properties(client, type_key, properties)
    return await client.update_object(object_id, properties=normalized)


# ---------------------------------------------------------------------------
# Hermes tool registration -- schemas + handler(args, **kwargs) wrappers
#
# Confirmed against tools/registry.py: registry.dispatch() calls
# `entry.handler(args, **kwargs)` where `args` is the LLM-supplied argument
# dict matching the JSON schema below (docs/design.md Section 12).
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "anytype_search_objects": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text search query."},
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of type keys to narrow the search to.",
            },
        },
        "required": ["query"],
    },
    "anytype_get_type": {
        "type": "object",
        "properties": {
            "type_key": {"type": "string", "description": "The type's key, e.g. 'task'."},
        },
        "required": ["type_key"],
    },
    "anytype_create_object": {
        "type": "object",
        "properties": {
            "type_key": {"type": "string", "description": "The object's type key, e.g. 'task'."},
            "name": {"type": "string", "description": "The object's title."},
            "properties": {
                "type": "object",
                "description": "Property key -> raw value. Validated and normalized "
                "server-side against the type's real schema before writing.",
            },
        },
        "required": ["type_key", "name"],
    },
    "anytype_update_object": {
        "type": "object",
        "properties": {
            "object_id": {"type": "string", "description": "The object to update."},
            "type_key": {"type": "string", "description": "The object's type key."},
            "properties": {
                "type": "object",
                "description": "Property key -> raw value to set.",
            },
        },
        "required": ["object_id", "type_key", "properties"],
    },
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "anytype_search_objects": "Search the Anytype space's objects by free-text query, "
    "optionally narrowed to specific type keys.",
    "anytype_get_type": "Look up an Anytype type's schema (properties, format, layout) "
    "for a given type key.",
    "anytype_create_object": "Create a new object in the Anytype space. Property keys "
    "are validated against the type's real schema before writing.",
    "anytype_update_object": "Update an existing Anytype object's properties. Property "
    "keys are validated against the type's real schema before writing.",
}


def make_tool_handlers(client: AnytypeClient) -> dict[str, Any]:
    """Build the args-dict handler closures ctx.register_tool() expects,
    bound to a single shared AnytypeClient."""

    # Confirmed live (beta round 15): Hermes's tool dispatch
    # (tools/registry.py's _normalize_handler_result) only accepts a plain
    # str result (or the {"_multimodal": True, "content": [...]} envelope,
    # not used here) -- anything else, including a bare dict, is rejected
    # outright with "Tool handler returned unsupported result type: dict"
    # before the LLM ever sees it. Every handler below now returns
    # json.dumps(...) instead of the raw dict.

    async def _search_objects(args: dict[str, Any], **_kwargs: Any) -> str:
        results = await search_objects(client, args["query"], args.get("types"))
        return json.dumps({"results": results})

    async def _get_type(args: dict[str, Any], **_kwargs: Any) -> str:
        return json.dumps(await get_type(client, args["type_key"]))

    async def _create_object(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            result = await create_object(
                client, args["type_key"], args["name"], args.get("properties")
            )
        except PropertyValidationError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result)

    async def _update_object(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            result = await update_object(
                client, args["object_id"], args["type_key"], args["properties"]
            )
        except PropertyValidationError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result)

    return {
        "anytype_search_objects": _search_objects,
        "anytype_get_type": _get_type,
        "anytype_create_object": _create_object,
        "anytype_update_object": _update_object,
    }
