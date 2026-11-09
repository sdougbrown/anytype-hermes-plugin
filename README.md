# hermes-anytype

A Hermes plugin that gives Hermes a live chat presence inside a self-hosted
[Anytype](https://anytype.io/) space, plus tools to search/create/update that
space's typed objects — driven by live schema introspection, so it works
against anyone's custom type setup with zero manual mapping.

See [docs/design.md](docs/design.md) for the full design and rationale.
**Status:** built against Hermes's confirmed real plugin API (see
[`hermes_anytype/adapter.py`](hermes_anytype/adapter.py) — verified against
`NousResearch/hermes-agent`'s own shipped Mattermost/IRC plugins, not
guessed). Not yet run against a live Anytype instance — manual verification
is still needed before calling this production-ready; see
[Development](#development).

## Prerequisites

1. A running Hermes instance (this plugin targets Hermes's official Docker
   image, `nousresearch/hermes-agent:latest` — see
   [Hermes's Docker docs](https://hermes-agent.nousresearch.com/docs/user-guide/docker)).
2. A self-hosted or cloud Anytype instance with the local API enabled
   (self-hosting the sync backend is optional and orthogonal to this — the
   local API works the same regardless of network mode).
3. **On macOS with Docker Desktop: enable "Use kernel networking for UDP"**
   (Settings → Resources → Network). Off by default, and without it the bot
   identity's login will fail intermittently with a QUIC/UDP timeout — this
   was root-caused across an extensive beta investigation (see
   [design.md Section 10](docs/design.md#10-hermess-anytype-identity-bot-account-setup)),
   and even with it enabled the failure isn't fully eliminated, just made
   survivable (see the retry logic in
   [`docker-compose.anytype-bot.yml`](docker-compose.anytype-bot.yml)).
   This is a VM-wide setting — toggling it restarts every container on the
   host, not just this one.
4. **A separate Anytype identity for Hermes itself**, invited into your
   space as its own member — otherwise every message posts under your own
   profile, not "Hermes". See [design.md Section 10](docs/design.md#10-hermess-anytype-identity-bot-account-setup)
   for the full walkthrough. Short version, using the official
   `ghcr.io/anyproto/anytype-cli` image via
   [`docker-compose.anytype-bot.yml`](docker-compose.anytype-bot.yml)
   (default Anytype Network case — see that file's header comment for the
   self-hosted-backend variant):

   ```bash
   docker compose -f docker-compose.anytype-bot.yml up -d

   # Back up the account recovery key now, then delete it from the
   # container -- it's the ONLY way to recover this identity if the
   # anytype-cli-data volume is ever lost. Treat it like a wallet seed
   # phrase: it's written to a file on the volume, deliberately never
   # printed to docker logs (see docker-compose.anytype-bot.yml's
   # comments for why that matters).
   docker compose -f docker-compose.anytype-bot.yml exec anytype-cli cat /root/.anytype/ACCOUNT_RECOVERY_KEY.txt
   # copy that into a password manager, then:
   docker compose -f docker-compose.anytype-bot.yml exec anytype-cli rm /root/.anytype/ACCOUNT_RECOVERY_KEY.txt

   docker compose -f docker-compose.anytype-bot.yml exec anytype-cli anytype auth apikey create hermes-integration
   # from your desktop app: Share Space -> copy invite link
   docker compose -f docker-compose.anytype-bot.yml exec anytype-cli anytype space join "<invite-link>"
   # then approve the join request in-app
   ```

## Install

**Recommended — via Hermes's own plugin installer**, which handles enabling
and safely prompts you per-variable for the `requires_env`/`optional_env`
block in `plugin.yaml`, merging into your existing `.env` rather than
overwriting it:

```bash
hermes plugins install <path-or-url-to-this-repo>
hermes plugins enable anytype-platform
```

The name `hermes plugins enable` expects is **`anytype-platform`** (from
`plugin.yaml`'s `name:` field) — not `hermes_anytype` (the directory/package
name). The two don't match; `hermes plugins enable hermes_anytype` won't
work.

**Manual alternative:** copy this repo's `hermes_anytype/` directory into
`<HERMES_HOME>/plugins/`, where `HERMES_HOME` is wherever your Hermes
container's data directory is actually mounted from on the host — check the
`volumes:` line for the `hermes` service in your own `docker-compose.yml`.
**Don't assume `~/.hermes`:** that's only Hermes's own docs' default example,
not a guarantee — real deployments commonly point it elsewhere. No compose
changes are needed either way, since that directory already covers
`plugins/` alongside `skills/`/`sessions/`/etc. Plugin loading is opt-in by
default (`config.yaml`'s `plugins.enabled: []`), so the manual path still
needs `hermes plugins enable anytype-platform` afterward — Hermes won't load
a plugin just because its directory exists.

Either way, no `pip install` step is required inside Hermes's own
environment: this plugin is deliberately built on `aiohttp`, which already
ships with Hermes core — see
[`anytype_client.py`](hermes_anytype/anytype_client.py)'s module docstring
for why that matters (Hermes's official image treats its install tree as
immutable at runtime, so a plugin with its own extra pip dependency would
force a custom derived image just to use it).

## Configure

If you installed via `hermes plugins install`/`enable` above, you're
already done — that flow prompts for each `requires_env`/`optional_env`
value from `plugin.yaml` and merges them into your existing `.env` safely.

If you installed manually, **do not `cp .env.example .env`** — on a live
Hermes instance, `<HERMES_HOME>/.env` almost certainly already holds
unrelated secrets (other plugins' tokens, dashboard credentials, etc.), and
that command would overwrite the whole file. Hand-merge these values into
the existing file instead:

```bash
ANYTYPE_API_KEY=...              # Hermes's own identity's API key, not yours
# host.docker.internal, not 127.0.0.1 -- Hermes and the anytype-cli bot node
# are normally two separate Docker Compose projects, so 127.0.0.1 from
# inside Hermes's own container is Hermes's own loopback, not the bot
# node's. See .env.example for the full explanation and the non-Docker-Hermes
# case.
ANYTYPE_API_BASE_URL=http://host.docker.internal:31012
ANYTYPE_SPACE_ID=...

# optional
ANYTYPE_CHATS=                   # comma-separated chat_ids; blank = auto-discover all
ANYTYPE_REQUIRE_MENTION=true     # false = respond to everything, everywhere
ANYTYPE_MENTION_TRIGGER=@hermes
ANYTYPE_FREE_RESPONSE_CHATS=     # comma-separated chat_ids that ignore REQUIRE_MENTION
ANYTYPE_ACCOUNT_ID=              # strongly recommended -- see .env.example, real @mentions need this
```

`ANYTYPE_REQUIRE_MENTION=true` (the default) means Hermes only replies when
addressed by `ANYTYPE_MENTION_TRIGGER` — good for a shared team room. Set it
`false`, or list specific chats in `ANYTYPE_FREE_RESPONSE_CHATS`, for a solo
space or a dedicated help room where every message should get a reply.

Set `ANYTYPE_ACCOUNT_ID` too if you're using mention mode — a real
@mention through Anytype's UI embeds the mentioned party's *display name*
as literal text, not the trigger string, so without this the plugin can
only catch people who type `@hermes` as plain text (confirmed live — see
`.env.example`).

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the Anytype REST client (against a real local `aiohttp.web` test
server, not a mocking library — see
[`tests/test_anytype_client.py`](tests/test_anytype_client.py) for why),
the property-validation/normalization logic and tool handlers in `tools.py`,
and the mention/reply-all filtering and env-config logic in `env_config.py`.

`hermes_anytype/adapter.py` — the actual `BasePlatformAdapter` subclass —
can't be unit-tested in this repo: it imports `gateway.config` /
`gateway.platforms.base`, which only exist inside a real Hermes install (see
the module's docstring). That's also true of a real Anytype instance in CI
(impractical to run one). Both need manual verification against a live
Hermes + Anytype setup before this is production-ready — there's no way
around that for either half.

## License

MIT — see [LICENSE](LICENSE).
