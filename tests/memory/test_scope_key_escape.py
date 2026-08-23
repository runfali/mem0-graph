"""
Tests for PR #6892: percent-escape session scope key values.

session scope keys (user_id / agent_id / run_id) previously joined raw
values with '&' and '='. Values containing '%', '&' or '=' corrupted the
key: an '&' inside a value was indistinguishable from the key separator.
This escapes '%' -> '%25', '&' -> '%26', '=' -> '%3D' so a single
unambiguous key survives the round trip.

The escaping lives in the shared `_build_session_scope` helper used by
both sync and async code paths, so one test suite covers both.
"""


from mem0.memory.main import _build_session_scope


class TestScopeKeyEscape:
    """Tests for percent-escaping of session scope key values."""

    def test_plain_id_unchanged(self):
        """Plain ids without special chars must produce the exact pre-fix key."""
        scope = _build_session_scope({"user_id": "hermes-user"})
        assert scope == "user_id=hermes-user"

    def test_plain_ids_multiple_keys_unchanged(self):
        """Multiple plain ids joined with '&' must be unchanged (regression)."""
        scope = _build_session_scope({"user_id": "u1", "agent_id": "a1", "run_id": "r1"})
        assert scope == "agent_id=a1&run_id=r1&user_id=u1"

    def test_percent_in_id_escaped(self):
        """'%' in a value is escaped to '%25'."""
        scope = _build_session_scope({"user_id": "50%off"})
        assert scope == "user_id=50%25off"

    def test_ampersand_in_id_escaped(self):
        """'&' in a value is escaped to '%26'."""
        scope = _build_session_scope({"agent_id": "a&b"})
        assert scope == "agent_id=a%26b"

    def test_equals_in_id_escaped(self):
        """'=' in a value is escaped to '%3D'."""
        scope = _build_session_scope({"run_id": "x=y"})
        assert scope == "run_id=x%3Dy"

    def test_mixed_special_chars_escaped(self):
        """'%', '&' and '=' in one value are all escaped."""
        scope = _build_session_scope({"user_id": "a%b&c=d"})
        assert scope == "user_id=a%25b%26c%3Dd"

    def test_escaped_value_not_confused_with_separator(self):
        """An '&' inside a value must not be mistaken for the key separator."""
        scope = _build_session_scope({"user_id": "u1", "agent_id": "a&b", "run_id": "r1"})
        assert scope == "agent_id=a%26b&run_id=r1&user_id=u1"

    def test_round_trip_preserves_keys_and_values(self):
        """Each 'key=value' segment survives parsing after escaping."""
        scope = _build_session_scope({"user_id": "a%b&c=d", "agent_id": "plain"})
        segments = dict(pair.split("=", 1) for pair in scope.split("&"))
        assert segments["user_id"] == "a%25b%26c%3Dd"
        assert segments["agent_id"] == "plain"

    def test_escape_is_idempotent_friendly(self):
        """Calling on already-escaped-style values does not double-escape plain ids."""
        scope = _build_session_scope({"user_id": "hermes-user", "agent_id": "a&b"})
        assert "a%26b" in scope
        assert "a%2526b" not in scope
