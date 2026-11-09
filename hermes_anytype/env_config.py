"""Pure env/config helpers, deliberately free of any Hermes-internal import.

adapter.py imports `gateway.config` / `gateway.platforms.base`, which only
exist inside a real Hermes install -- so nothing that imports adapter.py can
be unit-tested in this standalone repo (there's no pip-installable Hermes
package; the real gateway package is a sibling in Hermes's own monorepo, not
a dependency of ours). The logic actually worth unit-testing -- mention
filtering, env-var parsing, required-config checks -- lives here instead,
with no such import, so it stays covered even though the adapter class
itself can only be exercised inside a live Hermes environment (same
can't-run-the-real-thing-in-CI situation docs/design.md Section 7 already accepts
for Anytype itself).
"""

from __future__ import annotations

from typing import Any


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def env_bool(*values: Any, default: bool) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text:
            return text in ("1", "true", "yes", "on")
    return default


def is_mentioned(marks: list[dict[str, Any]] | None, account_id: str | None) -> bool:
    """Structural mention check against Anytype's real `marks` array.

    Confirmed live (docs/design.md Section 4.1/10, beta round 11) that a
    UI-driven @mention embeds the mentioned party's *display name* as
    literal text -- e.g. "Untitled" for a bot whose profile name was never
    set -- plus a separate `{"type": "mention", "param": <identity>}` mark
    referencing their real participant/account id. A trigger-string
    substring match alone misses every real UI mention whose display name
    isn't literally the trigger text, which is the common case. `param` is
    checked with `in` rather than `==` since its exact prefix format isn't
    fully pinned down -- the confirmed live example had the account id as a
    suffix of a longer string, not the whole value.
    """
    if not marks or not account_id:
        return False
    for mark in marks:
        if mark.get("type") != "mention":
            continue
        if account_id in (mark.get("param") or ""):
            return True
    return False


def should_respond(
    message_text: str,
    *,
    require_mention: bool,
    trigger: str,
    marks: list[dict[str, Any]] | None = None,
    account_id: str | None = None,
) -> bool:
    """Per-chat mention/reply-all filter (docs/design.md Section 2, Section 4.1).

    Two independent ways to count as "mentioned": a literal trigger-string
    substring match (works for anyone who just types "@hermes" as plain
    text), or a structural mention mark referencing the bot's own account
    id (works for anyone who used the UI's real @mention feature -- see
    is_mentioned's docstring for why the substring check alone isn't
    enough). `marks`/`account_id` are optional so existing substring-only
    call sites and tests keep working unchanged.
    """
    if not require_mention:
        return True
    if trigger.lower() in message_text.lower():
        return True
    return is_mentioned(marks, account_id)


def has_required_config(api_key: str, base_url: str, space_id: str) -> bool:
    """docs/design.md Section 6: fail loud at startup on bad/missing config, never
    silently no-op."""
    return bool(api_key.strip() and base_url.strip() and space_id.strip())


def parse_message_author(message: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (user_id, user_name) from a chat message payload.

    Confirmed live (docs/design.md Section 4.1, beta round 12): `creator`
    is a plain participant-id string (e.g. "_participant_bafyre..."), not a
    nested object -- an earlier version of this code assumed a dict shape
    (`message["creator"]["id"]`) and crashed with AttributeError on every
    single message once mention detection started actually matching (the
    crash path was unreachable before that fix landed, since should_respond
    always returned False first -- which is why it went unnoticed until
    then). The display name is a separate flat `creator_name` field, never
    nested under `creator` either.
    """
    user_id = message.get("creator") or message.get("creator_id")
    user_name = message.get("creator_name")
    return user_id, user_name
