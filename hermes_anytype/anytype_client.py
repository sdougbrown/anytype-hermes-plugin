"""Thin async REST + SSE wrapper over the Anytype local HTTP API.

Built on aiohttp rather than httpx deliberately: aiohttp already ships inside
Hermes's own runtime (see docs/design.md Section 12), so a plugin built on it needs
no extra pip install — important because Hermes's official Docker image
treats its install tree as immutable at runtime (no lazy installs), so any
plugin dependency not already bundled would force every Docker user to build
a derived image just to use this plugin.

Covers the subset of the API this plugin needs (see docs/design.md Section 9):
search, type/property introspection, object writes, and chat. Deliberately
dumb — no caching, no retry-with-backoff here (that's layered on top by
callers per the error-handling table in docs/design.md Section 6).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

ANYTYPE_API_VERSION = "2025-11-08"


class AnytypeAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Anytype API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class AnytypeConfig:
    api_key: str
    base_url: str
    space_id: str


class AnytypeClient:
    """One instance per configured space (see docs/design.md Section 2: single space per config).

    The aiohttp session is created lazily on first use rather than in
    __init__, since __init__ may run outside a running event loop (e.g. at
    plugin registration time).

    This client is long-lived (constructed once, shared across every call
    for the lifetime of the process), but not every caller runs on the same
    event loop. The adapter's SSE/reconnect loop runs directly on the
    gateway's single long-lived loop, but tool-handler calls (tools.py) are
    bridged through Hermes core's model_tools._run_async(), which -- since
    the gateway loop is already running -- spins up a brand-new disposable
    thread with its own brand-new event loop for *each call*, then closes
    that loop when the call returns. A bare `aiohttp.ClientSession` is
    bound to whichever loop it was created on; reusing one from a
    different (and, after the first call, already-closed) loop breaks in
    ways that only surface on the *second* use, with errors like "Timeout
    context manager should be used inside a task" -- confirmed live, beta
    round 19: search_objects (the first tool call) worked, get_type (the
    second) didn't, on an unmodified session-reuse check that only looked
    at `.closed`, never at which loop created it. _ensure_session tracks
    the creating loop and recreates the session whenever the current loop
    doesn't match, rather than assuming any non-closed session is safe to
    reuse.
    """

    def __init__(self, config: AnytypeConfig, *, timeout: float = 30.0) -> None:
        self._config = config
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Anytype-Version": ANYTYPE_API_VERSION,
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            # A session from a different loop can't be safely awaited-closed
            # here -- its loop may already be gone by the time we notice.
            # Just drop the reference; the disposable per-call loop that
            # created it tears its own resources down on close().
            self._session = aiohttp.ClientSession(
                base_url=self._config.base_url.rstrip("/"),
                headers=self._headers(),
                timeout=self._timeout,
            )
            self._session_loop = loop
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "AnytypeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self._ensure_session()
        error: AnytypeAPIError | None = None
        result: Any = None
        async with session.request(method, path, **kwargs) as response:
            body = await response.read()
            if response.status >= 400:
                try:
                    payload = json.loads(body) if body else None
                    message = payload.get("error", body.decode()) if payload else ""
                except (ValueError, UnicodeDecodeError):
                    payload = None
                    message = body.decode(errors="replace")
                error = AnytypeAPIError(response.status, message, payload)
            elif body:
                result = json.loads(body)
        if error is not None:
            raise error
        return result

    # -- Schema introspection --------------------------------------------

    async def list_types(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/types"
        )
        return data.get("data", data)

    async def get_type(self, type_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/types/{type_id}"
        )

    async def list_properties(self, type_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/v1/spaces/{self._config.space_id}/properties"
        params = {"type_id": type_id} if type_id else None
        data = await self._request("GET", path, params=params)
        return data.get("data", data)

    async def get_property(self, property_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/properties/{property_id}"
        )

    # -- Search / objects --------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        types: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"query": query}
        if types:
            body["types"] = types
        if filters:
            body["filters"] = filters
        if sort:
            body["sort"] = sort
        data = await self._request(
            "POST", f"/v1/spaces/{self._config.space_id}/search", json=body
        )
        return data.get("data", data)

    async def create_object(
        self,
        *,
        type_key: str,
        name: str,
        properties: list[dict[str, Any]] | None = None,
        icon: dict[str, Any] | None = None,
        body: str | None = None,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type_key": type_key, "name": name}
        if properties is not None:
            payload["properties"] = properties
        if icon is not None:
            payload["icon"] = icon
        if body is not None:
            payload["body"] = body
        if template_id is not None:
            payload["template_id"] = template_id
        return await self._request(
            "POST", f"/v1/spaces/{self._config.space_id}/objects", json=payload
        )

    async def update_object(
        self, object_id: str, *, properties: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/v1/spaces/{self._config.space_id}/objects/{object_id}",
            json={"properties": properties},
        )

    # -- Chats ---------------------------------------------------------------

    async def list_chats(self) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/v1/spaces/{self._config.space_id}/chats")
        return data.get("data", data)

    async def list_chats_with_discussions(self) -> list[dict[str, Any]]:
        """Chat list including page discussions. Verified live 2026-11-08:
        the v1 chat list omits discussions entirely, but the v2 list with
        include=discussions returns both named chats (kind "chat") and page
        discussion threads (kind "discussion", with parent_id = the page's
        object id). Every per-chat route accepts a discussion id unchanged.
        """
        data = await self._request(
            "GET",
            f"/v2/spaces/{self._config.space_id}/chats",
            params={"include": "discussions"},
        )
        return data.get("data", data.get("chats", data))

    async def add_chat_message(
        self,
        chat_id: str,
        *,
        text: str,
        marks: list[dict[str, Any]] | None = None,
        reply_to_message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if marks is not None:
            payload["marks"] = marks
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if attachments is not None:
            payload["attachments"] = attachments
        return await self._request(
            "POST",
            f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages",
            json=payload,
        )

    async def get_chat_messages(
        self,
        chat_id: str,
        *,
        before_order_id: str | None = None,
        after_order_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if before_order_id is not None:
            params["before_order_id"] = before_order_id
        if after_order_id is not None:
            params["after_order_id"] = after_order_id
        if limit is not None:
            params["limit"] = limit
        # v2, not v1 (confirmed live 2026-11-08): the v1 messages read
        # returns content.text == "" for messages authored by another
        # client in a page discussion -- id/creator/timestamps survive but
        # the body is silently empty, so the bot kept replying "your
        # message came through empty". The v2 read (and v2 stream) decode
        # every author's text correctly, so this reads v2 and normalizes
        # to the v1-ish field layout the adapter was built against. v2
        # inlines formatting into the text ("bo**ld**") rather than
        # exposing a separate marks array; marks are only used for
        # mention detection and this deployment runs with
        # ANYTYPE_REQUIRE_MENTION=false, so an empty marks list loses
        # nothing here (the trigger-string path of should_respond still
        # matches on text).
        data = await self._request(
            "GET",
            f"/v2/spaces/{self._config.space_id}/chats/{chat_id}/messages",
            params=params,
        )
        # v1's response was {"messages": [...]}, unlike every other list
        # endpoint in this API ({"data": [...]}) -- the generic
        # data.get("data", data) fallback used elsewhere silently returned
        # the whole response dict here (no "data" key), and the caller's
        # `for message in messages` iterated over that dict's keys (the
        # single string "messages") instead of message objects, crashing
        # with an unhandled AttributeError on message.get("id") every
        # single time. That crash happened outside any try/except in the
        # call chain, so the _run_chat task died silently on its very
        # first priming call with no exception ever logged anywhere --
        # indistinguishable from a hang. See docs/design.md Section 4.1
        # for the priming step this feeds. v2 uses the same "messages"
        # key; the isinstance filter above keeps that failure mode dead.
        return [
            self._normalize_v2_message(m)
            for m in data.get("messages", [])
            if isinstance(m, dict)
        ]

    @staticmethod
    def _normalize_v2_message(m: dict[str, Any]) -> dict[str, Any]:
        """Map the v2 chat-message shape onto the v1-ish layout the
        adapter and its helpers read: creator/creator_name/creator_id,
        content.text/marks, reply_to_message_id."""
        return {
            "id": m.get("id"),
            "order_id": m.get("order"),
            "creator": m.get("author_id") or m.get("author") or "",
            "creator_name": m.get("author") or "",
            "created_at": m.get("at"),
            "content": {"text": m.get("text") or "", "marks": []},
            "attachments": [
                # v1 reads use {"target": ..., "type": ...}; v2 uses
                # {"id": ..., "type": ...} -- normalize to target so the
                # adapter has one shape to read. NOTE (confirmed live
                # 2026-11-08): the wire "type" is itself unreliable --
                # sending kind "link" reads back as "file", the same
                # unknown-enum-coerces-to-zero fallback as mark types
                # (File=0 in ChatMessage.Attachment.Type) -- so this label
                # is best-effort, never load-bearing.
                {
                    "target": a.get("target") or a.get("id") or "",
                    "type": a.get("type") or a.get("kind") or "",
                }
                for a in (m.get("attachments") or [])
                if isinstance(a, dict)
            ],
            "reactions": m.get("reactions") or {},
            "reply_to_message_id": m.get("reply_to"),
        }

    async def get_object(self, object_id: str) -> dict[str, Any]:
        """Fetch one object by id. Returns the object dict itself (the wire
        envelope is {"object": {...}}), with `type` as a nested dict whose
        `name`/`key` are the human-readable type labels."""
        data = await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/objects/{object_id}"
        )
        return data.get("object", data)

    async def read_chat_messages(
        self, chat_id: str, *, message_type: str = "messages"
    ) -> None:
        await self._request(
            "POST",
            f"/v1/spaces/{self._config.space_id}/chats/{chat_id}/messages/read",
            json={"type": message_type},
        )

    async def list_members(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/v1/spaces/{self._config.space_id}/members"
        )
        return data.get("data", data)

    async def stream_chat_messages(
        self, chat_id: str, *, heartbeat_seconds: int = 30
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields decoded SSE events: {"event": "message_added", "data": {...}}.

        Hand-rolled SSE parsing rather than a dependency like httpx-sse — see
        the module docstring for why this plugin avoids non-bundled deps.
        The format is simple enough not to need a library: ``event:``/``data:``
        lines, blank-line-terminated, ``:``-prefixed comment lines as
        heartbeats. Reconnect/backoff is the adapter's responsibility
        (docs/design.md Section 6) — this just yields events for as long as the
        connection stays open.
        """
        # v2, not v1 (confirmed live 2026-11-08): the v1 stream delivers
        # content.text == "" for messages authored by another client in a
        # page discussion (id/creator survive, body silently empty), so
        # every inbound discussion message looked blank. The v2 stream
        # decodes every author's text. Wire shape differs too: v1 wraps
        # the message as data.payload.message, v2 as data.message.
        path = f"/v2/spaces/{self._config.space_id}/chats/{chat_id}/messages/stream"
        headers = {"Anytype-Heartbeat-Seconds": str(heartbeat_seconds)}
        session = await self._ensure_session()
        # The session's default timeout (self._timeout, `total=30s`, sized
        # for quick REST calls) must NOT apply here -- aiohttp's `total`
        # cancels the whole request, including an open streaming read, once
        # elapsed, regardless of ongoing activity. Confirmed live: with the
        # session default, the SSE stream force-reconnected every ~30s even
        # on a perfectly healthy connection. `total=None` removes that
        # ceiling; `sock_read` instead gives a real dead-connection
        # detector -- no bytes (including heartbeat comment lines) for this
        # long really does mean the connection died. Set well above
        # heartbeat_seconds so healthy heartbeats never trip it.
        stream_timeout = aiohttp.ClientTimeout(
            total=None, connect=10, sock_read=heartbeat_seconds * 3
        )
        async with session.get(path, headers=headers, timeout=stream_timeout) as response:
            response.raise_for_status()
            async for event in parse_sse_lines(response.content):
                # The real wire payload wraps the actual message one level
                # deeper than parse_sse_lines' generic {"event", "data"}
                # envelope: on v1, `data:` is
                # {"type": "message_added", "payload": {"message": {...}}};
                # on v2 it's {"id": ..., "type": "message_added",
                # "message": {...}}. parse_sse_lines is domain-agnostic on
                # purpose (testable against a plain byte iterator, no
                # anytype-specific knowledge), so it can't unwrap this --
                # confirmed live that every consumer downstream was silently
                # getting the wrapper object instead of the real message, so
                # message.get("id")/("content") always returned None/{} and
                # nothing ever matched, with no error anywhere since {} and
                # None are both individually valid, just wrong.
                data = event.get("data")
                if isinstance(data, dict):
                    inner = data.get("payload") or data
                    message = inner.get("message")
                    if message is not None:
                        message = self._normalize_v2_message(message)
                        event = {**event, "data": message}
                yield event


async def parse_sse_lines(lines: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """Parse raw SSE wire lines into {"event": ..., "data": ...} dicts.

    Pulled out of stream_chat_messages so it's testable against a plain
    async iterator of bytes, with no HTTP mocking required.
    """
    event_name = "message"
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if line == "":
            if data_lines:
                raw_data = "\n".join(data_lines)
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError:
                    data = None
                if data is not None:
                    yield {"event": event_name, "data": data}
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            # Confirmed live (beta round 16): yielding a real event here,
            # rather than silently swallowing it, is what lets a consumer
            # actually detect "connection went quiet" -- a dead connection
            # that never raises on either end (see adapter.py's
            # _STREAM_ACTIVITY_TIMEOUT) is otherwise indistinguishable from
            # a healthy one with nothing new to report.
            yield {"event": "heartbeat", "data": None}
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
