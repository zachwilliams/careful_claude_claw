"""
TokenTracker — parses PTY output for Claude token-usage events and persists
them to SQLite via db.insert_token_event.

Implements TokenTrackerProtocol from interfaces.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from careful_claude_claw import db
from careful_claude_claw.dashboard.interfaces import (
    SessionID,
    TokenEvent,
    TokenSummary,
    TokenTrackerProtocol,
)

# Matches ANSI CSI escape sequences (colours, cursor movement, erase, etc.)
_ANSI_ESCAPE = re.compile(rb"\x1b\[[0-9;]*[mGKHF]")

# Maximum per-session buffer size (~4 KB). Excess bytes at the front are
# discarded to prevent unbounded memory growth from malformed output.
_BUFFER_MAX = 4096


class TokenTracker:
    """Parses PTY output for token usage and maintains a rolling hourly count.

    Satisfies TokenTrackerProtocol (runtime_checkable).
    """

    def __init__(self) -> None:
        # Per-session byte buffers for incomplete JSON fragments.
        self._buffers: dict[SessionID, bytes] = {}

    # ------------------------------------------------------------------
    # TokenTrackerProtocol interface
    # ------------------------------------------------------------------

    def feed(self, session_id: SessionID, data: bytes) -> list[TokenEvent]:
        """Feed raw PTY output bytes; return any TokenEvents parsed."""
        buf = self._buffers.get(session_id, b"") + data

        # Cap buffer to prevent unbounded growth.
        if len(buf) > _BUFFER_MAX:
            buf = buf[-_BUFFER_MAX:]

        # Strip ANSI escape codes before trying to parse JSON.
        clean = _ANSI_ESCAPE.sub(b"", buf)

        events: list[TokenEvent] = []
        offset = 0

        while True:
            usage_pos = clean.find(b'"usage":', offset)
            if usage_pos == -1:
                # No more "usage": markers; keep only the tail that could
                # contain the start of a future marker.
                tail_start = max(0, len(clean) - len(b'"usage":') + 1)
                buf = buf[tail_start:] if tail_start > 0 else b""
                break

            # Find the opening brace of the usage object.
            brace_pos = clean.find(b"{", usage_pos + len(b'"usage":'))
            if brace_pos == -1:
                # Brace not yet arrived — keep from the "usage": marker onward.
                buf = buf[usage_pos:]
                break

            # Try to extract a complete JSON object starting at brace_pos.
            obj_bytes, end_pos = _extract_json_object(clean, brace_pos)
            if obj_bytes is None:
                if end_pos == -1:
                    # Incomplete — keep the buffer from the "usage": position.
                    buf = buf[usage_pos:]
                    break
                else:
                    # Malformed — skip past the bad fragment and continue.
                    offset = end_pos
                    continue

            # Attempt to parse the extracted object.
            try:
                obj = json.loads(obj_bytes)
                input_tokens = int(obj["input_tokens"])
                output_tokens = int(obj["output_tokens"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                # Skip this fragment.
                offset = end_pos
                continue

            event = TokenEvent(
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                recorded_at=datetime.utcnow(),
            )
            events.append(event)
            offset = end_pos

        self._buffers[session_id] = buf
        return events

    def record(self, event: TokenEvent) -> None:
        """Persist a TokenEvent to the SQLite token_events table."""
        db.insert_token_event(
            session_id=event.session_id,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            recorded_at=event.recorded_at,
        )

    def rolling_summary(self, window_hours: float = 1.0) -> TokenSummary:
        """Return aggregate token usage across all sessions in the last window_hours."""
        since = datetime.utcnow() - timedelta(hours=window_hours)
        rows = db.query_token_events_since(since)
        total_input = sum(r["input_tokens"] for r in rows)
        total_output = sum(r["output_tokens"] for r in rows)
        return TokenSummary(
            total_input=total_input,
            total_output=total_output,
            window_hours=window_hours,
            recorded_at=datetime.utcnow(),
        )

    def session_summary(self, session_id: SessionID) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) totals for one session."""
        rows = db.query_token_events_for_session(session_id)
        total_input = sum(r["input_tokens"] for r in rows)
        total_output = sum(r["output_tokens"] for r in rows)
        return total_input, total_output


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _extract_json_object(data: bytes, start: int) -> tuple[bytes | None, int]:
    """Extract a complete JSON object from *data* beginning at *start*.

    Returns:
        (object_bytes, end_index)  — on success; end_index is the position
                                     immediately after the closing '}'.
        (None, -1)                 — object is incomplete (more data needed).
        (None, end_index)          — object is syntactically bad; caller should
                                     skip to end_index and try again.
    """
    if start >= len(data) or data[start:start + 1] != b"{":
        return None, start + 1

    depth = 0
    in_string = False
    escape_next = False
    i = start

    while i < len(data):
        ch = data[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if in_string:
            if ch == ord("\\"):
                escape_next = True
            elif ch == ord('"'):
                in_string = False
            i += 1
            continue

        if ch == ord('"'):
            in_string = True
        elif ch == ord("{"):
            depth += 1
        elif ch == ord("}"):
            depth -= 1
            if depth == 0:
                obj_bytes = data[start: i + 1]
                return obj_bytes, i + 1

        i += 1

    # Reached end of buffer without closing brace — incomplete.
    return None, -1


# Verify the class structurally satisfies the protocol at import time.
assert isinstance(TokenTracker(), TokenTrackerProtocol), (
    "TokenTracker does not satisfy TokenTrackerProtocol"
)
