# hermes-anytype — Design & Roadmap

**Status:** Implemented against Hermes's confirmed real plugin API (verified against `NousResearch/hermes-agent`'s own shipped Mattermost/IRC plugins — see Section 12). Not yet run against a live Anytype instance.
**License:** MIT
**Repo name:** `hermes-anytype`

## 1. What this is

A Hermes plugin that gives Hermes (a) a live chat presence inside a self-hosted Anytype space, and (b) tools to search/create/update that space's typed objects — both driven by live schema introspection against the Anytype API, so it works against anyone's custom type setup (Person/Company/Task, or anything else) with zero manual mapping. Meant to be a genuine open-source community project, not a personal script: real README, clear config, sane defaults, MIT license. Scope is intentionally bounded rather than exhaustive — see the scope-cutoff calls in Section 6 and Section 12, and the decision not to cross-reference adjacent community projects in Section 4.2.

### Origin / motivation

Built out of a real use case: tracking a multi-stage pipeline of custom object types (e.g. Company → Role → Interview) in place of a hand-maintained markdown dashboard, and wanting an agent that can both talk about that data in Anytype's native chat and keep it organized on your behalf — the same pattern Hermes already applies to other personal knowledge-base platforms via its plugin ecosystem, just extended to Anytype's typed-object model. The live-introspection design (Section 2) means this works against *any* type schema, not just the one that motivated it — Person/Company/Task, or anything else a user has set up.

## 2. Scope decisions (settled)

These were deliberately chosen over alternatives during design — don't re-litigate without new information:

- **Chat gateway + object/schema tools, bundled in one plugin package.** Not split across two projects, and not dependent on the separate official `anyproto/anytype-mcp` server — this plugin is self-contained.
- **Real OSS polish from day one** — license, README, config validation with clear errors, basic tests. Built so a stranger can self-host it without asking questions.
- **Live introspection, no persisted schema cache.** No config file mapping types → properties that could go stale after a user edits their schema. (See Section 5 for how this is kept cheap despite being "live every time.")
- **Ships as a genuine Hermes platform-adapter plugin** (`ctx.register_platform()`), not a standalone bridge service. Requires a running Hermes instance; in exchange it gets Hermes's existing tool loop, memory, and retry handling for free. Confirmed Hermes plugins can register a platform adapter *and* tools from the same package in one `register(ctx)` call.
- **Single Anytype space per config.** Multiple spaces = multiple config blocks, not a list-of-spaces schema. Keeps the config and mental model simple.
- **Per-chat response mode, not global.** Each `chat_id` in the space gets its own setting: `mention` (only responds when addressed, e.g. `@hermes`) or `all` (responds to everything). Default is `mention`. This maps directly onto real usage patterns: a solo second-brain space sets its one chat to `all`; a team space sets a shared room to `mention` and a dedicated help room to `all`. Confirmed Anytype has no separate DM primitive — a space just has one or more `chat_id`s (`list-chats`/`create-chat`), so this per-chat config is the only lever needed.
- **Free writes by default.** The agent creates/updates objects autonomously when it decides to — no opt-in write-permission gate for v1. Revisit if real-world usage shows this needs a confirmation step for destructive edits.
- **Naming:** `hermes-anytype`.

## 3. Architecture

One Python package, following Hermes's own real plugin conventions (confirmed against `plugins/platforms/mattermost/` and `plugins/platforms/irc/` in `NousResearch/hermes-agent` — see Section 12):

```
hermes_anytype/
├── plugin.yaml
├── __init__.py          # register(ctx) — registers platform + tools (Hermes-agnostic import, defers adapter import)
├── adapter.py            # AnytypeAdapter(BasePlatformAdapter) — SSE listen, reply post
├── env_config.py          # pure env/mention-filter logic, deliberately free of any Hermes import (so it's unit-testable standalone)
├── tools.py               # search_objects / create_object / update_object / get_type + Hermes handler wrappers
└── anytype_client.py      # thin REST + SSE wrapper over the Anytype API, built on aiohttp (already bundled with Hermes — see its docstring)
```

`register(ctx)` calls both `ctx.register_platform(name="anytype", ...)` and `ctx.register_tool(...)` for each tool — one package, two registration surfaces, confirmed real (not guessed) against `hermes_cli/plugins.py`'s `PluginContext.register_platform`/`register_tool`.

### Config shape

Confirmed real: Hermes's platform plugins are configured via env vars (`plugin.yaml`'s `requires_env`/`optional_env`), not a nested custom YAML block — the original guess below was wrong in shape, right in spirit. **Caught by real-world install feedback:** `plugin.yaml` also needs a `provides_tools` list naming every tool the package registers (see `plugins/platforms/a2a/plugin.yaml` for the confirmed real pattern) — without it, `hermes doctor` warns, and in some deferred-loading contexts (CLI/TUI, not just the gateway process) the tools may never register at all. Missing this wasn't caught by anything short of an actual install attempt.

```bash
ANYTYPE_API_KEY=...
ANYTYPE_API_BASE_URL=http://127.0.0.1:31012   # Hermes's own bot identity's node, not the user's desktop app
ANYTYPE_SPACE_ID=...

# optional
ANYTYPE_CHATS=                    # comma-separated chat_ids; blank = auto-discover all
ANYTYPE_REQUIRE_MENTION=true
ANYTYPE_MENTION_TRIGGER=@hermes
ANYTYPE_FREE_RESPONSE_CHATS=      # comma-separated chat_ids that ignore REQUIRE_MENTION (mirrors Mattermost's MATTERMOST_FREE_RESPONSE_CHANNELS)
```

## 4. Components

### 4.1 Adapter (`adapter.py`)

`AnytypeAdapter` subclasses `gateway.platforms.base.BasePlatformAdapter`, confirmed against the real base class and the shipped Mattermost/IRC adapters. Only `connect()`, `disconnect()`, and `send()` are actually `@abstractmethod` on the base class; `send_typing()`/`get_chat_info()` are implemented too for good citizenship (the former is a no-op — Anytype's chat API has no typing-indicator endpoint).

- Third-party plugins were never added to Hermes core's `Platform` enum, but that enum has a `_missing_()` hook that creates a dynamic pseudo-member for any name already registered in `platform_registry` — so `Platform("anytype")` works in `__init__` for the same reason `Platform("irc")` works in the real IRC plugin: `register_platform(name="anytype", ...)` has already run by the time the adapter is constructed.
- `connect()` spawns one background `asyncio.Task` per configured chat (from `ANYTYPE_CHATS`, or auto-discovered via `list-chats` if unset), each holding an SSE stream (`GET /v1/spaces/{space_id}/chats/{chat_id}/messages/stream`) with exponential-backoff reconnect on drop.
- On `message_added`: mention/reply-all filtering is a real substring match (`env_config.should_respond`) — structural mention tracking via Anytype's `TextMark` shape was flagged as unverified against a running instance and was never confirmed, so the substring fallback described in the original design is what actually shipped, not a stopgap.
- Matching messages become a `MessageEvent` (built via `self.build_source(...)`) and get handed to Hermes's own turn pipeline via `self.handle_message(event)` — Hermes owns session/auth/turn plumbing entirely past that point.
- Replies go out via `send()` → `POST /v1/spaces/{space_id}/chats/{chat_id}/messages`, using `reply_to_message_id` for threading. Sent message IDs are tracked in a bounded cache so the adapter can filter its own messages back out of the SSE stream (avoids a reply loop) without needing to resolve "my own member ID" from the API.

**Beta round 10 (2026-08-17): first live plugin test finds a real code bug — the SSE stream reconnecting every ~30s.** With a real `ANYTYPE_API_KEY` in hand for the first time (round 9), the plugin ran inside real Hermes for the first time too. REST calls (auth, space listing, chat auto-discovery) all worked cleanly — but the SSE stream kept dropping and reconnecting roughly every 30-33 seconds regardless of connection health. Root cause, precisely isolated: `AnytypeClient.__init__` sets a session-wide `aiohttp.ClientTimeout(total=30.0)` sized for quick REST calls, and `stream_chat_messages()` was reusing that same session without overriding it for the long-lived streaming request. aiohttp's `total` cancels the *entire* request — including an open streaming read — once elapsed, regardless of ongoing activity (heartbeats included). Not a network problem at all; the client was cancelling itself on a schedule. Fixed in `anytype_client.py`: the streaming call now passes its own per-request `ClientTimeout(total=None, connect=10, sock_read=heartbeat_seconds * 3)` — no blanket ceiling, but a real dead-connection detector (`sock_read`, set well above the heartbeat cadence so healthy heartbeats never trip it). Added a regression test (`test_stream_chat_messages_overrides_session_default_timeout`) asserting the streaming call's timeout has `total=None` and a `sock_read` above 30s, so this can't silently regress. **Still open:** the fix hasn't been re-verified live yet — this round confirmed the diagnosis and shipped a fix, not a confirmed-working stream.

**Beta round 11 (2026-08-17): the SSE fix held, but messages still got no reply — two real bugs found, both previously flagged as open questions.** The stream stopped reconnecting on schedule (round 10's fix confirmed working), but a test message got no response. Root cause, captured from the actual raw SSE payload:

```json
"content": {
    "text": "Untitled sup?",
    "marks": [{"from": 0, "to": 8, "type": "mention", "param": "..._A9GoTfqnjjBe2HzAMVcLvorSXG4ebPxSnQqfh3FdLLBD4SAQ"}]
}
```

Two distinct bugs, both real, both previously called out as open questions rather than assumed solved:

1. **A real UI @mention's visible text is the mentioned party's *display name*, not the trigger string.** Typing `@hermes` and picking the bot from the UI's autocomplete produces literal text `"Untitled sup?"` (the bot's still-unset display name — see the still-open "Untitled" question below) plus a separate structural `{"type": "mention", "param": <identity>}` mark referencing the bot's real account id — the `param` suffix here is an exact match for the account id confirmed in beta round 9. This is exactly what this doc flagged as unverified all the way back in the original design ("prefer Anytype's structural mention tracking if the message/mark format exposes it... verify empirically against a running instance before committing to structural-only detection") — now confirmed, and confirmed that the substring-only fallback silently misses it. Fixed: `env_config.is_mentioned()` checks the `marks` array for a `type: "mention"` entry whose `param` contains the bot's own account id (new `ANYTYPE_ACCOUNT_ID` config, optional but required for this to work); `should_respond()` now checks trigger-substring OR structural mention, so plain-text "@hermes" typers and real UI-mention users both work.
2. **A second, more fundamental bug, found underneath the first:** `adapter.py`'s message handling read `message.get("text")` directly — but the real payload nests both `text` and `marks` under a `content` key. This wasn't a mention-detection-specific bug — it meant message text extraction was silently broken for *every* message in *every* mode, including reply-all, since `message.get("text")` on a payload with no top-level `text` field just returns `""`. Fixed alongside the mention-detection change: `content = message.get("content") or {}`, then `text`/`marks` read from that.

Both changes covered by new unit tests (`is_mentioned`, `should_respond` with the real captured mark shape) in `tests/test_env_config.py`. **Still open:** live re-verification that a real UI @mention now gets a reply, now that `ANYTYPE_ACCOUNT_ID` needs to actually be set for the fix to take effect — it wasn't configured during round 11's test, since the need for it wasn't known yet.

**Beta round 12 (2026-08-17): with `ANYTYPE_ACCOUNT_ID` set, mentions matched — and immediately crashed on the very next line.** `_handle_anytype_message`'s author parsing assumed `message["creator"]` was a nested object (`author.get("id")`, `author.get("name")`), but the real payload has `creator` as a plain participant-id *string*, plus a separate flat `creator_name` field the old code never read at all. Calling `.get("id")` on a string raises `AttributeError`. This code path only runs after `should_respond()` returns `True` — before round 11's mention-detection fix, nothing ever matched, so the crash was unreachable and invisible; the moment mentions started correctly matching, every single one hit this line.

One genuinely useful side effect of chasing this down: an exception raised inside `_handle_anytype_message` was propagating all the way out through the `async for` loop in `_run_chat`, tearing down the *entire* SSE connection and forcing a full reconnect — for every message, not just failing that one message. Fixed alongside the parsing bug: `_handle_anytype_message` calls are now wrapped in their own try/except inside the loop, logged individually (`"failed to handle message %s in chat %s"`) without taking down the stream. A single message that hits some future unanticipated shape should fail loudly and specifically now, not silently nuke the whole connection.

Fix: extracted `env_config.parse_message_author()` (a pure, testable function, same pattern as `is_mentioned`/`should_respond`) reading `creator`/`creator_id`/`creator_name` as flat fields, replacing the nested-dict assumption entirely. Covered by new unit tests using the real confirmed field shape. **Still open:** live re-verification that a real UI @mention now actually reaches `handle_message()` and produces a reply — round 12 confirmed the crash and shipped the fix, not a working end-to-end reply yet.

**Beta round 13 (2026-08-17): the plugin's own logic verified correct in isolation against real live payloads — the actual blocker turned out to be outside this project entirely.** With the round 12 fix deployed, still no reply, and — notably — the new per-message error handler didn't fire either, which should log unconditionally on any failure in that code path. That absence pointed upstream of the handler, not inside it.

Two genuinely new findings, both confirmed by bypassing Hermes and talking to the raw Anytype SSE stream directly:

- **The plugin's mention-detection and author-parsing logic is correct.** Tested in isolation against the exact real payloads for two live test messages — both correctly identified as matches. Not a logic bug.
- **A confirmed, real, previously-undocumented API behavior: a fresh SSE connection replays a chat's entire message history as `message_added` events, not just messages that arrive after connecting** — an isolated test observed all 8 of a chat's historical messages delivered immediately on connect. The adapter had no deduplication for already-processed *incoming* message ids (only for its own outgoing replies), so every reconnect — not just the first-ever connection — would silently reprocess the whole backlog. This also corrects a wrong claim earlier in this doc (Section 6): reconnecting to a fresh stream does **not** avoid a full history replay, contrary to what was assumed when the reconnect logic was first written.

  Fixed: `_run_chat` now primes a bounded seen-message-id set from the chat's current history (`get_chat_messages`, capped at 200) before ever opening the live stream, and every incoming message id gets remembered as seen the moment it's evaluated — whether it matched anything or not — so a later replay of the same message (from any future reconnect, not just the first connection) is skipped. No new pure-testable logic here (unlike `is_mentioned`/`parse_message_author`) — priming is inherently coupled to the live API client and instance state, so this is one more thing that can only be verified live, not in this repo's own test suite.

- **The actual reason nothing was replying is not this plugin at all.** Across multiple gateway restarts, the *entire* Hermes gateway — Discord, Telegram, Email, not just Anytype — went completely silent for minutes at a time shortly after startup, with no corresponding error; one instance needed a hard `SIGKILL` to clear. A Discord rate-limit backoff log line appeared right before each silence, suggesting (not confirmed) a blocking sleep somewhere in Hermes core freezing the single-threaded event loop for every platform at once rather than just Discord's — but the timing didn't fully hold up under a direct check (silence persisted well past when that specific backoff window should have cleared), so that's a lead, not a confirmed cause. **This is explicitly out of scope for `hermes-anytype`** — it's a Hermes-core issue, in a codebase this project doesn't own or modify, and continuing to restart-and-retest against a gateway that keeps hanging wasn't going to produce a reliable answer about the Anytype plugin either way. Correctly flagged back rather than chased further. A live stack trace (`py-spy` or similar) would likely be the next real diagnostic step, but that's a call for whoever owns that gateway process to make, not something to install into a running container unprompted.

**Net status:** as far as this repository's own code goes, everything currently testable in isolation has been verified correct — mention detection, author parsing, and now message deduplication. The plugin has not yet been confirmed to produce an actual end-to-end reply, but that's now blocked on a separate, external availability issue, not on anything in `hermes_anytype`.

### 4.2 Tools (`tools.py` + `anytype_client.py`)

Registered as `anytype_search_objects` / `anytype_get_type` / `anytype_create_object` / `anytype_update_object` via `ctx.register_tool(name, toolset, schema, handler, is_async=True, ...)` — confirmed real against `hermes_cli/plugins.py`. Hermes's tool registry dispatches with `handler(args: dict, **kwargs) -> dict`, so each tool's core logic (Hermes-agnostic, independently testable) is wrapped in a thin `handler(args, **kwargs)` closure in `tools.py`'s `make_tool_handlers()`.

- `search_objects(query, types?)` — wraps `POST /v1/spaces/{space_id}/search` (the `types` filter narrows to specific type keys; `type_key` values for the tool's own schema enum are refreshed once per conversation, not per message, not cached to disk).
- `create_object(type_key, name, properties)` / `update_object(object_id, properties)` — **the key correctness design**: the handler internally calls `get-type` / `list-properties` itself, before ever submitting to Anytype, to validate and normalize the `properties` keys the LLM supplied. This lookup is invisible to the LLM's context (an HTTP round-trip inside the handler, not a tool call) — it costs latency, not tokens. If a property key doesn't match, the wrapper catches `PropertyValidationError` and returns `{"error": "'assignee' not found on type 'task'. Available: assigned_to, due_date, status."}` as the tool result instead of forwarding a bad write or raising an exception that kills the turn.
- `get_type(type_key)` — optional, LLM-visible, for cases where the agent wants to explain a schema conversationally. Not required for the write path's correctness (that's handled server-side per above).

**Scope note:** a third-party Hermes skill, `clawhub/anytype` (`Foolafroos/anytype-hermes-skill`), already wraps the official `anyproto/anytype-mcp` server for pull-based object CRUD — meaningful overlap with this section. Deliberately kept independent rather than deferring to it: this plugin doesn't depend on Node.js or a second MCP server process, and the validation design above is more correctness-focused than a generic MCP passthrough. No cross-reference to that skill is maintained in this repo's docs — keeping it in sync would mean ongoing tracking of an external project's changes, which is out of scope here.

This design was chosen specifically to balance two competing concerns raised during brainstorming: **context/token cost** (don't dump full schema into every turn or require a visible discover-then-retry tool-call dance) vs. **risk of the LLM guessing a wrong property key** (don't let bad guesses silently reach Anytype or surface as confusing raw API errors). Pushing validation into the handler, invisible to context, resolves both.

## 5. Data flow

```
Anytype SSE event (message_added)
  → gateway filters by that chat's mode/mention config
  → Hermes turn starts, message as input
  → Hermes's reasoning loop optionally calls tools
      (create_object/update_object validate server-side before writing)
  → Hermes produces reply text
  → gateway posts reply via add-chat-message, threaded via reply_to_message_id
```

## 6. Error handling

| Failure | Behavior |
|---|---|
| SSE disconnect (network blip, Anytype restart) | Exponential-backoff reconnect. **Correction (beta round 13, Section 4.1): the claim that reconnecting to a fresh live stream avoids a full history replay was wrong** -- confirmed live, a fresh SSE connection replays the chat's entire message history as `message_added` events, every time, not just genuinely new messages. Guarded against with an ID-based seen-message dedup set, primed from the chat's current history before the stream ever opens (Section 4.1) -- not a fix for the replay itself, just for the adapter reprocessing it. Cursor-based gapless resume (skipping the replay at the source, via `after_order_id`) remains a real, not-implemented enhancement (Section 12). |
| Bad/revoked API key | Fail loud at plugin startup with a clear config error — never silently no-op |
| Property validation mismatch on write | Corrective tool-result error back to the LLM (see Section 4.2), not an exception |
| Anytype 5xx / rate limit | Bounded retry-with-backoff in `anytype_client.py`, not infinite |

## 7. Testing

- Unit tests for `anytype_client.py` (mocked HTTP) — the REST+SSE wrapper.
- Unit tests for the property-validation/normalization logic in the tool handlers — this is the correctness-critical piece.
- Unit tests for per-chat mention/reply-all filtering logic in the gateway.
- A real self-hosted Anytype instance in CI is likely impractical (needs a running server) — rely on thorough mocking, plus a documented manual verification checklist in the README for contributors rather than fabricated integration coverage.

## 8. Distribution

- MIT license.
- Confirmed real (not a guess): Hermes's official Docker image (`nousresearch/hermes-agent:latest`) mounts a single host directory → container `/opt/data`, which already covers `plugins/` alongside `skills/`/`sessions/`/etc — so dropping this plugin into `<host-dir>/plugins/` needs zero compose changes. **Caught by real-world install feedback:** the host-side path is whatever the operator's own compose file specifies for that mount — Hermes's own docs example uses `~/.hermes`, but that's just an example, not a guarantee, and this README originally (wrongly) assumed it as fact. Fixed to say "check your own compose file's volume line" instead of asserting a specific path. The image treats its install tree as immutable at runtime ("no lazy installs"), which is *why* `anytype_client.py` is built on `aiohttp` (already bundled with Hermes core) instead of `httpx`/`httpx-sse`: a plugin with its own extra pip dependency would force every Docker user to build and maintain a derived image just to use it. Directory-drop plugins (this one) also need no pip-install step at all, even outside Docker, since Hermes imports the plugin directory directly rather than via a package manager.
- README covers: prerequisites (running Hermes + Anytype with local API access — self-hosting the sync backend specifically is optional, orthogonal, and not required), install (drop-in, no pip step), the real env-var config from Section 3, and mention-mode vs. reply-all framing from Section 2.
- **Two more gaps caught by the same real-world beta pass, both about install mechanics rather than the plugin's actual code:**
  - Plugin loading is opt-in by default (`config.yaml`'s `plugins.enabled: []`, confirmed from `hermes_cli/plugins.py`) — dropping the directory in isn't sufficient on its own; `hermes plugins enable anytype-platform` is a required step the README originally omitted. Also worth calling out on its own: the name that command expects is `anytype-platform` (`plugin.yaml`'s `name:` field), not `hermes_anytype` (the directory/package name) — a real point of confusion since they don't match.
  - The README's original `cp .env.example .env` instruction is actively dangerous on a live instance — `<HERMES_HOME>/.env` already holds unrelated secrets for anyone with other plugins installed, and that command overwrites the whole file rather than merging. Hermes's own installer (`hermes plugins install` / `hermes plugins enable`, which drives `hermes_cli/plugins_cmd.py`'s `_prompt_plugin_env_vars` → `save_env_value()`) already does this safely, per-key, off the same `requires_env`/`optional_env` block in `plugin.yaml` — now the README's primary documented path, with manual `.env` editing demoted to an alternative that explicitly warns to merge, not overwrite.

## 9. Technical reference (verified against the live Anytype API spec, 2025-11-08 version)

Captured here so implementation doesn't have to re-derive this from scratch.

**Property (relation) formats** — `create-property` request schema, `format` enum:
`text, number, select, multi_select, date, files, checkbox, url, email, phone, objects`.
`objects` is the object-to-object relation type — its value is a list of other objects' IDs (`ObjectsPropertyLinkValue`: `{key, objects: [id, ...]}`). This is the mechanism for typed relations (e.g. a Role's `company` property, a Person's `mother`/`siblings`).

**Object creation** — `create-object` takes `type_key`, `name`, `icon`, `body`, `template_id`, and a `properties` array of typed link-value objects (`TextPropertyLinkValue`, `NumberPropertyLinkValue`, `SelectPropertyLinkValue`, `MultiSelectPropertyLinkValue`, `DatePropertyLinkValue`, `FilesPropertyLinkValue`, `ObjectsPropertyLinkValue`, etc. — one `oneOf` variant per format).

**Schema introspection** — `list-types` / `get-type` (`GET /v1/spaces/{space_id}/types/{type_id}`) return a type's key, name, icon, layout — explicitly documented as existing to "assist clients in understanding the expected structure... for objects of that type." `list-properties` / `get-property` do the equivalent for relations.

**Search** — `search-space` (`POST /v1/spaces/{space_id}/search`) takes `query`, `filters`, `sort`, and a `types` array to filter results by type key.

**Chat endpoints**, all under `/v1/spaces/{space_id}/chats/{chat_id}/`:
- `list-chats` / `create-chat` — a space can have multiple chats; no separate DM primitive exists.
- `POST /messages` (add-chat-message) — body: `text` (required), `marks` (rich-text formatting spans), `reply_to_message_id` (threading), `attachments` (can target an object ID directly — a message can carry a real embedded object reference, not just a link).
- `GET /messages/stream` (chat-message-stream) — Server-Sent Events: `message_added`, `message_updated`, `message_deleted`, `reactions_updated`. Heartbeat comment lines keep the connection alive; tunable via `Anytype-Heartbeat-Seconds` header (1–60s, default 30s).
- `GET /messages` (get-chat-messages) — cursor-based pagination via `before_order_id`/`after_order_id`.
- `GET /messages/search` (search-chat-messages) — full-text search over chat history.
- `POST /messages/read` (read-chat-messages) — marks messages read; `type` param defaults to `"messages"`, can be set to `"mentions"` specifically — confirms Anytype tracks @-mentions as a first-class read-state, not just a text convention. The exact `TextMark` type string for a mention wasn't pinned down as a strict enum in the spec — verify empirically.
- `list-members` / `get-member` — space membership, useful for resolving a human display name to a member ID if needed.

**Auth / connection:** API key generated in Anytype's app settings (API Keys section); default local base URL `http://127.0.0.1:31009` (or `31012` for `anytype-cli`). MCP/API version pinned via an `Anytype-Version` header (e.g. `2025-11-08`).

## 10. Hermes's Anytype identity (bot account setup)

Open question from Section 9 that turned out to matter architecturally: **Anytype has no bot-token concept.** Unlike Slack/Discord, there's no way to mint a credential scoped *inside* an existing account. Every space participant is a full any-sync identity (own keypair, own local node). The REST API key from the challenge flow (`POST /v1/auth/challenges` → `POST /v1/auth/api_keys`) just authenticates to whichever account is running the local node at the base URL you point at — it does not create an identity. **If `api_key` is minted against the user's own desktop Anytype app, every Hermes message posts under the human's own member profile, not "Hermes."**

To give Hermes a distinct profile, it needs its own account, running its own node, invited into the space as its own member. `anyproto/anytype-cli` is built for exactly this ("bot accounts") — it embeds `anytype-heart` headless, no desktop app required. Shipped as [`docker-compose.anytype-bot.yml`](../docker-compose.anytype-bot.yml) at the repo root, using the official `ghcr.io/anyproto/anytype-cli` image — the bootstrap-then-serve pattern in that file is confirmed against `anyproto/any-sync-dockercompose`'s own (optional, commented-out) `anytype-cli` service block, not invented:

1. `docker compose -f docker-compose.anytype-bot.yml up -d` — the bootstrap service runs the CLI headless just long enough to `auth create hermes` (mints a new account key; account-key auth only, no mnemonic login) if `/root/.anytype/config.json` doesn't already exist, then the main service takes over with `restart: unless-stopped`. That file is for the default Anytype Network case; if self-hosting your own any-sync backend instead, don't use it — uncomment the equivalent block already in your `any-sync-dockercompose` checkout so the bot shares your `network.yml` and actually lands on your network (an unrelated bot account with no `--network-config` pointed at the same deployment lands on an unreachable network).
2. **Immediately back up the account recovery key, then delete it from the container** — `docker compose ... exec anytype-cli cat /root/.anytype/ACCOUNT_RECOVERY_KEY.txt`, copy it into a password manager, then `docker compose ... exec anytype-cli rm /root/.anytype/ACCOUNT_RECOVERY_KEY.txt`. This key is the *only* way to recover this identity if the `anytype-cli-data` volume is ever lost — treat it like a wallet seed phrase, not a config value.
3. `docker compose ... exec anytype-cli anytype auth apikey create hermes-integration` — mints the API key scoped to *this* identity/node. This is `ANYTYPE_API_KEY`; `ANYTYPE_API_BASE_URL` is this container's published port (`http://127.0.0.1:31012` by default).
4. From the human's desktop app: Share Space → generate an invite link (approval-required by default; owner can enable auto-approve but Anytype's own docs advise against it for sensitive spaces).
5. `docker compose ... exec anytype-cli anytype space join "<invite-link>"`, then the human approves the join request in-app. Will likely need explicit promotion to Editor role to match the "free writes by default" decision in Section 2 — default post-join role isn't confirmed.

**Caught by real-world beta feedback (a from-scratch install attempt against a live Docker Hermes stack):** the compose file and this section originally used `/app/anytype` as the binary path in both the bootstrap entrypoint and these exec commands. Wrong — `/app` is just the image's empty `WorkingDir`; the actual binary is `/usr/local/bin/anytype`, already on `PATH` via the image's own `ENTRYPOINT`, so the right invocation is the bare `anytype` command. Compounding it: the bootstrap script swallowed the resulting failure (`|| true` / `exit 0` regardless), so `docker compose up -d` reported success, the main container came up "healthy" (the healthcheck only checked that the port was open, not that an account existed), and the empty-account failure was invisible short of reading `docker logs` directly. Fixed in both places: the bootstrap script now propagates a real failure (blocking the main container from ever starting via `depends_on: condition: service_completed_successfully`), and the healthcheck now also asserts `/root/.anytype/config.json` exists, not just that the port answers.

**Caught by a second round of the same beta feedback:** `auth create`'s own output — which includes the account's recovery key in plaintext — was left to flow into the bootstrap container's stdout, which `docker logs` captures. That's a far more widely readable, longer-retained surface than a wallet-seed-phrase-equivalent secret should ever land on (log aggregators, anyone with `docker logs` access, or just accidentally pasted into a chat/ticket while debugging — which is what actually happened). Fixed: that output is now redirected to `ACCOUNT_RECOVERY_KEY.txt` on the persistent volume instead (`chmod 600`), never touching stdout/logs; step 2 above was added so the key still gets extracted and backed up rather than silently orphaned in the volume. Also switched the bootstrap script from a single `entrypoint: /bin/sh -c "..."` scalar (which needs two correctly-nested layers of escaping — Compose's own `${...}` interpolation *and* shell quoting — to round-trip, and wasn't independently verifiable without Docker itself available to check against) to list-form `entrypoint`/`command`, which sidesteps that ambiguity entirely.

**Beta round 2 (2026-08-16, same-day retest): confirmed the path-bug fix works, hit a new blocker one step later.** After the fix above, `anytype auth create` now genuinely succeeds — a real `config.json` lands in the volume, the healthcheck passes for real. But the *main* `anytype-cli` container then fails to auto-login using that freshly-created identity:

```
Failed to auto-login with account key after 3 attempts: wallet recovery failed: rpc error: code = DeadlineExceeded desc = context deadline exceeded
authorize: rpc error: code = Unauthenticated desc = parse token: signature is invalid
```

...and `auth apikey create` subsequently fails the same way (`Unauthenticated: parse token: signature is invalid`). This is a live any-sync networking/protocol issue, not a documentation or shell-scripting bug — the kind of thing that genuinely needs an actual Docker host and live network access to iterate on, which isn't available to whoever's editing this doc from a plain source checkout. What the beta pass ruled out: TCP/443 reachability to the any-sync coordinator (confirmed fine, both raw IP and hostname), a stale/wrong image (same `latest` digest across both containers in the same test run), and firewall-blocks-everything (ruled out) — VPN interference (a `gluetun` container elsewhere on the same host) looked unlikely but wasn't ruled out with full certainty.

**Leading hypothesis, unconfirmed:** both containers log a QUIC buffer-size warning at startup (`failed to sufficiently increase receive buffer size (was: 208 kiB, wanted: 7168 kiB, got: 416 kiB)`), meaning any-sync uses QUIC/UDP for at least part of its protocol — only TCP reachability was verified above, so a UDP-path problem (or the OS-level receive-buffer ceiling the warning itself names) is the strongest untested lead. That ceiling (`net.core.rmem_max`/`net.core.wmem_max`) is a host-wide Linux kernel parameter, not something a container can raise for itself — if you want to try this, it has to happen on the Docker *host*, not in `docker-compose.anytype-bot.yml`:

```bash
sudo sysctl -w net.core.rmem_max=7500000
sudo sysctl -w net.core.wmem_max=7500000
# persist across reboots:
echo 'net.core.rmem_max=7500000' | sudo tee /etc/sysctl.d/99-anysync-quic.conf
echo 'net.core.wmem_max=7500000' | sudo tee -a /etc/sysctl.d/99-anysync-quic.conf
sudo sysctl --system
```

Two low-confidence mitigations landed alongside this note (docker-compose.anytype-bot.yml): the settle time between `auth create` succeeding and killing the temporary bootstrap server was bumped from 2s to 5s (in case the identity needs more time to propagate to the coordinator before the process holding that session is torn down), and a comment recommending pinning `ANYTYPE_CLI_VERSION` instead of trusting `latest` long-term, for reproducibility. Neither is a confirmed fix — flagged as such in the file itself. **Status: this now blocks reaching a live Anytype instance entirely, one step further than round 1's path bug but still before `ANYTYPE_API_KEY` exists.** Needs the sysctl experiment (or other live network debugging) run against the real environment to make further progress.

**Beta round 3 (2026-08-16, same-day retest): settle-time bump made no difference (as flagged it might not); sysctl experiment blocked by environment, not declined.** Same `DeadlineExceeded` failure, byte-for-byte, confirming the 2s→5s change wasn't the fix (expected, given how it was flagged above). More useful finding: the test host runs **Docker Desktop for Mac**, not a bare Linux host — so `sudo sysctl -w net.core.rmem_max=...` isn't reachable the normal way; Docker Desktop's VM isn't `ssh`-able, and applying a sysctl change there would mean a shared, host-wide change affecting every other container on that machine, not a scoped one. Correctly paused rather than making that change unprompted.

This actually strengthens the QUIC/UDP hypothesis rather than dead-ending it: Docker Desktop for Mac's user-space networking layer has long-standing, independently-documented UDP/QUIC reliability problems — this is corroborating evidence from an unrelated source, not just this one warning line (see the round 4 correction below on exactly which backend/citation applies). No client-side "prefer TCP" flag for `anytype-cli`/`anytype-heart` is documented anywhere findable (checked `any-sync-dockercompose`'s own Configuration wiki, which only covers self-hosted coordinator/node-side transport config, not client-side) — and self-hosting isn't the setup being tested here anyway (this is the default Anytype Network case), so there's no coordinator config to change even if there were a documented knob.

**Beta round 4 (2026-08-16): native test confirms Docker is the actual variable; further digging narrows down which Docker layer, and finds a real stay-in-Docker experiment.** Running `anytype-cli` natively on the same Mac (same OS, same network, same any-sync backend, outside Docker entirely) succeeded cleanly — `auth create` in under a second, no `DeadlineExceeded`, no QUIC buffer warning at all — while the still-running Docker container hit the identical round 2/3 failure concurrently, side by side. About as clean a controlled comparison as this gets.

Given other VPN/proxy layers already exist on this particular host, several more specific explanations for *why* Docker's networking breaks this were ruled out rather than accepting "it's just Docker Desktop" at face value:
- **gluetun** (a VPN container used by other services on this host) — `anytype-cli` sits on its own plain bridge network, not `network_mode: service:gluetun`; confirmed only two unrelated containers actually ride gluetun's network stack.
- **Tailscale, containerized** — separate network namespace, no path overlap.
- **Tailscale, host-level** — the one real lead here: a host-level Tailscale daemon had installed a route via a low-MTU (1280) interface. Checked whether it was acting as an exit node (which would tunnel *all* host/Docker-VM traffic through that constrained path) — `tailscale status --json` confirms `ExitNodeID: False`, so it only routes tailnet-range traffic, not everything. Ruled out with a real check, not assumed.
- **MTU mismatch** generally — host and Docker bridge interfaces both report a clean 1500, no mismatch.
- **DNS** — resolves correctly and quickly from inside the container.
- **Caddy** — no anytype-related config anywhere across this host's Caddyfiles.

**Correction to the round 2/3 citation above:** it names `vpnkit` specifically — wrong for this host. `vpnkit` isn't even running here; this Docker Desktop (4.86.0) is configured with the newer **gVisor** network backend (`networkType: gvisor`, the default since Docker Desktop 4.19), a different codebase with its own separate history of UDP/QUIC issues (`docker/desktop-feedback#133`, among others). "User-space network emulation instead of native kernel networking" is still the right general theory and still matches everything observed — the specific backend named just needs to be gVisor, not vpnkit, on this host.

**New, genuinely promising, stays-100%-in-Docker experiment:** Docker Desktop's network backend is switchable — `gvisor` (current default) vs. the older `vpnkit` — via one setting in `~/Library/Group Containers/group.com.docker/settings-store.json` (`"networkType": "vpnkit"`, then quit and restart Docker Desktop). Since gVisor and vpnkit have *different*, not necessarily equally bad, UDP/QUIC quirks — Docker's move to gVisor in 4.19 was for performance, not because vpnkit was universally worse — this is worth trying before concluding Docker Desktop can't run this at all. Fully reversible, no shared-VM access needed, no separate host process.

**Net status after round 4:** all other plausible explanations on this host (gluetun, Tailscale exit-node routing, MTU, DNS, Caddy) are now ruled out rather than merely untested, which if anything makes the underlying "Docker Desktop's user-space networking" theory more likely, not less.

**Beta round 5 (2026-08-16): the failure boundary narrows to one specific RPC, and the actual root cause is found.** Before trying the vpnkit-backend switch, the tester ruled out the remaining generic suspects (no third-party firewall/monitoring tool installed; `daemon.json` has no custom DNS/MTU/proxy config; the Docker Desktop VM is nowhere near resource-starved) and then built a real QUIC version-negotiation probe rather than continuing to infer UDP behavior from log lines. Findings:

- Generic UDP round-trips are **not** broken by Docker's network stack — the probe succeeds in ~13ms against Google's public QUIC endpoint, from inside a container on the same network as anytype-cli.
- A **fresh** `auth create` (brand-new identity, no prior state) succeeds in Docker in ~1 second. This directly contradicts "any-sync over QUIC just doesn't work in this Docker setup" — it does, for account creation.
- The failure is specifically in **recovering an existing stored account** — a different RPC than creation. Reproduced cleanly and repeatedly on a controlled identity: create succeeds, then a fresh container's auto-login against that same stored key hits the exact `DeadlineExceeded` from rounds 2/3.
- The receive-buffer warning is present in both the successful and failing runs, confirming it's a real difference from native (which shows none) but not sufficient alone to explain the failure — a contributing factor under heavier load, not the whole story.

This lines up with a genuine root cause found by reading the actual source (docs and web search had nothing — same pattern as finding Hermes's real plugin API earlier in this project): account **creation** and account **recovery/auto-login** are different RPCs in anytype-heart, and recovery has to pull back an existing account's history — a heavier, more timing-sensitive transfer than a brand-new account's near-empty initial sync. Traced three repos to confirm:

- `anyproto/any-sync`'s `net/peerservice/peerservice.go` has a real `PreferQuic(bool)` toggle controlling per-connection transport ordering (yamux/TCP vs. QUIC).
- `anyproto/anytype-heart`'s `core/anytype/config/config.go` calls `PreferQuic(true)` unconditionally unless a `PeferYamuxTransport` config flag is set — and the comment right next to it, written by their own engineers, says: *"used only in case client has some problems with QUIC."* That flag threads all the way from a real, correctly-spelled `PreferYamuxTransport` field on the `RpcAccountSelectRequest` protobuf message (confirmed field name and type directly in the generated `pb/commands.pb.go`).
- `anyproto/anytype-cli`'s `core/serviceprogram/serviceprogram.go` (`attemptAutoLogin()`, called when `anytype serve` finds a stored key on startup — this is the exact log line "Found stored account key, attempting auto-login...") calls `core/auth.go`'s `Authenticate()`, which builds an `AccountSelect` request that **never sets `PreferYamuxTransport`.** A separate `AccountSelect` call exists too, immediately after `AccountCreate` in a different function (`CreateAccount`) — used for brand-new accounts, and left alone since that path is confirmed already working over QUIC.

**Fix, patched and built (not yet verified live):** forked `anyproto/anytype-cli` to [`thisispete/anytype-cli`](https://github.com/thisispete/anytype-cli), with a one-line, narrowly-scoped patch setting `PreferYamuxTransport: true` on just the `AccountSelect` call inside `Authenticate()` — the one confirmed-broken RPC, not a blanket "avoid QUIC everywhere" change. The official image can't be reused with a patch applied: its release build cross-compiles from a macOS runner using Homebrew (`mingw-w64`, `musl-cross`) and links against `anyproto/tantivy-go`'s prebuilt native library, which for Linux is **musl-only** (`linux-${arch}-musl.tar.gz` — matching Alpine's libc, not Ubuntu's glibc, which is why a plain `golang:` build on a bare Ubuntu runner wasn't an option either). Built instead via a new GitHub Actions workflow on the fork ([`.github/workflows/build-patched-image.yml`](https://github.com/thisispete/anytype-cli/blob/main/.github/workflows/build-patched-image.yml)) that compiles natively per-architecture (`ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64 — no cross-compilation needed) inside `golang:1.25-alpine` containers, matching the musl toolchain the tantivy release expects, then reuses the repo's own unmodified `Dockerfile` (which already just expects `anytype-linux-${TARGETARCH}` binaries in its build context) to assemble and push a multi-arch image. Verified via the real CI run, not assumed: all three jobs (`build-binary` × 2 archs, `build-image`) completed successfully, with a real pushed manifest digest (`sha256:80a4cc155e7bace398f764863477d4007f0501791f4731dd7a2412af36218449`) confirmed in the build logs. `docker-compose.anytype-bot.yml` now points at `ghcr.io/thisispete/anytype-cli:patched` instead of the official image.

**Beta round 6 (2026-08-17): the first build was broken, but not by the QUIC/auto-login fix — a packaging bug, caught live.** The round-5 image's bootstrap crashed immediately on binary startup: `Error loading shared library libstdc++.so.6` / `libgcc_s.so.1`, ~60 relocation errors, then a clean failure (round 1's fail-loud fix working exactly as designed — the main container correctly never started on a broken bootstrap). Root cause: the fork's CI build used a bare `go build` inside `golang:1.25-alpine`, which links dynamically by default on Alpine's native gcc — unlike upstream's actual release process, which cross-compiles from a macOS runner using a `musl-cross` toolchain that produces static binaries by default (no host libc to link against when cross-compiling). The final `alpine:3.23` runtime image only has `ca-certificates`/`netcat-openbsd` installed, so a dynamically-linked binary needing glibc's C++ runtime could never work there. The `PreferYamuxTransport` fix itself was never exercised — the binary could not execute a single command.

Fixed with explicit `-linkmode external -extldflags -static` (matching upstream's actual static output) plus `-tags netgo` (defensive: static musl binaries have a known-unreliable DNS/NSS path, and this binary's entire job is resolving a coordinator hostname). Also added real verification this pipeline was missing the first time: a `readelf -d` check that fails the build on any remaining dynamic dependency, and a smoke-test step that runs the binary inside a bare `alpine:3.23` container (matching the real runtime exactly) before ever uploading an artifact. That verification immediately caught a second, unrelated bug on the next CI run — the build container runs as root (needed for `apk add`), so its output landed root-owned on the runner's filesystem, and the runner's own non-root user couldn't `chmod +x` it for the smoke test. Fixed by `chown`-ing the output to the runner's UID/GID before exiting the container. Third CI run succeeded clean end-to-end: both native builds, both static-link checks, both smoke tests, final multi-arch push — verified via a new digest in the build logs (`sha256:3f4b0fb3e3b34ee814fc4179e9a5f8df8d387e59838d31eee59ffdb2ad035651`), superseding the broken one. `docker-compose.anytype-bot.yml` already points at the `:patched` tag, so no compose changes needed — it now resolves to the fixed image automatically.

**Beta round 7 (2026-08-17): a third, distinct bug — this one a build mistake, not an upstream issue, confirmed by reading the actual handler code.** The round-6 image ran correctly (no more linking errors) but crashed with a real Go panic on the very first RPC: `panic: client platform with the version must be set using the InitialSetParameters method`, inside anytype-heart's `AccountCreate` handler. Traced precisely rather than guessed:

- `anytype-heart`'s `InitialSetParameters` handler (`core/metrics.go`) rejects an empty `Version` field with a `BAD_INPUT` response — a real, deliberate validation, not a bug on their end.
- `anytype-cli`'s `CreateWallet` (the function `auth create` uses) calls `InitialSetParameters` but **discards the response** (`_, err := client.InitialSetParameters(...)`) — it only checks the transport-level Go error, never the response's embedded error code. So the `BAD_INPUT` rejection was silently swallowed, execution continued straight into `AccountCreate`, and the server panicked on a precondition (`clientWithVersion` being set) that was never actually satisfied. This particular bug is real and upstream's, worth reporting separately from the `PreferYamuxTransport` patch once that's confirmed working — but it only *surfaces* because of what comes next.
- The actual trigger: `Version` was empty because **this fork's CI build never set it.** Upstream's own Makefile always injects it via `-X 'github.com/anyproto/anytype-cli/core.Version=...'` ldflags (from `git describe --tags`); the from-scratch build workflow here never included that flag at all. Not a stale-branch or version-mismatch issue (briefly suspected, checked, and ruled out via the `anyproto/anytype-cli` tags/release API — `v0.3.6` was cut essentially the same moment as the `anytype-heart` v0.50.10 bump commit this fork built from, so upstream's official image is presumably on the same anytype-heart version and evidently doesn't hit this, consistent with the missing-ldflag diagnosis rather than a version incompatibility).

Fixed: added the missing `-X core.Version=0.0.0-hermes-anytype-patch-<short-sha>` ldflag. Also hardened the CI pipeline again — the `--help`-only smoke test from round 6 proved the binary starts but never exercised a real RPC, exactly the gap that let this ship undetected. Added a genuine functional smoke test that runs `auth create` against a real throwaway account inside a container matching the runtime image, for both architectures, before ever uploading an artifact. New CI run succeeded end-to-end *including* that functional test actually completing — verified via a new digest (`sha256:3b2d2e28e076dfe5a9656ff7a533d002ddbab03705ea1fbd4f7f54526a434adc`).

**Beta round 8 (2026-08-17): the patch reached the real code path — and does not fix the problem. The actual fix turned out to be a Docker Desktop setting, and even that isn't fully deterministic.** First, a single live test: bootstrap completed cleanly (real account, real `Account Id`), the main container picked up the stored key, and `attemptAutoLogin()` → `Authenticate()` ran with `PreferYamuxTransport: true` actually in effect — no build defect standing in the way this time. Result: the exact same failure, byte-for-byte, as rounds 2/3 — same `DeadlineExceeded`, same `quic-go` receive-buffer warning, same failure point (`wallet recovery failed`). This disproves the transport-preference hypothesis as sufficient, not just leaves it unconfirmed — it was well-grounded in real source and still didn't work.

That prompted a fresh look at Docker Desktop's own settings, and found the real lever: **Settings → Resources → Network → "Use kernel networking for UDP."** Off by default. Confirmed by inspecting the `com.docker.virtualization` process command line before/after toggling — it adds/removes a `--kernel-for-udp` flag, routing UDP through the host's real kernel network stack instead of gVisor's userspace reimplementation (the same userspace stack this section already suspected). It's a VM-wide setting, same blast radius as the gVisor/vpnkit engine choice from round 4 — restarting it interrupts every container on the host, not just this one.

With it enabled, the previously-100%-deterministic failure stopped being deterministic — which made "does it actually work" a harder question than the first couple of live tests suggested. Two early tests looked like a clean win (one on the real `hermes` identity against the patched image, one against the unpatched official image after a volume-preserving image swap) — but an isolated fresh-identity repro against the *unpatched* image, run minutes later under identical settings, failed with the identical `DeadlineExceeded`, directly contradicting the "already confirmed working" read. That discrepancy is exactly why a proper sample size mattered here rather than declaring victory on two lucky runs. A controlled batch — 5 fresh-identity trials each, unpatched vs. patched, `kernel-for-udp` on throughout, 60s cap per trial:

| | Original (unpatched) | Patched |
|---|---|---|
| Result | 3/5 success (19s, 18s, FAIL 23s, FAIL 28s, 18s) | 3/5 success (18s, FAIL 29s, FAIL 28s, 6s, 18s) |

Identical 60% success rate for both images — statistically indistinguishable at this sample size, confirming `PreferYamuxTransport` has no measurable effect (consistent with the single-test result above, now with real statistical backing rather than one data point). Timing is bimodal and matches anytype-heart's own hardcoded 3-attempt internal retry (successes land at 6–19s when an early attempt lands; failures resolve at 23–29s when all three exhaust) — consistent with genuine intermittent packet loss on the UDP path even through the kernel-networking route, not a deterministic bug reachable from a client-side flag.

**Revised, now well-evidenced conclusion:** `kernel-for-udp` is a real, substantial improvement (0% → ~60% per independent attempt) but not a deterministic fix on its own. `PreferYamuxTransport` measurably does nothing and was never the actual fix — the round 5-7 root-cause chain (creation vs. recovery being different RPCs) may still accurately describe what's different about the failing RPC, but the patch built on that diagnosis doesn't move the needle. Worth being honest this was a plausible, well-evidenced hypothesis that didn't pan out on a fair test, not a wrong theory abandoned without one.

**Response, implemented:** rather than keep chasing a fully deterministic single root cause, `docker-compose.anytype-bot.yml`'s main `anytype-cli` service now wraps `anytype serve` in a retry loop. `anytype-heart` does not exit or crash when its own internal auto-login retry is exhausted — it logs the failure and keeps running, gRPC server up but permanently unauthenticated — so Docker's own `restart: unless-stopped` policy never even triggers (it only acts on process exit), and the container just sits there reporting "healthy" (the healthcheck only checks the port answers) while being useless. The wrapper watches `anytype serve`'s own log output for the real outcome (`Successfully logged in...` / `Failed to auto-login...` / `No stored account key...`) and, on failure, kills and restarts the whole process — a fresh process re-triggers anytype-heart's internal 3-attempt sequence again, another independent shot at the ~60% per-attempt rate. At 10 outer retries (the default, configurable via `ANYTYPE_CLI_LOGIN_RETRIES`), expected overall failure probability is roughly 0.4¹⁰ ≈ 0.01%. Verified locally against a fake `anytype` binary before ever handing this back for another live round — fail/fail/success, all-fail exhaustion, immediate success, and the no-stored-key case all behave correctly, including that per-attempt log-line-offset tracking correctly avoids false-positive matches from a previous attempt's failure line still sitting in the file.

**The patched fork is retired, not maintained going forward.** With `PreferYamuxTransport` confirmed to have zero measurable effect, `docker-compose.anytype-bot.yml` reverts to the official `ghcr.io/anyproto/anytype-cli` image — maintaining a forked image plus its own CI pipeline indefinitely, in sync with upstream, for a patch proven to do nothing, isn't worth the ongoing cost. `thisispete/anytype-cli` and its build workflow are left in place as a historical record of the investigation (including two real, separately-useful findings along the way: the static-linking build defect and the `CreateWallet` discarded-response bug, still worth filing upstream as their own thing someday, unrelated to whether the transport-preference theory panned out) but nothing here depends on it anymore.

The README's prerequisites section now documents `kernel-for-udp` as a required macOS Docker Desktop setup step.

**Unverified, needs empirical check before finalizing setup docs:** whether `auth create hermes` makes "Hermes" the actual display name/avatar shown in chat, or whether that has to be set separately once logged in as that identity. Same category of "verify against a running instance" caveat as the mention-mark shape in Section 9. (No beta pass has reached this question yet — round 1 was blocked at the path bug, rounds 2 and 3 at the auto-login failure above.)

This doesn't change the config shape in Section 3 — still one `api_key`/`api_base_url` pair — but adds a real manual prerequisite outside the plugin itself: standing up a second, bot-owned Anytype node before Hermes can appear as itself rather than as the human. Belongs in the README's prerequisites section (Section 8).

## 11. Alternatives considered (context, not decisions)

Captured for whoever picks this up later, since they were seriously discussed:

- **Claude-first via MCP instead of Hermes.** The object/schema capability layer already exists as an official server (`anyproto/anytype-mcp`) — client-agnostic, works with Claude Desktop (local MCP), claude.ai Connectors (remote MCP behind a reverse proxy, a common self-hosted deployment pattern), or Claude Code under a Pro/Max login — all without metered API billing. The one thing none of those paths can do is proactive/reactive chat presence (something has to sit listening and call an LLM on its own initiative, which structurally requires a metered API-backed process) — that's the actual reason a Hermes-side gateway is still worth building, not just inertia. Likely end state: both — Claude/Connectors for on-demand pull-based use, this plugin for the ambient/reactive chat presence Claude structurally can't provide under a subscription model.
- **Retiring Hermes entirely.** Partially reconsidered mid-design. Claude Code now has native scheduled-task support (a real replacement for simple cron-style recurring prompts), but Hermes's Discord-gateway model still provides ambient cross-device context continuity (same conversation reachable from any device with zero setup) that Claude Code's current session/machine-bound model doesn't fully replicate — claude.ai syncs conversational context across devices, but Claude Code sessions tied to a specific machine's filesystem still require deliberate remote setup (SSH, or Anthropic's newer Remote Control / cloud-session mechanism) rather than being automatic. Not resolved — flagged as an open question, not blocking this project.

## 12. Next steps

1. ~~Bootstrap the actual repo~~ — done: `git init`, MIT `LICENSE`, this file at `docs/design.md`, `.gitignore`.
2. ~~Confirm Hermes's exact plugin-authoring API surface~~ — done, against real source rather than docs (Hermes's own docs page for this was incomplete): cloned `NousResearch/hermes-agent` and read `hermes_cli/plugins.py` (`register_platform`/`register_tool` signatures), `gateway/platforms/base.py` (`BasePlatformAdapter`, `MessageEvent`, `SendResult`, the `Platform` enum's `_missing_()` dynamic-member hook), and the shipped `plugins/platforms/mattermost/` + `plugins/platforms/irc/` adapters as real templates. `adapter.py`/`tools.py`/`__init__.py`/`plugin.yaml` are now built against that, not a guess.
3. ~~Verify the mention-mark shape empirically~~ — done, beta round 11, Section 4.1: real UI @mentions embed the mentioned party's display name as literal text plus a structural `{"type": "mention", "param": <account id>}` mark. Structural detection implemented (`env_config.is_mentioned`, `ANYTYPE_ACCOUNT_ID` config) alongside the substring fallback, not instead of it. **Still open:** live re-verification with `ANYTYPE_ACCOUNT_ID` actually configured, since it wasn't set during the round that found this.
4. ~~Root-cause and fix the auto-login failure~~ — **fully resolved and confirmed live, beta round 9.** The real fix was a Docker Desktop setting (`kernel-for-udp`, 0% → ~60% per-attempt success), not the `PreferYamuxTransport` code patch (confirmed zero measurable effect via a controlled 5-trial-per-image comparison, Section 10). Since `kernel-for-udp` alone isn't deterministic, the main service wraps `anytype serve` in a retry loop (default 10 attempts). Round 9 confirmed this working against real, uncontrolled flakiness on the very first `down -v`/`up -d` cycle — outer attempt 1 hit a genuine `DeadlineExceeded`, the wrapper detected it and restarted the process, attempt 2 succeeded cleanly (~48s total). A real `ANYTYPE_API_KEY` was minted against a real authenticated `hermes` identity. This is the thing the entire project has been blocked on since round 1 of beta testing — first time a live Anytype API key has existed. The patched fork (`thisispete/anytype-cli`) is retired — reverted to the official image. Two genuinely separate bugs found along the way (the static-linking build defect, the `CreateWallet` discarded-response bug) remain worth filing upstream someday.
5. **Still open, deliberately deprioritized (Section 1):** cursor-based SSE resume (`after_order_id`) instead of reconnecting to a fresh live stream on drop (Section 6). A real gap, not a blocker.
6. **Beta passes against a live Docker Hermes stack, 2026-08-16 through 2026-08-17, thirteen rounds:** round 1 confirmed the plugin itself imports, registers, and passes `hermes plugins doctor` cleanly, and surfaced/got-fixed four install-mechanics bugs. Rounds 2-4 chased the auto-login failure through Docker Desktop's networking. Rounds 5-7 built, then twice re-built, a patched fork chasing a transport-preference theory — two real build bugs caught along the way. Round 8 resolved it: the patch measurably does nothing; the actual fix is a Docker Desktop setting (`kernel-for-udp`), backstopped by a retry wrapper. Round 9 confirmed the retry wrapper working against real flakiness and produced this project's first-ever real `ANYTYPE_API_KEY`. Round 10 fixed a real SSE-stream self-cancellation bug. Round 11 found two more real plugin bugs (unhandled `content` nesting, missing structural @mention detection — the mention-mark shape flagged as unverified all the way back in the original design, now confirmed and handled). Round 12 fixed a crash reachable only once mentions started matching (`creator` field shape). Round 13 verified the plugin's own logic correct in isolation against real live payloads, found and fixed a real message-deduplication gap (fresh SSE connections replay entire chat history, confirmed live — corrects a wrong assumption in Section 6), and traced the actual remaining silence to a Hermes-core gateway hang **explicitly out of scope for this repository** — not something `hermes-anytype`'s own code can fix. As far as this repo's own code goes, everything currently testable has been verified correct; an actual end-to-end reply is blocked on an external issue, not on anything here.
## 13. Live fixes, 2026-11-08 (first real discussion traffic)

Two confirmed-live findings from the first night of real two-way traffic in a
page discussion, both fixed and verified at the wire level:

1. **v1 message reads return empty text for other authors' messages.**
   Symptom: every message Doug sent from the iOS app arrived at the
   gateway with `content.text == ""` (REST read and v1 SSE stream both) —
   id/creator/timestamps intact, body silently gone — so the bot kept
   replying "your message came through empty". The bot's own messages read
   back fine, which made it look like an author-specific decryption bug.
   The v2 endpoints (`/v2/spaces/{space}/chats/{chat}/messages` and
   `/v2/.../messages/stream`) decode every author's text correctly, so
   `get_chat_messages`/`stream_chat_messages` now read v2 and normalize to
   the v1-ish shape (`creator`/`creator_name`/`content.text`/
   `reply_to_message_id`). v2 inlines formatting into `text`
   ("bo**ld**") instead of exposing a marks array — acceptable because
   marks are only used for mention detection and this deployment runs with
   `ANYTYPE_REQUIRE_MENTION=false` (the trigger-substring path still
   matches on text). The still-empty *historical* messages won't recover —
   they were stored degraded at sync time; only post-fix messages decode.

2. **The mark type for inline code is "Keyboard", not "code".**
   Proto enum `Block.Content.Text.Mark.Type` is
   `Strikethrough=0, Keyboard=1, Italic=2, Bold=3, Underscored=4,
   Link=5, ...` — there is no "code" value, and the middleware silently
   coerces unknown mark strings to enum value 0, i.e. **Strikethrough**
   (same fallback that coerces "underline"). Every backtick span the bot
   sent therefore rendered struck-through in the client. Fixed in
   `_markdown_to_marks` (`code` → `"keyboard"`), verified live: a
   `keyboard` mark persists as `keyboard`, a `code` mark comes back as
   `strikethrough`.

Also that night: the bot node's `ghcr.io/anyproto/anytype-cli` image was
updated Oct 13 → Nov 5 build (container recreated via
`anytype-bot.service`; harmless but keeps the node current with the
iOS app's newer discussion work). Chat *listing* for discussions
(v2 list with `include=discussions`) had landed earlier the same evening.

## 14. Attachment visibility (2026-11-08, later that night)

An object attached in the iOS app's discussion editor ("attach the hockey
draft discussion chat") never reached the bot: the message arrives with
`attachments: []` on both v1 and v2 reads, so iOS attaches are almost
certainly stored as structured message *blocks* (`MessageBlockLink`), which
the REST read does not expose at all (v1: `blocks: null`; v2: key omitted) —
the same REST-can't-see-blocks ceiling as Section 13's code-fence finding.
The attachment pipeline itself is fine: a bot-sent attachment with
`{"target": <object id>, "kind": "link"}` persists and reads back on v1 and
v2 (note: `kind: "link"` reads back as `"file"` — unknown attachment-kind
strings coerce to `File=0`, same enum fallback as marks).

Since REST-visible attachments did work end-to-end, the adapter now makes
them visible to the model: `_handle_anytype_message` appends one line per
attachment (`[attached Page: 🏒 Hockey Pool — Draft Night Strategy (Nov 9)]
(object id: ...)`) resolving the target object's real name and type via the
new `AnytypeClient.get_object` (GET `/v1/spaces/{id}/objects/{id}`, envelope
`{"object": {...}}`, `type` a nested dict). Best-effort by design: a failed
resolution logs a warning and degrades to the raw id, never blocks the
message. Mentions/should_respond still evaluate on the original text only —
an attachment can never trigger a response by itself.

Remaining gap (needs middleware/gRPC work, not plugin work): iOS-side
attaches that land in `blocks`, and inbound-to-bot attaches can't be sent
back with rich links — outbound stays text+marks.
