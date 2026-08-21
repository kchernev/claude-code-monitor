"""``Session.activity()`` — the live "what is it doing right now" readout.

This is a state machine over the tail of the transcript, and every branch of
it is a claim shown to the user in the UI. The cases below are the ones where
a naive reading gets it wrong: leftovers from a killed process, a turn that
already ended, and a tool call the CLI has not flushed yet.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_monitor.models import Session


def make_live(*, tail=None, pending=(), ended_ago=5.0, **kw) -> Session:
    """A session the resource monitor has attached a live pid to."""
    s = Session(session_id="s1", path="/tmp/s1.jsonl",
                project_dir="-home-dev-proj", **kw)
    s.pid = 4242
    s.ended = datetime.now(timezone.utc) - timedelta(seconds=ended_ago)
    s.tail = tail or {}
    s.pending_tools = list(pending)
    return s


def tool(name="Bash", *, ago=3.0, text="ls -la", sub="list files"):
    return {"name": name, "text": text, "sub": sub, "ts": time.time() - ago}


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_a_session_with_no_process_reports_no_activity():
    s = Session(session_id="s1", path="/tmp/s1.jsonl", project_dir="p")
    assert s.is_live is False
    assert s.activity() is None


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def test_a_finished_turn_is_waiting_for_the_user():
    s = make_live(tail={"kind": "assistant", "stop": "end_turn",
                        "ts": time.time() - 5})
    a = s.activity()
    assert a["state"] == "waiting"
    assert a["label"] == "waiting for your input"


def test_a_stop_sequence_ends_the_turn_the_same_way():
    s = make_live(tail={"kind": "assistant", "stop": "stop_sequence",
                        "ts": time.time() - 5})
    assert s.activity()["state"] == "waiting"


def test_an_interrupted_turn_says_so():
    s = make_live(tail={"kind": "interrupt", "ts": time.time() - 5})
    a = s.activity()
    assert a["state"] == "waiting"
    assert "interrupted" in a["label"]


def test_an_empty_tail_is_treated_as_waiting():
    assert make_live().activity()["state"] == "waiting"


# ---------------------------------------------------------------------------
# Tools in flight
# ---------------------------------------------------------------------------


def test_an_unresolved_tool_call_is_reported_as_running():
    s = make_live(tail={"kind": "assistant", "stop": None,
                        "ts": time.time() - 10},
                  pending=[tool("Bash", ago=8)])
    a = s.activity()
    assert a["state"] == "tool"
    assert a["label"] == "running Bash"
    assert a["detail"] == "ls -la"
    assert a["sub"] == "list files"
    assert a["since_s"] == pytest.approx(8, abs=1)


def test_concurrent_tool_calls_are_counted_and_all_returned():
    s = make_live(tail={"kind": "assistant", "stop": None, "ts": time.time()},
                  pending=[tool("Read", ago=30), tool("Grep", ago=20),
                           tool("Bash", ago=10)])
    a = s.activity()
    # The newest names the state; the rest are counted, and the UI gets them all.
    assert a["label"] == "running Bash +2 more"
    assert [t["name"] for t in a["tools"]] == ["Read", "Grep", "Bash"]
    assert a["tools"][0]["since_s"] == pytest.approx(30, abs=1)


def test_a_tool_call_a_crash_left_open_forever_is_ignored():
    # No foreground tool runs for two hours; a record this old is a leftover
    # from a killed process, not work in progress.
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 5},
                  pending=[tool("Bash", ago=7201)])
    a = s.activity()
    assert a["state"] == "thinking"
    assert a["label"] == "working on the prompt"


def test_a_pending_tool_without_a_timestamp_is_ignored():
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 5},
                  pending=[{"name": "Bash", "text": "", "sub": "", "ts": None}])
    assert s.activity()["state"] == "thinking"


def test_a_finished_turn_never_reports_a_leftover_tool_as_running():
    # The turn ended cleanly, so anything still open is debris — reporting it
    # would show a permanently "running" tool on an idle session.
    s = make_live(tail={"kind": "assistant", "stop": "end_turn",
                        "ts": time.time() - 5},
                  pending=[tool("Bash", ago=4)])
    assert s.activity()["state"] == "waiting"


def test_an_interrupt_clears_the_in_flight_turn_too():
    s = make_live(tail={"kind": "interrupt", "ts": time.time() - 5},
                  pending=[tool("Bash", ago=4)])
    assert s.activity()["state"] == "waiting"


# ---------------------------------------------------------------------------
# Between tool calls
# ---------------------------------------------------------------------------


def test_a_returned_tool_result_names_what_just_came_back():
    # The CLI flushes a tool_use record only with its result, so the tool that
    # just returned is the freshest evidence of what the session is doing.
    s = make_live(tail={"kind": "result", "ts": time.time() - 2,
                        "tool": {"name": "Grep", "text": "TODO", "sub": "*.py"}})
    a = s.activity()
    assert a["state"] == "thinking"
    assert a["label"] == "working — after Grep"
    assert a["tool_name"] == "Grep"
    assert a["detail"] == "TODO"


def test_a_result_with_no_recorded_tool_still_reads_as_working():
    s = make_live(tail={"kind": "result", "ts": time.time() - 2})
    a = s.activity()
    assert a["state"] == "thinking"
    assert a["label"] == "processing tool results"


def test_a_fresh_prompt_means_the_model_is_working_on_it():
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 1})
    a = s.activity()
    assert a["state"] == "thinking"
    assert a["label"] == "working on the prompt"


# ---------------------------------------------------------------------------
# Mid-response
# ---------------------------------------------------------------------------


def test_an_open_assistant_record_reads_as_writing_a_response():
    s = make_live(tail={"kind": "assistant", "stop": None,
                        "ts": time.time() - 5})
    a = s.activity()
    assert a["state"] == "responding"
    assert a["label"] == "writing a response"


def test_a_long_silence_mid_response_is_reported_as_work_not_typing():
    # Text chunks land every few seconds while genuinely writing; a long quiet
    # stretch is an unflushed tool call, and saying "writing" would mislead.
    s = make_live(tail={"kind": "assistant", "stop": None,
                        "ts": time.time() - 120}, ended_ago=120)
    a = s.activity()
    assert a["state"] == "responding"
    assert a["label"] == "working — running a tool or thinking"


# ---------------------------------------------------------------------------
# Stall detection and context
# ---------------------------------------------------------------------------


def test_work_with_nothing_written_for_a_long_time_is_flagged_stalled():
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 1000},
                  ended_ago=1000)
    assert s.activity()["stalled"] is True


def test_a_session_merely_waiting_for_you_is_not_stalled():
    s = make_live(tail={"kind": "assistant", "stop": "end_turn",
                        "ts": time.time() - 100_000}, ended_ago=100_000)
    assert s.activity()["stalled"] is False


def test_active_states_carry_the_prompt_they_are_answering():
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 1},
                  last_prompt="rewrite the cache layer")
    assert s.activity()["prompt"] == "rewrite the cache layer"


def test_a_waiting_session_does_not_echo_a_prompt():
    s = make_live(tail={"kind": "assistant", "stop": "end_turn",
                        "ts": time.time() - 1},
                  last_prompt="rewrite the cache layer")
    assert "prompt" not in s.activity()


def test_the_prompt_falls_back_to_the_last_browsable_one():
    s = make_live(tail={"kind": "prompt", "ts": time.time() - 1},
                  prompts=[{"ts": "x", "text": "first"},
                           {"ts": "y", "text": "most recent"}])
    assert s.activity()["prompt"] == "most recent"
