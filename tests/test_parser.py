"""Transcript parsing: deduplication, attribution, agents and the tail."""

from __future__ import annotations

import json

import pytest

from claude_monitor.parser import (
    billing_key, iter_records, parse_agent_file, parse_session_file,
    session_from_dict, session_to_dict, tool_call_text, _is_human_turn,
    _read_agent_meta,
)

from conftest import tool_use, usage_block


# ---------------------------------------------------------------------------
# Record iteration
# ---------------------------------------------------------------------------


def test_iter_records_yields_each_line_with_its_end_offset(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    recs = list(iter_records(p))
    assert [r for r, _ in recs] == [{"a": 1}, {"a": 2}]
    assert [o for _, o in recs] == [9, 18]


def test_a_partial_trailing_line_is_skipped_not_fatal(tmp_path):
    # A transcript being appended to concurrently routinely ends mid-line.
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"a": 2', encoding="utf-8")
    assert [r for r, _ in iter_records(p)] == [{"a": 1}]


def test_blank_lines_and_garbage_are_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n\n   \nnot json at all\n{"a": 2}\n', encoding="utf-8")
    assert [r for r, _ in iter_records(p)] == [{"a": 1}, {"a": 2}]


def test_iter_records_can_resume_from_an_offset(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    assert [r for r, _ in iter_records(p, start_offset=9)] == [{"a": 2}]


def test_a_missing_file_yields_nothing_rather_than_raising(tmp_path):
    assert list(iter_records(tmp_path / "nope.jsonl")) == []


# ---------------------------------------------------------------------------
# Billing identity
# ---------------------------------------------------------------------------


def test_message_id_and_request_id_together_identify_an_api_call():
    rec = {"message": {"id": "msg_1"}, "requestId": "req_1", "uuid": "u1"}
    assert billing_key(rec) == ("call", "msg_1", "req_1")


def test_the_same_message_retried_as_a_new_request_is_a_separate_call():
    a = billing_key({"message": {"id": "msg_1"}, "requestId": "req_1"})
    b = billing_key({"message": {"id": "msg_1"}, "requestId": "req_2"})
    assert a != b


def test_billing_identity_degrades_through_message_id_then_uuid():
    assert billing_key({"message": {"id": "msg_1"}}) == ("msg", "msg_1")
    assert billing_key({"uuid": "u1"}) == ("uuid", "u1")


def test_records_with_no_identity_at_all_are_never_conflated():
    # A counter, never id(rec): CPython recycles addresses between loop
    # iterations, so two unrelated records could share one and the second
    # would be silently dropped as a duplicate.
    keys = {billing_key({}) for _ in range(50)}
    assert len(keys) == 50


# ---------------------------------------------------------------------------
# Deduplication of replayed calls
# ---------------------------------------------------------------------------


def test_a_replayed_call_is_billed_exactly_once(corpus_dir):
    # Resuming a session replays earlier messages into the transcript. Summing
    # them naively inflated one observed session's output tokens 4.8x.
    s = corpus_dir.session()
    for _ in range(5):
        s.assistant(msg_id="msg_1", request_id="req_1", output=1000)
    sess = parse_session_file(s.build())
    assert sess.api_calls == 1
    assert sess.usage.output_tokens == 1000


def test_genuinely_distinct_calls_are_all_billed(corpus_dir):
    s = corpus_dir.session()
    for i in range(3):
        s.assistant(msg_id=f"msg_{i}", request_id=f"req_{i}", output=100)
    sess = parse_session_file(s.build())
    assert sess.api_calls == 3
    assert sess.usage.output_tokens == 300


def test_dedup_spans_the_main_thread_and_every_subagent(corpus_dir):
    # The same billed call appearing in two files is still one API call.
    s = corpus_dir.session()
    s.assistant(msg_id="msg_shared", request_id="req_shared", output=500)
    agent = s.agent("a1")
    agent.assistant(msg_id="msg_shared", request_id="req_shared", output=500)
    agent.assistant(msg_id="msg_own", request_id="req_own", output=70)
    sess = parse_session_file(s.build())

    assert sess.api_calls == 1
    assert [a.api_calls for a in sess.agents] == [1]
    assert sess.usage.output_tokens + sess.agents[0].usage.output_tokens == 570


def test_a_record_with_no_usage_object_is_not_an_api_call(corpus_dir):
    s = corpus_dir.session()
    s.raw({"type": "assistant", "timestamp": "2026-08-20T12:00:00Z",
           "uuid": "u1", "message": {"role": "assistant", "model": "claude-opus-5"}})
    assert parse_session_file(s.build()).api_calls == 0


# ---------------------------------------------------------------------------
# Cost attribution
# ---------------------------------------------------------------------------


def test_each_call_is_priced_with_its_own_model_never_apportioned(corpus_dir):
    # Opus output is 5x Haiku's. Splitting a session total by call count would
    # be wrong by a factor of several; per-model stats are kept per call.
    s = corpus_dir.session()
    s.assistant(model="claude-opus-5", output=1_000_000)
    s.assistant(model="claude-haiku-4-5", output=1_000_000)
    sess = parse_session_file(s.build())

    assert sess.per_model["claude-opus-5"].cost == pytest.approx(25.0)
    assert sess.per_model["claude-haiku-4-5"].cost == pytest.approx(5.0)
    assert sess.cost == pytest.approx(30.0)
    assert sess.models == {"claude-opus-5": 1, "claude-haiku-4-5": 1}


def test_the_fast_speed_flag_reprices_the_call(corpus_dir):
    s = corpus_dir.session()
    s.assistant(model="claude-opus-5", output=1_000_000,
                usage=usage_block(output=1_000_000, fast=True))
    assert parse_session_file(s.build()).cost == pytest.approx(50.0)


def test_peak_context_is_the_largest_input_any_call_saw(corpus_dir):
    s = corpus_dir.session()
    s.assistant(cache_read=120_000, input=500)
    s.assistant(cache_read=40_000, input=100)
    assert parse_session_file(s.build()).peak_context == 120_500


def test_the_timeline_is_sorted_even_when_records_are_not(corpus_dir):
    s = corpus_dir.session()
    s.assistant(at=300, output=1)
    s.assistant(at=100, output=2)
    s.assistant(at=200, output=3)
    timeline = parse_session_file(s.build()).timeline
    assert [p[0] for p in timeline] == sorted(p[0] for p in timeline)
    assert [p[1] for p in timeline] == [2, 3, 1]


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------


def test_session_level_fields_are_harvested_from_the_records(corpus_dir):
    s = corpus_dir.session(project="monitor", cwd="/home/dev/monitor")
    s.meta(branch="feature/x", version="3.1.0")
    s.title("Rebuild the parse cache")
    s.last_prompt("now make it incremental")
    s.permission_mode("acceptEdits")
    s.file_edit("/home/dev/monitor/parser.py")
    s.file_edit("/home/dev/monitor/parser.py")
    sess = parse_session_file(s.build())

    assert sess.cwd == "/home/dev/monitor"
    assert sess.project == "monitor"
    assert sess.git_branch == "feature/x"
    assert sess.version == "3.1.0"
    assert sess.title == "Rebuild the parse cache"
    assert sess.last_prompt == "now make it incremental"
    assert sess.permission_mode == "acceptEdits"
    assert sess.files_touched == {"/home/dev/monitor/parser.py": 2}


def test_generation_time_comes_from_turn_duration_records(corpus_dir):
    s = corpus_dir.session()
    s.turn_duration(1500, uuid="d1")
    s.turn_duration(2500, uuid="d2")
    s.turn_duration(1500, uuid="d1")          # replayed on resume
    assert parse_session_file(s.build()).active_seconds == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Human turns vs harness noise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "<system-reminder>stay on task</system-reminder>",
    "<task-notification>agent finished</task-notification>",
    "<local-command-stdout>ok</local-command-stdout>",
    "<command-name>/loop</command-name>",
    "[Image: screenshot.png]",
    "Caveat: The messages below were generated by the user",
])
def test_harness_injected_text_is_not_a_human_turn(text):
    assert _is_human_turn({"message": {"role": "user", "content": text}}) is False


def test_the_same_screening_applies_to_list_form_content():
    # Older transcripts carry no origin.kind, so list content has to be
    # screened by its first text block exactly as string content is.
    rec = {"message": {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>noise</system-reminder>"}]}}
    assert _is_human_turn(rec) is False


def test_a_tool_result_carrier_is_not_a_human_turn():
    rec = {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    assert _is_human_turn(rec) is False


def test_a_non_human_origin_disqualifies_a_turn():
    rec = {"origin": {"kind": "agent"},
           "message": {"role": "user", "content": "do it"}}
    assert _is_human_turn(rec) is False


def test_a_real_typed_prompt_is_a_human_turn():
    rec = {"origin": {"kind": "human"},
           "message": {"role": "user", "content": "please refactor this"}}
    assert _is_human_turn(rec) is True


def test_prompts_are_captured_for_browsing_with_their_source(corpus_dir):
    s = corpus_dir.session()
    s.user("first thing", prompt_source="typed")
    s.user("<system-reminder>ignore me</system-reminder>")
    s.user_result("tu_1")
    s.user("second thing", prompt_source="paste")
    sess = parse_session_file(s.build())

    assert sess.user_turns == 2
    assert [p["text"] for p in sess.prompts] == ["first thing", "second thing"]
    assert [p["source"] for p in sess.prompts] == ["typed", "paste"]


def test_prompt_capture_is_capped(corpus_dir):
    s = corpus_dir.session()
    for i in range(205):
        s.user(f"prompt {i}")
    sess = parse_session_file(s.build())
    assert sess.user_turns == 205
    assert len(sess.prompts) == 200


def test_failed_tool_results_are_counted(corpus_dir):
    s = corpus_dir.session()
    s.user_result("t1", is_error=True)
    s.user_result("t2", is_error=True)
    s.user_result("t3")
    assert parse_session_file(s.build()).tool_errors == 2


def test_tool_calls_are_counted_per_name(corpus_dir):
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Bash", {"command": "ls"}, "a"),
                       tool_use("Read", {"file_path": "/x"}, "b")])
    s.assistant(tools=[tool_use("Bash", {"command": "pwd"}, "c")])
    assert parse_session_file(s.build()).tool_counts == {"Bash": 2, "Read": 1}


# ---------------------------------------------------------------------------
# Tool call summaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,inp,expected", [
    ("Bash", {"command": "ls -la", "description": "List files"},
     ("ls -la", "List files")),
    ("Read", {"file_path": "/home/dev/x.py"}, ("/home/dev/x.py", "")),
    ("Glob", {"pattern": "**/*.py", "path": "/src"}, ("**/*.py", "/src")),
    ("Grep", {"pattern": "TODO", "glob": "*.py"}, ("TODO", "*.py")),
    ("WebSearch", {"query": "flask csrf"}, ("flask csrf", "")),
    ("Skill", {"skill": "code-review", "args": "high"}, ("code-review", "high")),
    ("TodoWrite", {"todos": [1, 2, 3]}, ("3 todo items", "")),
])
def test_tool_call_text_summarises_each_tool_by_its_own_key_field(
        name, inp, expected):
    assert tool_call_text(name, inp) == expected


def test_an_unknown_tool_falls_back_to_a_json_preview():
    primary, sub = tool_call_text("SomeNewTool", {"alpha": 1, "beta": 2})
    assert '"alpha": 1' in primary and sub == ""


def test_tool_call_text_survives_a_non_dict_input():
    assert tool_call_text("Bash", "just a string")[0] == "just a string"


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def test_the_meta_sidecar_supplies_type_description_and_depth(corpus_dir):
    s = corpus_dir.session()
    s.agent("a1", meta={"agentType": "Explore", "description": "Map the parser",
                        "spawnDepth": 2}).assistant(output=10)
    run = parse_session_file(s.build()).agents[0]
    assert (run.subagent_type, run.description, run.spawn_depth) == (
        "Explore", "Map the parser", 2)


def test_a_sidecar_is_matched_on_the_full_stem_not_the_last_dot(tmp_path):
    # with_suffix() cuts at the last dot, so an agent id containing one would
    # resolve to a *different* agent's sidecar and inherit its label.
    d = tmp_path / "subagents"
    d.mkdir()
    (d / "agent-a.b.jsonl").write_text("", encoding="utf-8")
    (d / "agent-a.b.meta.json").write_text(
        json.dumps({"description": "correct"}), encoding="utf-8")
    (d / "agent-a.meta.json").write_text(
        json.dumps({"description": "WRONG agent"}), encoding="utf-8")
    assert _read_agent_meta(d / "agent-a.b.jsonl")["description"] == "correct"


def test_only_the_leading_agent_marker_is_stripped_from_the_id(tmp_path):
    d = tmp_path / "subagents"
    d.mkdir()
    p = d / "agent-wf-agent-7.jsonl"
    p.write_text("", encoding="utf-8")
    assert parse_agent_file(p, "s1").agent_id == "wf-agent-7"


@pytest.mark.parametrize("blob", ['{"broken": ', b"\xff\xfe not utf-8"])
def test_an_unreadable_sidecar_never_fails_the_parse(tmp_path, blob):
    d = tmp_path / "subagents"
    d.mkdir()
    (d / "agent-a.jsonl").write_text("", encoding="utf-8")
    sidecar = d / "agent-a.meta.json"
    if isinstance(blob, bytes):
        sidecar.write_bytes(blob)
    else:
        sidecar.write_text(blob, encoding="utf-8")
    assert _read_agent_meta(d / "agent-a.jsonl") == {}


def test_a_sidecar_that_is_not_an_object_is_ignored(tmp_path):
    d = tmp_path / "subagents"
    d.mkdir()
    (d / "agent-a.jsonl").write_text("", encoding="utf-8")
    (d / "agent-a.meta.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_agent_meta(d / "agent-a.jsonl") == {}


def test_an_agent_is_complete_only_when_it_ends_its_turn(corpus_dir):
    s = corpus_dir.session()
    s.agent("done").assistant(stop_reason="end_turn", text="here is the answer")
    s.agent("cut").assistant(stop_reason=None, output=5)
    runs = {a.agent_id: a for a in parse_session_file(s.build()).agents}

    assert runs["done"].completed is True
    assert runs["done"].final_message == "here is the answer"
    assert runs["cut"].completed is False


def test_an_agents_span_covers_its_first_and_last_record(corpus_dir):
    s = corpus_dir.session()
    a = s.agent("a1")
    a.assistant(at=10, output=1)
    a.assistant(at=70, output=1)
    run = parse_session_file(s.build()).agents[0]
    assert run.duration_s == pytest.approx(60.0)


def test_the_parent_thread_supplies_what_the_sidecar_lacks(corpus_dir):
    # An Agent tool result carries the agentId that links the two together.
    s = corpus_dir.session()
    s.spawn_agent("a1", description="Audit pricing", subagent_type="Explore",
                  prompt="TASK: audit", model="claude-sonnet-5")
    s.agent("a1").assistant(model="claude-sonnet-5", output=10)
    run = parse_session_file(s.build()).agents[0]

    assert run.description == "Audit pricing"
    assert run.subagent_type == "Explore"
    assert run.prompt == "TASK: audit"
    assert run.model == "claude-sonnet-5"


def test_an_agent_that_has_not_run_yet_takes_its_model_from_the_parent(
        corpus_dir):
    s = corpus_dir.session()
    s.spawn_agent("a1", description="Not started", model="claude-haiku-4-5")
    s.agent("a1")                      # transcript exists but is still empty
    run = parse_session_file(s.build()).agents[0]
    assert run.model == "claude-haiku-4-5"


def test_the_agents_own_transcript_outranks_the_parents_resolved_model(
        corpus_dir):
    # The parent records what it asked for; the transcript records what
    # actually ran, which is what the bill is computed from.
    s = corpus_dir.session()
    s.spawn_agent("a1", description="Audit", model="claude-haiku-4-5")
    s.agent("a1").assistant(model="claude-opus-5", output=10)
    assert parse_session_file(s.build()).agents[0].model == "claude-opus-5"


def test_an_agent_call_with_no_transcript_yet_still_shows_up(corpus_dir):
    # Work that was launched but has written nothing must not vanish.
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Agent", {"description": "Pending work",
                                          "subagent_type": "Plan"}, "tu_x")])
    sess = parse_session_file(s.build())
    assert [(a.agent_id, a.description) for a in sess.agents] == [
        ("(pending)", "Pending work")]


def test_a_label_shared_by_two_agents_is_flagged_as_untrustworthy(corpus_dir):
    # A duplicated description is the observable symptom of a stale label
    # surviving a resume, so the UI must fall back to the prompt.
    s = corpus_dir.session()
    for aid in ("a1", "a2"):
        s.agent(aid, meta={"description": "Same label"}).assistant(output=1)
    s.agent("a3", meta={"description": "Distinct"}).assistant(output=1)
    runs = {a.agent_id: a for a in parse_session_file(s.build()).agents}

    assert runs["a1"].label_ambiguous and runs["a2"].label_ambiguous
    assert not runs["a3"].label_ambiguous


# ---------------------------------------------------------------------------
# Workflow fan-outs
# ---------------------------------------------------------------------------


def test_workflow_agents_are_tagged_from_their_directory(corpus_dir):
    s = corpus_dir.session()
    s.agent("w1", workflow="wf_abc123").assistant(output=5)
    s.agent("plain").assistant(output=5)
    runs = {a.agent_id: a for a in parse_session_file(s.build()).agents}

    assert runs["w1"].workflow_id == "wf_abc123"
    assert runs["plain"].workflow_id == ""


def test_the_journal_supplies_a_workflow_agents_result(corpus_dir):
    s = corpus_dir.session()
    s.agent("w1", workflow="wf_abc123").assistant(output=5)
    s.journal("wf_abc123", "w1", "found 3 issues")
    run = parse_session_file(s.build()).agents[0]

    assert run.result == "found 3 issues"
    # A recorded return value proves the agent finished, even with no end_turn.
    assert run.completed is True


def test_a_structured_journal_result_is_serialised(corpus_dir):
    s = corpus_dir.session()
    s.agent("w1", workflow="wf_abc").assistant(output=5)
    s.journal("wf_abc", "w1", {"issues": 3})
    assert json.loads(parse_session_file(s.build()).agents[0].result) == {
        "issues": 3}


# ---------------------------------------------------------------------------
# Transcript tail (drives the live activity readout)
# ---------------------------------------------------------------------------


def test_an_unanswered_tool_call_is_left_pending(corpus_dir):
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Bash", {"command": "sleep 60"}, "tu_1")])
    sess = parse_session_file(s.build())

    assert [t["name"] for t in sess.pending_tools] == ["Bash"]
    assert sess.pending_tools[0]["text"] == "sleep 60"
    assert sess.tail["kind"] == "assistant"


def test_a_returned_result_closes_its_pending_call(corpus_dir):
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Bash", {"command": "ls"}, "tu_1")])
    s.user_result("tu_1")
    sess = parse_session_file(s.build())

    assert sess.pending_tools == []
    assert sess.tail["kind"] == "result"
    assert sess.tail["tool"]["name"] == "Bash"


def test_an_interrupt_abandons_every_in_flight_call(corpus_dir):
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Bash", {"command": "ls"}, "tu_1"),
                       tool_use("Read", {"file_path": "/x"}, "tu_2")])
    s.interrupt()
    sess = parse_session_file(s.build())

    assert sess.pending_tools == []
    assert sess.tail["kind"] == "interrupt"


def test_an_interrupt_delivered_inside_a_tool_result_also_counts(corpus_dir):
    s = corpus_dir.session()
    s.assistant(tools=[tool_use("Bash", {"command": "ls"}, "tu_1")])
    s.user_result("tu_1", content="[Request interrupted by user for tool use]")
    assert parse_session_file(s.build()).tail["kind"] == "interrupt"


def test_only_the_newest_few_open_calls_are_kept(corpus_dir):
    # A crash can leave arbitrarily many calls unresolved forever; the readout
    # only ever shows the last of them.
    s = corpus_dir.session()
    for i in range(6):
        s.assistant(tools=[tool_use("Bash", {"command": f"cmd{i}"}, f"tu_{i}")])
    sess = parse_session_file(s.build())

    assert len(sess.pending_tools) == 3
    assert [t["text"] for t in sess.pending_tools] == ["cmd3", "cmd4", "cmd5"]


def test_a_plain_prompt_is_the_tail_when_nothing_is_in_flight(corpus_dir):
    s = corpus_dir.session()
    s.user("do the thing")
    assert parse_session_file(s.build()).tail["kind"] == "prompt"


# ---------------------------------------------------------------------------
# Cache serialisation
# ---------------------------------------------------------------------------


def test_a_session_survives_a_round_trip_through_the_cache_format(corpus_dir):
    s = corpus_dir.session(project="monitor", cwd="/home/dev/monitor")
    s.meta(branch="main", version="3.0.0")
    s.title("A session")
    s.last_prompt("keep going")
    s.assistant(model="claude-opus-5", output=250, cache_read=9000,
                tools=[tool_use("Bash", {"command": "ls"}, "tu_1")])
    s.user_result("tu_1", is_error=True)
    s.user("a human prompt")
    s.turn_duration(2000)
    s.file_edit("/home/dev/monitor/x.py")
    s.agent("a1", meta={"agentType": "Explore", "description": "Look around"}
            ).assistant(output=40, stop_reason="end_turn")
    original = parse_session_file(s.build())

    restored = session_from_dict(session_to_dict(original))

    for field in ("session_id", "cwd", "title", "last_prompt", "git_branch",
                  "version", "started", "ended", "cost", "uncached_cost",
                  "api_calls", "user_turns", "models", "tool_counts",
                  "tool_errors", "peak_context", "active_seconds", "prompts",
                  "files_touched", "timeline", "tail", "pending_tools"):
        assert getattr(restored, field) == getattr(original, field), field
    assert restored.usage == original.usage
    assert restored.per_model.keys() == original.per_model.keys()
    assert [a.agent_id for a in restored.agents] == [
        a.agent_id for a in original.agents]
    assert restored.agents[0].description == "Look around"
    assert restored.agents[0].completed is True
    assert restored.total_cost == pytest.approx(original.total_cost)


def test_the_round_trip_is_json_safe(corpus_dir):
    s = corpus_dir.session()
    s.assistant(output=10, tools=[tool_use("Bash", {"command": "ls"}, "tu_1")])
    blob = session_to_dict(parse_session_file(s.build()))
    assert json.loads(json.dumps(blob)) is not None
