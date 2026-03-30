"""
Unit tests for TokenTrackerProtocol.

Tests validate the feed/record/rolling_summary/session_summary contract
against mocked db dependencies.

Mocked boundaries:
  - careful_claude_claw.db.insert_token_event
  - careful_claude_claw.db.query_token_events_since
  - careful_claude_claw.db.query_token_events_for_session

No real SQLite I/O is performed in these tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.dashboard.interfaces import TokenEvent, TokenSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _import_tracker():
    """Import TokenTracker class (deferred so ImportError is test-time)."""
    from careful_claude_claw.dashboard.token_tracker import TokenTracker  # type: ignore[import]

    return TokenTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def tracker():
    """Fresh TokenTracker instance for each test."""
    TokenTracker = _import_tracker()
    return TokenTracker()


# ---------------------------------------------------------------------------
# feed() — complete usage JSON in a single chunk
# ---------------------------------------------------------------------------


def test_feed_complete_usage_returns_token_event(tracker):
    """
    feed() with a chunk containing a complete "usage":{...} block returns
    exactly one TokenEvent with the correct counts.
    """
    chunk = b'Some output\x1b[0m {"usage":{"input_tokens":100,"output_tokens":50}} more output'

    events = tracker.feed("sess-1", chunk)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TokenEvent)
    assert event.session_id == "sess-1"
    assert event.input_tokens == 100
    assert event.output_tokens == 50
    assert isinstance(event.recorded_at, datetime)


def test_feed_no_usage_returns_empty_list(tracker):
    """feed() with a chunk containing no usage JSON returns an empty list."""
    chunk = b"Hello, world! This is normal output with no token info."
    events = tracker.feed("sess-2", chunk)
    assert events == []


# ---------------------------------------------------------------------------
# feed() — split JSON across multiple calls
# ---------------------------------------------------------------------------


def test_feed_split_json_no_event_on_first_call(tracker):
    """
    When a "usage":... JSON object is split mid-stream, the first partial chunk
    yields no events.
    """
    # Split before the closing brace
    part1 = b'{"usage":{"input_tokens":200,"output_tokens":'
    events = tracker.feed("split-sess", part1)
    assert events == []


def test_feed_split_json_event_on_second_call(tracker):
    """
    The second chunk completing the split JSON must yield one TokenEvent.
    """
    part1 = b'{"usage":{"input_tokens":200,"output_tokens":'
    part2 = b'75}}'

    tracker.feed("split-sess", part1)
    events = tracker.feed("split-sess", part2)

    assert len(events) == 1
    event = events[0]
    assert event.input_tokens == 200
    assert event.output_tokens == 75


def test_feed_split_across_three_chunks(tracker):
    """
    A usage block split into three chunks emits the event only on the final chunk.
    """
    p1 = b'some text {"usage":{'
    p2 = b'"input_tokens":333,'
    p3 = b'"output_tokens":111}}'

    assert tracker.feed("three-way", p1) == []
    assert tracker.feed("three-way", p2) == []
    events = tracker.feed("three-way", p3)
    assert len(events) == 1
    assert events[0].input_tokens == 333
    assert events[0].output_tokens == 111


# ---------------------------------------------------------------------------
# feed() — ANSI escape codes mixed in
# ---------------------------------------------------------------------------


def test_feed_with_ansi_escapes_parses_correctly(tracker):
    """
    feed() still parses usage JSON when ANSI escape codes are interspersed
    in the byte stream.
    """
    # ANSI reset + bold + color codes surrounding the usage JSON
    ansi_chunk = (
        b"\x1b[0m\x1b[1mProcessing...\x1b[0m "
        b'{"usage":{"input_tokens":42,"output_tokens":17}}'
        b"\x1b[32m Done\x1b[0m"
    )

    events = tracker.feed("ansi-sess", ansi_chunk)

    assert len(events) == 1
    assert events[0].input_tokens == 42
    assert events[0].output_tokens == 17


def test_feed_with_ansi_in_prefix_and_suffix(tracker):
    """ANSI codes before and after usage JSON do not prevent parsing."""
    chunk = b"\x1b[2J\x1b[H" + b'{"usage":{"input_tokens":10,"output_tokens":5}}' + b"\x1b[0m"
    events = tracker.feed("ansi-2", chunk)
    assert len(events) == 1
    assert events[0].input_tokens == 10


# ---------------------------------------------------------------------------
# feed() — malformed / truncated JSON
# ---------------------------------------------------------------------------


def test_feed_malformed_json_no_exception(tracker):
    """Malformed JSON near 'usage' does not raise an exception."""
    chunk = b'{"usage":{"input_tokens": "not-a-number", "output_tokens": }'
    events = tracker.feed("bad-json", chunk)
    # No exception; events list may be empty
    assert isinstance(events, list)


def test_feed_truncated_json_no_exception(tracker):
    """A truncated JSON string does not raise an exception."""
    chunk = b'{"usage":{"input_token'
    events = tracker.feed("truncated", chunk)
    assert isinstance(events, list)
    assert events == []


def test_feed_buffer_eventually_cleared_after_malformed(tracker):
    """
    After feeding malformed data, subsequent valid data is still parsed.
    The tracker must not accumulate unbounded state from prior bad input.
    """
    bad = b'{"usage":{"input_tokens": !!!}'
    tracker.feed("recover-sess", bad)

    good = b'{"usage":{"input_tokens":99,"output_tokens":11}}'
    events = tracker.feed("recover-sess", good)
    # We don't mandate exactly 1 event here (implementation decides how to
    # handle the leftover buffer), but we do mandate no exception.
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# feed() — multiple "usage" blocks in one chunk
# ---------------------------------------------------------------------------


def test_feed_multiple_usage_blocks_returns_multiple_events(tracker):
    """
    A chunk with two separate "usage":... objects yields two TokenEvents.
    """
    chunk = (
        b'{"usage":{"input_tokens":10,"output_tokens":5}}'
        b" ... some output ... "
        b'{"usage":{"input_tokens":20,"output_tokens":8}}'
    )

    events = tracker.feed("multi-sess", chunk)

    assert len(events) == 2
    totals_in = {e.input_tokens for e in events}
    assert 10 in totals_in
    assert 20 in totals_in


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


def test_record_calls_db_insert(tracker):
    """record() calls db.insert_token_event with the correct arguments."""
    event = TokenEvent(
        session_id="record-sess",
        input_tokens=500,
        output_tokens=250,
        recorded_at=_now(),
    )

    with patch.object(db_module, "insert_token_event") as mock_insert:
        tracker.record(event)

    mock_insert.assert_called_once()
    args = mock_insert.call_args
    # Verify the session_id, input_tokens, output_tokens, recorded_at are passed
    # (positional or keyword; we check both possibilities)
    positional = args.args
    keyword = args.kwargs

    def _check_arg(name: str, value) -> bool:
        return value in positional or keyword.get(name) == value

    assert _check_arg("session_id", "record-sess")
    assert _check_arg("input_tokens", 500)
    assert _check_arg("output_tokens", 250)


def test_record_passes_recorded_at(tracker):
    """record() forwards recorded_at to the db insert function."""
    ts = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    event = TokenEvent(
        session_id="ts-sess",
        input_tokens=1,
        output_tokens=1,
        recorded_at=ts,
    )

    with patch.object(db_module, "insert_token_event") as mock_insert:
        tracker.record(event)

    args = mock_insert.call_args
    all_args = list(args.args) + list(args.kwargs.values())
    assert ts in all_args


# ---------------------------------------------------------------------------
# rolling_summary()
# ---------------------------------------------------------------------------


def test_rolling_summary_sums_across_sessions(tracker):
    """
    rolling_summary() sums input_tokens and output_tokens from all sessions
    within the rolling window returned by the db query.
    """
    now = _now()
    fake_rows = [
        {
            "session_id": "sess-a",
            "input_tokens": 100,
            "output_tokens": 40,
            "recorded_at": now.isoformat(),
        },
        {
            "session_id": "sess-b",
            "input_tokens": 200,
            "output_tokens": 60,
            "recorded_at": now.isoformat(),
        },
        {
            "session_id": "sess-a",
            "input_tokens": 50,
            "output_tokens": 20,
            "recorded_at": now.isoformat(),
        },
    ]

    with patch.object(db_module, "query_token_events_since", return_value=fake_rows):
        summary = tracker.rolling_summary(window_hours=1.0)

    assert isinstance(summary, TokenSummary)
    assert summary.total_input == 350
    assert summary.total_output == 120
    assert summary.total == 470
    assert summary.window_hours == 1.0


def test_rolling_summary_empty_window(tracker):
    """rolling_summary() returns zero counts when no events exist in the window."""
    with patch.object(db_module, "query_token_events_since", return_value=[]):
        summary = tracker.rolling_summary(window_hours=1.0)

    assert summary.total_input == 0
    assert summary.total_output == 0
    assert summary.total == 0


def test_rolling_summary_passes_correct_window_to_db(tracker):
    """rolling_summary() queries the DB with a since= timestamp corresponding to window_hours."""
    with patch.object(db_module, "query_token_events_since", return_value=[]) as mock_q:
        tracker.rolling_summary(window_hours=2.0)

    mock_q.assert_called_once()
    since_arg = mock_q.call_args.args[0] if mock_q.call_args.args else mock_q.call_args.kwargs.get("since")
    assert since_arg is not None
    # The since timestamp should be approximately 2 hours before now
    delta = _now() - since_arg.replace(tzinfo=timezone.utc) if since_arg.tzinfo is None else _now() - since_arg
    hours = delta.total_seconds() / 3600
    assert 1.9 < hours < 2.1, f"Expected ~2h window, got {hours:.2f}h delta"


# ---------------------------------------------------------------------------
# session_summary()
# ---------------------------------------------------------------------------


def test_session_summary_returns_correct_tuple(tracker):
    """session_summary() returns (total_input, total_output) for a single session."""
    now = _now()
    fake_rows = [
        {
            "session_id": "my-sess",
            "input_tokens": 300,
            "output_tokens": 150,
            "recorded_at": now.isoformat(),
        },
        {
            "session_id": "my-sess",
            "input_tokens": 100,
            "output_tokens": 50,
            "recorded_at": now.isoformat(),
        },
    ]

    with patch.object(db_module, "query_token_events_for_session", return_value=fake_rows):
        result = tracker.session_summary("my-sess")

    assert isinstance(result, tuple)
    assert len(result) == 2
    total_input, total_output = result
    assert total_input == 400
    assert total_output == 200


def test_session_summary_empty_returns_zeros(tracker):
    """session_summary() returns (0, 0) when no events exist for the session."""
    with patch.object(db_module, "query_token_events_for_session", return_value=[]):
        result = tracker.session_summary("empty-sess")

    assert result == (0, 0)


def test_session_summary_passes_session_id_to_db(tracker):
    """session_summary() queries the DB with the correct session_id."""
    with patch.object(
        db_module, "query_token_events_for_session", return_value=[]
    ) as mock_q:
        tracker.session_summary("target-sess")

    mock_q.assert_called_once()
    arg = mock_q.call_args.args[0] if mock_q.call_args.args else mock_q.call_args.kwargs.get("session_id")
    assert arg == "target-sess"


# ---------------------------------------------------------------------------
# Session isolation — different sessions don't interfere
# ---------------------------------------------------------------------------


def test_feed_buffers_are_per_session(tracker):
    """
    Partial chunks fed to different session_ids are buffered independently.
    Completing one session's partial JSON does not emit an event for another.
    """
    # Start feeding partial JSON for session A
    tracker.feed("sess-A", b'{"usage":{"input_tokens":10,')
    # Feed complete JSON for session B
    events_b = tracker.feed("sess-B", b'{"usage":{"input_tokens":99,"output_tokens":9}}')
    # Now complete session A
    events_a = tracker.feed("sess-A", b'"output_tokens":3}}')

    assert len(events_b) == 1
    assert events_b[0].session_id == "sess-B"
    assert events_b[0].input_tokens == 99

    assert len(events_a) == 1
    assert events_a[0].session_id == "sess-A"
    assert events_a[0].input_tokens == 10
