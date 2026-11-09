"""AnytypeClient tests against a real local aiohttp.web server (via
aiohttp.test_utils), not a third-party HTTP-mocking library.

aioresponses was tried first but monkeypatches aiohttp's ClientResponse
constructor, which breaks on every aiohttp release that touches that
signature (it already had on the aiohttp version this repo pins). Driving
requests through a real, tiny aiohttp server instead only depends on
aiohttp's own stable public API, so it can't lag behind aiohttp itself.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hermes_anytype.anytype_client import (
    AnytypeAPIError,
    AnytypeClient,
    AnytypeConfig,
    parse_sse_lines,
)


class FakeAnytypeServer:
    """Records requests and serves pre-registered canned responses."""

    def __init__(self):
        self.requests: list[dict] = []
        self._responses: dict[tuple[str, str], tuple[int, dict]] = {}
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._app = app
        self._server: TestServer | None = None
        self.base_url = ""

    def set_response(self, method: str, path: str, *, status: int = 200, payload: dict) -> None:
        self._responses[(method.upper(), path)] = (status, payload)

    async def _handle(self, request: web.Request) -> web.Response:
        raw_body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": dict(request.headers),
                "body": json.loads(raw_body) if raw_body else None,
            }
        )
        key = (request.method, request.path)
        if key not in self._responses:
            return web.json_response({"error": f"no mock for {key}"}, status=500)
        status, payload = self._responses[key]
        return web.json_response(payload, status=status)

    async def start(self) -> None:
        self._server = TestServer(self._app)
        await self._server.start_server()
        self.base_url = str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()


@pytest.fixture
async def fake_server():
    server = FakeAnytypeServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def client(fake_server):
    config = AnytypeConfig(api_key="test-key", base_url=fake_server.base_url, space_id="space1")
    async with AnytypeClient(config) as c:
        yield c


async def test_search_sends_query_and_returns_data(client, fake_server):
    fake_server.set_response(
        "POST", "/v1/spaces/space1/search", payload={"data": [{"id": "obj1"}]}
    )

    results = await client.search("roadmap", types=["task"])

    assert results == [{"id": "obj1"}]
    request = fake_server.requests[0]
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["headers"]["Anytype-Version"] == "2025-11-08"
    assert request["body"] == {"query": "roadmap", "types": ["task"]}


async def test_create_object_posts_expected_payload(client, fake_server):
    fake_server.set_response(
        "POST",
        "/v1/spaces/space1/objects",
        payload={"id": "obj1", "name": "Ship the thing"},
    )

    result = await client.create_object(type_key="task", name="Ship the thing")

    assert result["id"] == "obj1"
    assert fake_server.requests[0]["body"] == {"type_key": "task", "name": "Ship the thing"}


async def test_raises_on_error_status(client, fake_server):
    fake_server.set_response(
        "GET",
        "/v1/spaces/space1/types/bogus",
        status=404,
        payload={"error": "type not found"},
    )

    with pytest.raises(AnytypeAPIError) as exc_info:
        await client.get_type("bogus")

    assert exc_info.value.status_code == 404
    assert "type not found" in str(exc_info.value)


async def test_add_chat_message_includes_reply_to(client, fake_server):
    fake_server.set_response(
        "POST", "/v1/spaces/space1/chats/chat1/messages", payload={"id": "msg2"}
    )

    await client.add_chat_message("chat1", text="hi", reply_to_message_id="msg1")

    assert fake_server.requests[0]["body"] == {"text": "hi", "reply_to_message_id": "msg1"}


async def test_get_chat_messages_unwraps_messages_key(client, fake_server):
    """Regression test for a real bug found live: the chat-messages list
    response shape is {"messages": [...]}, unlike every other list endpoint
    in this API ({"data": [...]}). The generic data.get("data", data) fallback
    used elsewhere silently returned the whole response dict here (no "data"
    key present), and the caller's `for message in messages` then iterated
    over that dict's keys -- the single string "messages" -- crashing with
    an unhandled AttributeError on message.get("id") every single call.
    That crash happened outside any try/except in the call chain, so the
    adapter's per-chat task died silently on its very first priming call
    with no exception ever logged -- indistinguishable from a hang.

    Reads v2 (see the client comment for why v1 is a dead end for other
    authors' discussion messages) and normalizes v2 messages to the v1-ish
    shape the adapter reads.
    """
    fake_server.set_response(
        "GET",
        "/v2/spaces/space1/chats/chat1/messages",
        payload={
            "messages": [
                {
                    "id": "msg1",
                    "order": "!!1",
                    "author": "Doug Brown",
                    "author_id": "participant_1",
                    "at": "2026-11-09T02:55:56Z",
                    "text": "Targets are mostly D",
                    "reply_to": "msg0",
                },
                {"id": "msg2"},
            ]
        },
    )

    messages = await client.get_chat_messages("chat1")

    assert messages == [
        {
            "id": "msg1",
            "order_id": "!!1",
            "creator": "participant_1",
            "creator_name": "Doug Brown",
            "created_at": "2026-11-09T02:55:56Z",
            "content": {"text": "Targets are mostly D", "marks": []},
            "attachments": [],
            "reactions": {},
            "reply_to_message_id": "msg0",
        },
        {
            "id": "msg2",
            "order_id": None,
            "creator": "",
            "creator_name": "",
            "created_at": None,
            "content": {"text": "", "marks": []},
            "attachments": [],
            "reactions": {},
            "reply_to_message_id": None,
        },
    ]


def test_ensure_session_recreates_when_the_running_loop_changes():
    """Regression test for a real bug found live (beta round 19): tool
    handlers (tools.py) are dispatched through Hermes core's
    model_tools._run_async(), which -- since the gateway's own loop is
    already running -- spins up a brand-new disposable event loop for
    *each call* and closes it when the call returns. This client is a
    long-lived singleton shared across every call, so _ensure_session()
    was reusing the same aiohttp.ClientSession (only checking `.closed`,
    never which loop created it) across calls that actually ran on
    different, and after the first call already-dead, event loops. The
    first tool call worked (session freshly bound to that call's loop);
    every call after it broke with aiohttp errors like "Timeout context
    manager should be used inside a task" -- confirmed live: search_objects
    (first) succeeded, get_type (second) didn't. A bare aiohttp session is
    loop-bound; _ensure_session must recreate it whenever the current loop
    doesn't match the loop it was created on, not just when it's closed.

    Drives two independent event loops directly (the same pattern
    model_tools._run_async uses) rather than relying on pytest-asyncio's
    single shared test loop, which would never reproduce this. The fake
    server runs on its own persistently-running background-thread loop
    (via run_forever) so it keeps accepting connections from whichever
    client-side loop is calling it -- run_until_complete(server.start())
    alone would leave the server's loop idle the instant start() returns,
    with nothing left pumping it to accept the client's real connections.
    """
    import threading

    server = FakeAnytypeServer()
    server_loop = asyncio.new_event_loop()
    server_thread = threading.Thread(target=server_loop.run_forever, daemon=True)
    server_thread.start()
    asyncio.run_coroutine_threadsafe(server.start(), server_loop).result()

    config = AnytypeConfig(api_key="test-key", base_url=server.base_url, space_id="space1")
    client = AnytypeClient(config)
    server.set_response("GET", "/v1/spaces/space1/chats", payload={"data": []})

    try:
        loop_one = asyncio.new_event_loop()
        session_one = loop_one.run_until_complete(client._ensure_session())
        result_one = loop_one.run_until_complete(client.list_chats())
        loop_one.close()

        loop_two = asyncio.new_event_loop()
        session_two = loop_two.run_until_complete(client._ensure_session())
        result_two = loop_two.run_until_complete(client.list_chats())
        loop_two.close()
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), server_loop).result()
        server_loop.call_soon_threadsafe(server_loop.stop)
        server_thread.join(timeout=5)
        server_loop.close()

    assert result_one == []
    assert result_two == []
    assert session_one is not session_two


async def test_stream_chat_messages_overrides_session_default_timeout(fake_server, mocker):
    """Regression test for a real bug found live: the session's default
    `total=30s` timeout (sized for quick REST calls) was being inherited by
    the SSE stream too, force-cancelling a perfectly healthy connection
    every ~30s regardless of activity. stream_chat_messages must pass its
    own per-call timeout override with no `total` ceiling."""
    fake_server.set_response(
        "GET", "/v2/spaces/space1/chats/chat1/messages/stream", payload={}
    )
    config = AnytypeConfig(api_key="test-key", base_url=fake_server.base_url, space_id="space1")
    async with AnytypeClient(config) as client:
        # Session is created lazily; force creation first so there's a real
        # bound session to spy on. `wraps=` keeps the request going through
        # to the real (fake) server instead of re-implementing it here.
        session = await client._ensure_session()
        get_spy = mocker.patch.object(session, "get", wraps=session.get)

        try:
            async for _event in client.stream_chat_messages("chat1"):
                break  # fake_server's JSON response isn't valid SSE; loop just needs to not hang
        except Exception:
            pass  # only the call arguments matter for this test, not a full successful stream

        assert get_spy.call_count == 1
        _args, kwargs = get_spy.call_args
        timeout = kwargs.get("timeout")
        assert timeout is not None, "stream_chat_messages must pass an explicit timeout override"
        assert timeout.total is None, "a `total` timeout would cancel a long-lived SSE stream"
        assert timeout.sock_read is not None and timeout.sock_read > 30, (
            "sock_read should be well above the heartbeat interval, not left unset"
        )


# -- SSE parsing (no HTTP mocking needed -- see anytype_client.py) ----------


async def _lines(*raw: str):
    for line in raw:
        yield line.encode()


async def test_parse_sse_lines_yields_event_and_data():
    events = [
        event
        async for event in parse_sse_lines(
            _lines(
                "event: message_added",
                'data: {"id": "msg1", "text": "hi"}',
                "",
            )
        )
    ]
    assert events == [{"event": "message_added", "data": {"id": "msg1", "text": "hi"}}]


async def test_parse_sse_lines_defaults_event_name_to_message():
    events = [
        event
        async for event in parse_sse_lines(_lines('data: {"id": "msg1"}', ""))
    ]
    assert events == [{"event": "message", "data": {"id": "msg1"}}]


async def test_parse_sse_lines_yields_heartbeat_comments_as_real_events():
    # Confirmed live (beta round 16): silently swallowing heartbeats (the
    # old behavior) made a dead connection indistinguishable from a
    # healthy one with nothing new to report -- adapter.py's activity
    # watchdog needs a real event for every heartbeat to detect genuine
    # silence.
    events = [
        event
        async for event in parse_sse_lines(
            _lines(":heartbeat", 'data: {"id": "msg1"}', "")
        )
    ]
    assert events == [
        {"event": "heartbeat", "data": None},
        {"event": "message", "data": {"id": "msg1"}},
    ]


async def test_parse_sse_lines_skips_malformed_json():
    events = [
        event
        async for event in parse_sse_lines(_lines("data: not json", ""))
    ]
    assert events == []


async def test_parse_sse_lines_joins_multiline_data():
    events = [
        event
        async for event in parse_sse_lines(
            _lines("data: {\"id\":", 'data: "msg1"}', "")
        )
    ]
    assert events == [{"event": "message", "data": {"id": "msg1"}}]
