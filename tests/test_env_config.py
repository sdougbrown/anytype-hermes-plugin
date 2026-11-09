from hermes_anytype.env_config import (
    env_bool,
    has_required_config,
    is_mentioned,
    parse_message_author,
    should_respond,
    split_csv,
)


def test_split_csv_strips_and_drops_empties():
    assert split_csv("a, b ,, c") == ["a", "b", "c"]
    assert split_csv("") == []
    assert split_csv(None) == []


def test_env_bool_parses_truthy_and_falsy_strings():
    assert env_bool("true", default=False) is True
    assert env_bool("1", default=False) is True
    assert env_bool("yes", default=False) is True
    assert env_bool("false", default=True) is False
    assert env_bool("0", default=True) is False


def test_env_bool_uses_default_when_all_values_unset():
    assert env_bool(None, "", default=True) is True
    assert env_bool(None, default=False) is False


def test_env_bool_prefers_first_set_value():
    assert env_bool("false", "true", default=True) is False


def test_has_required_config():
    assert has_required_config("key", "http://127.0.0.1:31012", "space1") is True
    assert has_required_config("", "http://127.0.0.1:31012", "space1") is False
    assert has_required_config("key", "  ", "space1") is False


def test_should_respond_all_mode_always_true():
    assert should_respond("anything at all", require_mention=False, trigger="@hermes")
    assert should_respond("", require_mention=False, trigger="@hermes")


def test_should_respond_mention_mode_requires_trigger():
    assert should_respond("hey @hermes can you help", require_mention=True, trigger="@hermes")
    assert not should_respond("no trigger here", require_mention=True, trigger="@hermes")


def test_should_respond_mention_mode_is_case_insensitive():
    assert should_respond("Hey @HERMES!", require_mention=True, trigger="@hermes")


def test_should_respond_mention_mode_uses_custom_trigger():
    assert should_respond("!bot status?", require_mention=True, trigger="!bot")
    assert not should_respond("@hermes status?", require_mention=True, trigger="!bot")


# -- Structural mention detection (docs/design.md Section 4.1, beta round 11) --
#
# Real UI @mentions embed the mentioned party's *display name* as literal
# text, not the trigger string -- e.g. a bot whose profile name was never
# set shows up as "Untitled". A substring match on trigger text alone misses
# these; is_mentioned checks the actual mention marks instead.

REAL_MENTION_MARKS = [
    {
        "from": 0,
        "to": 8,
        "type": "mention",
        "param": "AAAAprefix_A9GoTfqnjjBe2HzAMVcLvorSXG4ebPxSnQqfh3FdLLBD4SAQ",
    }
]
ACCOUNT_ID = "A9GoTfqnjjBe2HzAMVcLvorSXG4ebPxSnQqfh3FdLLBD4SAQ"


def test_is_mentioned_matches_real_mention_mark():
    assert is_mentioned(REAL_MENTION_MARKS, ACCOUNT_ID) is True


def test_is_mentioned_false_with_no_marks():
    assert is_mentioned([], ACCOUNT_ID) is False
    assert is_mentioned(None, ACCOUNT_ID) is False


def test_is_mentioned_false_with_no_account_id():
    assert is_mentioned(REAL_MENTION_MARKS, None) is False
    assert is_mentioned(REAL_MENTION_MARKS, "") is False


def test_is_mentioned_ignores_non_mention_marks():
    marks = [{"type": "bold", "param": ACCOUNT_ID}]
    assert is_mentioned(marks, ACCOUNT_ID) is False


def test_is_mentioned_false_for_a_different_account():
    marks = [{"type": "mention", "param": "someone_else_entirely"}]
    assert is_mentioned(marks, ACCOUNT_ID) is False


def test_should_respond_catches_real_mention_even_when_display_name_is_untitled():
    # Confirmed live: a real @mention's visible text is the bot's display
    # name ("Untitled sup?"), not the trigger string -- substring matching
    # alone would silently ignore this exact message.
    assert should_respond(
        "Untitled sup?",
        require_mention=True,
        trigger="@hermes",
        marks=REAL_MENTION_MARKS,
        account_id=ACCOUNT_ID,
    )


def test_should_respond_still_works_without_marks_or_account_id():
    """Backward compatible: existing substring-only call sites unaffected."""
    assert should_respond("hey @hermes", require_mention=True, trigger="@hermes")
    assert not should_respond("no trigger", require_mention=True, trigger="@hermes")


# -- Message author parsing (docs/design.md Section 4.1, beta round 12) --
#
# `creator` is a plain participant-id string in the real payload, not a
# nested {"id": ..., "name": ...} object -- an earlier version of this code
# assumed the dict shape and crashed with AttributeError on every message
# once mention detection started actually matching.

def test_parse_message_author_reads_flat_creator_and_creator_name():
    message = {
        "id": "msg1",
        "creator": "_participant_bafyre_A9GoTfqnjjBe2HzAMVcLvorSXG4ebPxSnQqfh3FdLLBD4SAQ",
        "creator_name": "▲PETE",
    }
    user_id, user_name = parse_message_author(message)
    assert user_id == "_participant_bafyre_A9GoTfqnjjBe2HzAMVcLvorSXG4ebPxSnQqfh3FdLLBD4SAQ"
    assert user_name == "▲PETE"


def test_parse_message_author_falls_back_to_creator_id():
    message = {"id": "msg1", "creator_id": "fallback-id"}
    user_id, user_name = parse_message_author(message)
    assert user_id == "fallback-id"
    assert user_name is None


def test_parse_message_author_handles_missing_fields():
    assert parse_message_author({}) == (None, None)
