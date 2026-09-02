"""The data model: usage arithmetic, agent state, and live activity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_monitor.models import (
    AgentRun, ApiCall, ModelStat, Session, Usage, _distill_topic,
)


def now(offset_s: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_s)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_from_api_keeps_the_five_minute_and_one_hour_writes_apart():
    # The two TTLs bill at 1.25x and 2.0x input, so collapsing them would
    # misprice every long-lived cache.
    u = Usage.from_api({
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 40,
            "ephemeral_1h_input_tokens": 50,
        },
    })
    assert (u.cache_write_5m, u.cache_write_1h) == (40, 50)
    assert (u.input_tokens, u.output_tokens, u.cache_read) == (10, 20, 30)


def test_older_transcripts_flat_cache_write_counts_as_a_five_minute_write():
    u = Usage.from_api({"cache_creation_input_tokens": 500})
    assert (u.cache_write_5m, u.cache_write_1h) == (500, 0)


def test_the_explicit_split_wins_over_the_flat_total_when_both_are_present():
    u = Usage.from_api({
        "cache_creation_input_tokens": 999,
        "cache_creation": {"ephemeral_5m_input_tokens": 7,
                           "ephemeral_1h_input_tokens": 3},
    })
    assert (u.cache_write_5m, u.cache_write_1h) == (7, 3)


def test_from_api_tolerates_missing_and_null_fields():
    u = Usage.from_api({"input_tokens": None, "cache_creation": None,
                        "server_tool_use": None})
    assert u.total == 0


def test_from_api_picks_up_server_tool_requests():
    u = Usage.from_api({"server_tool_use": {"web_search_requests": 4}})
    assert u.web_search_requests == 4


def test_total_input_counts_every_billed_input_token():
    u = Usage(input_tokens=1, cache_write_5m=2, cache_write_1h=4,
              cache_read=8, output_tokens=16)
    assert u.total_input == 15
    assert u.total == 31


def test_cache_hit_rate_is_the_cached_share_of_input():
    assert Usage(input_tokens=25, cache_read=75).cache_hit_rate == 0.75
    # No input at all must not divide by zero.
    assert Usage(output_tokens=10).cache_hit_rate == 0.0


def test_add_accumulates_every_field():
    a = Usage(1, 2, 3, 4, 5, 6, 7)
    a.add(Usage(1, 2, 3, 4, 5, 6, 7))
    assert a == Usage(2, 4, 6, 8, 10, 12, 14)


def test_model_stat_merge_folds_calls_usage_and_cost():
    a = ModelStat(calls=1, usage=Usage(input_tokens=10), cost=1.0,
                  uncached_cost=2.0)
    a.merge(ModelStat(calls=2, usage=Usage(input_tokens=5), cost=0.5,
                      uncached_cost=1.5))
    assert a.calls == 3
    assert a.usage.input_tokens == 15
    assert (a.cost, a.uncached_cost) == (1.5, 3.5)


# ---------------------------------------------------------------------------
# ApiCall
# ---------------------------------------------------------------------------


def test_api_call_prices_itself_from_its_own_model_and_speed():
    usage = Usage(output_tokens=1_000_000)
    normal = ApiCall(timestamp=None, model="claude-opus-5", usage=usage)
    fast = ApiCall(timestamp=None, model="claude-opus-5", usage=usage, fast=True)
    assert normal.cost == pytest.approx(25.00)
    assert fast.cost == pytest.approx(50.00)


def test_api_call_uncached_cost_is_the_no_cache_counterfactual():
    call = ApiCall(timestamp=None, model="claude-opus-5",
                   usage=Usage(cache_read=1_000_000))
    assert call.cost == pytest.approx(0.50)
    assert call.uncached_cost == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# Topic distillation
# ---------------------------------------------------------------------------


def test_an_explicit_task_line_wins_over_surrounding_prose():
    prompt = (
        "You are a subagent. Follow the instructions carefully.\n"
        "TASK: Audit the parser for duplicate billing\n"
        "Report back as JSON.\n"
    )
    assert _distill_topic(prompt) == "Audit the parser for duplicate billing"


@pytest.mark.parametrize("marker", ["TASK:", "TASK —", "Goal:", "OBJECTIVE:",
                                    "mission:", "Your job:"])
def test_task_markers_are_recognised_case_insensitively(marker):
    assert _distill_topic(marker + " ship the thing") == "ship the thing"


def test_a_workflow_task_em_dash_beats_the_house_rules_preamble():
    prompt = (
        "HOUSE RULES: product language everywhere a person can see it\n"
        "A test server runs at http://localhost:5099.\n"
        "TASK — Trust.tsx, Money.tsx and ToBuild.tsx fixes. You own EXACTLY "
        "those three files in /home/smith/workspace/aifbox/client/src/admin/\n"
    )
    assert _distill_topic(prompt) == "Trust.tsx, Money.tsx and ToBuild.tsx fixes"


def test_a_work_package_line_beats_the_you_are_an_agent_preamble():
    prompt = (
        "You are an implementation agent working inside /home/dev/proj\n"
        "THE SPECIFICATION is /tmp/spec.md\n"
        "YOUR WORK PACKAGE: WP0.8 — Pairing hygiene: trunk-to-canopy merge\n"
        "Sub-waves 0a and 0b run together now.\n"
    )
    assert _distill_topic(prompt).startswith("WP0.8")


def test_a_marker_still_reads_through_markdown_bullets_and_hashes():
    assert _distill_topic("## TASK: rebuild the index") == "rebuild the index"


def test_long_filesystem_paths_collapse_so_the_subject_stays_visible():
    topic = _distill_topic("Review /home/dev/proj/claude_monitor/parser.py now")
    assert topic == "Review … now"


def test_a_bare_heading_is_skipped_for_the_first_real_line():
    assert _distill_topic("# Plan\nRewrite the cache invalidation logic") == (
        "Rewrite the cache invalidation logic"
    )


def test_the_topic_is_cut_at_a_sentence_boundary_when_one_falls_sensibly():
    assert _distill_topic(
        "Investigate the flaky test suite. Then report back."
    ) == "Investigate the flaky test suite"


def test_an_early_full_stop_is_not_treated_as_a_boundary():
    # Cutting at index < 25 would leave a uselessly short topic.
    assert _distill_topic("Fix it. Then rewrite the whole scheduler module") == (
        "Fix it. Then rewrite the whole scheduler module"
    )


def test_the_topic_is_capped_at_the_limit():
    assert len(_distill_topic("x" * 400)) == 90


def test_an_empty_prompt_distills_to_nothing():
    assert _distill_topic("") == ""
    assert _distill_topic("   \n\n  ") == ""


# ---------------------------------------------------------------------------
# AgentRun
# ---------------------------------------------------------------------------


def test_a_completed_agent_is_done_even_if_its_session_died():
    run = AgentRun(agent_id="a", session_id="s", completed=True,
                   ended=now(-100_000))
    assert run.state(parent_live=False) == "done"


def test_an_agent_writing_right_now_under_a_live_session_is_running():
    run = AgentRun(agent_id="a", session_id="s", ended=now(-5))
    assert run.state(parent_live=True) == "running"


def test_an_unfinished_agent_under_a_dead_session_is_stopped_not_running():
    # Without the parent-liveness check, every interrupted run would report
    # as still running for the rest of time.
    run = AgentRun(agent_id="a", session_id="s", ended=now(-5))
    assert run.state(parent_live=False) == "stopped"


def test_an_unfinished_agent_that_went_quiet_is_stopped():
    run = AgentRun(agent_id="a", session_id="s", ended=now(-3600))
    assert run.state(parent_live=True) == "stopped"
    assert run.state(parent_live=True, stale_after_s=7200) == "running"


def test_an_agent_that_never_wrote_anything_is_stopped():
    assert AgentRun(agent_id="a", session_id="s").state(parent_live=True) == "stopped"


def test_the_sidecar_description_is_the_agents_topic():
    run = AgentRun(agent_id="abcdef123456789", session_id="s",
                   description="Audit pricing", prompt="TASK: something else")
    assert run.topic == "Audit pricing"


def test_a_duplicated_label_is_distrusted_in_favour_of_the_prompt():
    # A description shared by two agents means Claude Code carried a stale
    # label onto a resumed run, so the prompt is the better source.
    run = AgentRun(agent_id="abcdef123456789", session_id="s",
                   description="Audit pricing", label_ambiguous=True,
                   prompt="TASK: rebuild the workflow index")
    assert run.topic == "rebuild the workflow index"


def test_an_agent_with_neither_label_nor_prompt_falls_back_to_its_id():
    run = AgentRun(agent_id="abcdef1234567890", session_id="s")
    assert run.topic == "abcdef123456"


def test_agent_duration_and_throughput():
    run = AgentRun(agent_id="a", session_id="s", started=now(-10),
                   ended=now(), usage=Usage(output_tokens=500))
    assert run.duration_s == pytest.approx(10, abs=0.5)
    assert run.output_tps == pytest.approx(50, rel=0.1)


def test_throughput_is_zero_rather_than_undefined_without_a_duration():
    assert AgentRun(agent_id="a", session_id="s").output_tps == 0.0


# ---------------------------------------------------------------------------
# Session rollups
# ---------------------------------------------------------------------------


def make_session(**kw) -> Session:
    kw.setdefault("session_id", "s1")
    kw.setdefault("path", "/tmp/s1.jsonl")
    kw.setdefault("project_dir", "-home-dev-proj")
    return Session(**kw)


def test_the_real_cwd_names_the_project_ahead_of_the_encoded_directory():
    s = make_session(cwd="/home/dev/claude-code-monitor")
    assert s.project == "claude-code-monitor"


def test_without_a_cwd_the_project_falls_back_to_the_encoded_directory():
    assert make_session(project_dir="-home-dev-proj").project == "proj"


def test_the_primary_model_is_the_most_used_one():
    s = make_session(models={"claude-sonnet-5": 2, "claude-opus-5": 9})
    assert s.primary_model == "claude-opus-5"
    assert make_session().primary_model == ""


def test_total_cost_includes_every_subagent():
    s = make_session(cost=1.0)
    s.agents = [AgentRun(agent_id="a", session_id="s1", cost=0.25),
                AgentRun(agent_id="b", session_id="s1", cost=0.75)]
    assert s.agent_cost == pytest.approx(1.0)
    assert s.total_cost == pytest.approx(2.0)


def test_total_usage_folds_the_agents_in():
    s = make_session(usage=Usage(output_tokens=100))
    s.agents = [AgentRun(agent_id="a", session_id="s1",
                         usage=Usage(output_tokens=50))]
    assert s.total_usage.output_tokens == 150
    # Folding must not mutate the session's own usage.
    assert s.usage.output_tokens == 100


def test_cache_savings_never_goes_negative():
    assert make_session(cost=2.0, uncached_cost=5.0).cache_savings == 3.0
    assert make_session(cost=5.0, uncached_cost=2.0).cache_savings == 0.0


def test_throughput_prefers_generation_time_over_wall_clock():
    # Wall-clock includes the hours a session sat idle waiting for a human,
    # which would report a fast session as glacially slow.
    s = make_session(usage=Usage(output_tokens=1000), active_seconds=10.0,
                     started=now(-3600), ended=now())
    assert s.output_tps == pytest.approx(100.0)


def test_throughput_falls_back_to_wall_clock_without_turn_durations():
    s = make_session(usage=Usage(output_tokens=1000), started=now(-100),
                     ended=now())
    assert s.output_tps == pytest.approx(10.0, rel=0.05)


def test_burn_rate_is_dollars_per_hour_over_the_span():
    s = make_session(cost=2.0, started=now(-1800), ended=now())
    assert s.burn_rate_hourly == pytest.approx(4.0, rel=0.05)


def test_a_session_that_never_wrote_anything_is_infinitely_idle():
    assert make_session().idle_s == float("inf")


def test_recent_velocity_only_counts_the_trailing_window():
    t = 1_000_000.0
    s = make_session(timeline=[
        (t, 900, 0, 0.0, 0.0),          # long before the window
        (t + 500, 100, 0, 0.0, 0.0),
        (t + 510, 100, 0, 0.0, 0.0),
        (t + 520, 100, 0, 0.0, 0.0),
    ])
    # Window covers the last three points: 300 tokens over a 20s span.
    assert s.recent_velocity(window_s=60) == pytest.approx(15.0)


def test_recent_velocity_needs_two_points_to_measure_a_rate():
    s = make_session(timeline=[(1_000_000.0, 500, 0, 0.0, 0.0)])
    assert s.recent_velocity(window_s=60) == 0.0
    assert make_session().recent_velocity() == 0.0
