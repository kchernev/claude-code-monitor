"""Aggregations: daily attribution, fan-out shape, and the derived rates."""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone

import pytest

from claude_monitor import analytics
from claude_monitor.models import AgentRun, ModelStat, Session, Usage

UTC = timezone.utc


def at(day_offset: int, hour: int = 12, minute: int = 0) -> datetime:
    """A UTC instant on a day relative to today, for window-anchored maths."""
    d = datetime.now(UTC).date() + timedelta(days=day_offset)
    return datetime.combine(d, dt_time(hour, minute), tzinfo=UTC)


def make_session(sid="s1", project="proj", **kw) -> Session:
    kw.setdefault("cwd", f"/home/dev/{project}")
    return Session(session_id=sid, path=f"/tmp/{sid}.jsonl",
                   project_dir=f"-home-dev-{project}", **kw)


def agent(aid="a1", sid="s1", **kw) -> AgentRun:
    return AgentRun(agent_id=aid, session_id=sid, **kw)


def stat(model_usage, cost=0.0, uncached=0.0, calls=1) -> ModelStat:
    return ModelStat(calls=calls, usage=model_usage, cost=cost,
                     uncached_cost=uncached)


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------


def test_totals_include_every_subagent():
    s = make_session(cost=1.0, uncached_cost=2.0, api_calls=3,
                     usage=Usage(output_tokens=100), active_seconds=10.0)
    s.agents = [agent(cost=0.5, uncached_cost=1.0, api_calls=2,
                      usage=Usage(output_tokens=50))]
    b = analytics.totals([s])

    assert b.sessions == 1 and b.agents == 1
    assert b.api_calls == 5
    assert b.cost == pytest.approx(1.5)
    assert b.usage.output_tokens == 150
    assert b.savings == pytest.approx(1.5)


def test_bucket_savings_never_goes_negative():
    b = analytics.Bucket(key="x", cost=5.0, uncached_cost=2.0)
    assert b.savings == 0.0
    assert b.savings_pct == 0.0


def test_projects_are_grouped_and_ranked_by_spend():
    a = make_session("s1", project="cheap", cost=1.0)
    b = make_session("s2", project="pricey", cost=9.0)
    c = make_session("s3", project="cheap", cost=2.0)
    buckets = analytics.by_project([a, b, c])

    assert [x.key for x in buckets] == ["pricey", "cheap"]
    assert buckets[1].cost == pytest.approx(3.0)
    assert buckets[1].sessions == 2


def test_models_are_grouped_from_exact_per_call_attribution():
    # Never apportion a session total across models by call count: an Opus
    # call and a Haiku call differ by 5x on output.
    s = make_session(per_model={
        "claude-opus-5": stat(Usage(output_tokens=1000), cost=0.025, calls=1),
        "claude-haiku-4-5": stat(Usage(output_tokens=9000), cost=0.045, calls=9),
    })
    s.agents = [agent(per_model={
        "claude-opus-5": stat(Usage(output_tokens=500), cost=0.0125, calls=1)})]
    buckets = {b.key: b for b in analytics.by_model([s])}

    assert buckets["claude-opus-5"].usage.output_tokens == 1500
    assert buckets["claude-opus-5"].api_calls == 2
    assert buckets["claude-opus-5"].sessions == 1
    assert buckets["claude-opus-5"].agents == 1
    assert buckets["claude-haiku-4-5"].api_calls == 9


def test_agent_types_are_rolled_up_with_their_runtime():
    s = make_session()
    s.agents = [
        agent("a1", subagent_type="Explore", cost=1.0,
              started=at(0, 10), ended=at(0, 10, 5)),
        agent("a2", subagent_type="Explore", cost=2.0),
        agent("a3", cost=0.5),
    ]
    buckets = {b.key: b for b in analytics.agent_type_summary([s])}

    assert buckets["Explore"].agents == 2
    assert buckets["Explore"].cost == pytest.approx(3.0)
    assert buckets["Explore"].active_seconds == pytest.approx(300.0)
    assert "(unspecified)" in buckets


def test_tools_are_split_between_the_main_thread_and_subagents():
    s = make_session(tool_counts={"Bash": 5, "Read": 2})
    s.agents = [agent(tool_counts={"Bash": 1, "Grep": 7})]
    rows = analytics.tool_summary([s])

    assert rows[0] == ("Grep", 0, 7)          # highest total first
    assert dict((n, (m, sub)) for n, m, sub in rows)["Bash"] == (5, 1)


def test_cache_efficiency_reports_the_realised_saving():
    s = make_session(cost=1.0, uncached_cost=4.0,
                     usage=Usage(input_tokens=100, cache_read=900))
    eff = analytics.cache_efficiency([s])

    assert eff["hit_rate"] == pytest.approx(0.9)
    assert eff["savings"] == pytest.approx(3.0)
    assert eff["savings_pct"] == pytest.approx(75.0)


def test_cache_efficiency_on_an_empty_corpus_does_not_divide_by_zero():
    eff = analytics.cache_efficiency([])
    assert eff["hit_rate"] == 0.0 and eff["savings_pct"] == 0.0


# ---------------------------------------------------------------------------
# Daily attribution
# ---------------------------------------------------------------------------


def test_spend_lands_on_the_day_each_call_happened():
    # A multi-day session used to put its whole bill on day one, which is why
    # the daily chart disagreed with the window total.
    s = make_session(timeline=[
        (at(-1, 23).timestamp(), 100, 1000, 1.0, 2.0),
        (at(0, 1).timestamp(), 200, 2000, 3.0, 6.0),
        (at(0, 2).timestamp(), 300, 3000, 5.0, 10.0),
    ])
    series = dict(analytics.by_day([s], days=7))
    today = datetime.now(UTC).date()

    assert series[today].cost == pytest.approx(8.0)
    assert series[today].api_calls == 2
    assert series[today - timedelta(days=1)].cost == pytest.approx(1.0)


def test_the_series_covers_the_whole_window_with_gaps_filled():
    s = make_session(timeline=[(at(-4).timestamp(), 10, 100, 1.0, 1.0)])
    series = analytics.by_day([s], days=5)

    assert len(series) == 5
    assert series[0][0] == datetime.now(UTC).date() - timedelta(days=4)
    assert series[-1][0] == datetime.now(UTC).date()
    assert [b.cost for _d, b in series[1:]] == [0.0, 0.0, 0.0, 0.0]


def test_an_empty_corpus_produces_no_series():
    assert analytics.by_day([], days=7) == []


def test_an_agent_spanning_midnight_is_split_across_both_days():
    s = make_session()
    s.agents = [agent(started=at(-1, 23), ended=at(0, 1), cost=4.0,
                      uncached_cost=8.0, api_calls=6,
                      usage=Usage(output_tokens=600))]
    series = dict(analytics.by_day([s], days=7))
    today = datetime.now(UTC).date()

    # A two-hour run, one hour either side of midnight.
    assert series[today - timedelta(days=1)].cost == pytest.approx(2.0)
    assert series[today].cost == pytest.approx(2.0)
    # Run count and calls belong to the day it started, undivided.
    assert series[today - timedelta(days=1)].agents == 1
    assert series[today - timedelta(days=1)].api_calls == 6
    assert series[today].agents == 0


def test_an_instantaneous_agent_is_not_divided_by_zero():
    s = make_session()
    s.agents = [agent(started=at(0, 10), ended=at(0, 10), cost=3.0)]
    series = dict(analytics.by_day([s], days=3))
    assert series[datetime.now(UTC).date()].cost == pytest.approx(3.0)


def test_an_agent_that_never_started_is_skipped():
    s = make_session()
    s.agents = [agent(cost=99.0)]
    assert analytics.by_day([s], days=3) == []


def test_each_day_counts_the_sessions_that_were_active_on_it():
    a = make_session("s1", timeline=[(at(0, 1).timestamp(), 1, 1, 1.0, 1.0),
                                     (at(0, 2).timestamp(), 1, 1, 1.0, 1.0)])
    b = make_session("s2", timeline=[(at(0, 3).timestamp(), 1, 1, 1.0, 1.0)])
    series = dict(analytics.by_day([a, b], days=3))
    assert series[datetime.now(UTC).date()].sessions == 2


# ---------------------------------------------------------------------------
# Workflow fan-outs
# ---------------------------------------------------------------------------


def test_workflow_agents_group_by_run_within_their_session():
    s = make_session()
    s.agents = [
        agent("a1", workflow_id="wf_1", started=at(0, 1)),
        agent("a2", workflow_id="wf_1", started=at(0, 2)),
        agent("a3", workflow_id="wf_2", started=at(0, 3)),
        agent("a4"),                                   # not a workflow agent
    ]
    runs = analytics.all_workflows([s])

    assert [w.workflow_id for w in runs] == ["wf_2", "wf_1"]   # newest first
    assert len(runs[1].agents) == 2


def test_the_same_workflow_id_in_two_sessions_stays_separate():
    a, b = make_session("s1"), make_session("s2")
    a.agents = [agent("a1", sid="s1", workflow_id="wf_1", started=at(0, 1))]
    b.agents = [agent("a2", sid="s2", workflow_id="wf_1", started=at(0, 2))]
    assert len(analytics.all_workflows([a, b])) == 2


def test_a_workflow_spans_its_earliest_start_to_its_latest_end():
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1", agents=[
        agent("a1", started=at(0, 10), ended=at(0, 11)),
        agent("a2", started=at(0, 9), ended=at(0, 12)),
    ])
    assert wf.started == at(0, 9)
    assert wf.ended == at(0, 12)
    assert wf.duration_s == pytest.approx(3 * 3600)


def test_peak_parallelism_is_the_widest_moment_not_the_agent_count():
    # Ten agents run two at a time is a very different shape from ten at once.
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1", agents=[
        agent("a1", started=at(0, 10, 0), ended=at(0, 10, 30)),
        agent("a2", started=at(0, 10, 10), ended=at(0, 10, 40)),
        agent("a3", started=at(0, 10, 20), ended=at(0, 10, 50)),
        agent("a4", started=at(0, 11, 0), ended=at(0, 11, 10)),
    ])
    assert len(wf.agents) == 4
    assert wf.peak_parallelism == 3


def test_agents_that_merely_touch_do_not_count_as_parallel():
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1", agents=[
        agent("a1", started=at(0, 10), ended=at(0, 11)),
        agent("a2", started=at(0, 11), ended=at(0, 12)),
    ])
    assert wf.peak_parallelism == 1


def test_parallelism_of_a_workflow_with_no_finished_agents_is_zero():
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1",
                               agents=[agent("a1", started=at(0, 10))])
    assert wf.peak_parallelism == 0
    assert analytics.WorkflowRun("wf_1", "s1").duration_s == 0.0


def test_a_workflow_rolls_up_the_cost_and_usage_of_its_fan_out():
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1", agents=[
        agent("a1", cost=1.5, usage=Usage(output_tokens=10)),
        agent("a2", cost=2.5, usage=Usage(output_tokens=20)),
    ])
    assert wf.cost == pytest.approx(4.0)
    assert wf.usage.output_tokens == 30


def test_a_workflow_counts_its_agents_by_state():
    now = datetime.now(UTC)
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1",
                               parent_live=True, agents=[
        agent("a1", completed=True),
        agent("a2", ended=now - timedelta(seconds=5)),
        agent("a3", ended=now - timedelta(hours=3)),
    ])
    assert (wf.completed, wf.running, wf.stopped) == (1, 1, 1)


def test_the_workflow_topic_is_the_most_common_agent_topic():
    wf = analytics.WorkflowRun(workflow_id="wf_1", session_id="s1", agents=[
        agent("a1", description="review the parser"),
        agent("a2", description="review the parser"),
        agent("a3", description="something else"),
    ])
    assert wf.topic == "review the parser"


def test_a_tie_between_topics_always_breaks_the_same_way():
    # Iterating a set would let the title change between runs, because string
    # hashing is salted per process.
    agents = [agent("a1", description="zebra"), agent("a2", description="alpha")]
    forwards = analytics.WorkflowRun("wf_1", "s1", agents=agents).topic
    backwards = analytics.WorkflowRun("wf_1", "s1",
                                      agents=list(reversed(agents))).topic
    assert forwards == backwards == "alpha"


def test_a_workflow_with_no_topics_falls_back_to_its_id():
    assert analytics.WorkflowRun("wf_1", "s1").topic == "wf_1"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_agents_are_listed_newest_first_across_every_session():
    a, b = make_session("s1"), make_session("s2")
    a.agents = [agent("old", sid="s1", started=at(-2))]
    b.agents = [agent("new", sid="s2", started=at(0)),
                agent("undated", sid="s2")]
    assert [r.agent_id for r in analytics.all_agents([a, b])] == [
        "new", "old", "undated"]


def test_running_agents_are_pinned_above_whatever_the_sort_was():
    now = datetime.now(UTC)
    runs = [
        agent("cheap-done", completed=True),
        agent("running-now", sid="live", ended=now - timedelta(seconds=2)),
        agent("also-done", completed=True),
    ]
    pinned = analytics.pin_running(runs, live_session_ids={"live"})

    assert pinned[0].agent_id == "running-now"
    # The partition is stable, so the rest keep the caller's order.
    assert [r.agent_id for r in pinned[1:]] == ["cheap-done", "also-done"]


def test_top_sessions_can_rank_by_each_supported_key():
    a = make_session("s1", cost=10.0, usage=Usage(output_tokens=1),
                     started=at(0, 1), ended=at(0, 2))
    b = make_session("s2", cost=1.0, usage=Usage(output_tokens=999))
    b.agents = [agent("x"), agent("y")]

    assert [s.session_id for s in analytics.top_sessions([a, b], key="cost")][0] == "s1"
    assert [s.session_id for s in analytics.top_sessions([a, b], key="tokens")][0] == "s2"
    assert [s.session_id for s in analytics.top_sessions([a, b], key="agents")][0] == "s2"
    assert [s.session_id for s in analytics.top_sessions([a, b], key="duration")][0] == "s1"
    # An unknown key falls back to cost rather than raising.
    assert [s.session_id for s in analytics.top_sessions([a, b], key="nope")][0] == "s1"
    assert len(analytics.top_sessions([a, b], n=1)) == 1


def test_live_sessions_are_pinned_to_the_top_of_the_recent_list():
    stale = make_session("stale", ended=at(0, 23))
    live = make_session("live", ended=at(-3))
    live.pid = 1234
    assert [s.session_id for s in analytics.recent_sessions([stale, live])] == [
        "live", "stale"]


# ---------------------------------------------------------------------------
# Rates and series
# ---------------------------------------------------------------------------


def test_recent_rates_only_count_the_trailing_window():
    # A lifetime average keeps reporting an hours-old burst as if it were
    # happening this minute.
    now = datetime.now(UTC).timestamp()
    s = make_session(timeline=[
        (now - 7200, 10_000, 0, 100.0, 100.0),        # long past
        (now - 60, 900, 0, 1.0, 1.0),                 # inside the window
    ])
    usd_per_hour, tps = analytics.recent_rates([s], window_s=900)

    assert usd_per_hour == pytest.approx(1.0 / 900 * 3600)
    assert tps == pytest.approx(1.0)


def test_recent_rates_apportion_an_agent_by_its_overlap_with_the_window():
    now = datetime.now(UTC)
    s = make_session()
    s.agents = [agent(started=now - timedelta(seconds=1800),
                      ended=now - timedelta(seconds=900) + timedelta(seconds=450),
                      cost=2.0, usage=Usage(output_tokens=1000))]
    # The run spans 1350s, of which 450s falls inside a 900s window: one third.
    usd_per_hour, _tps = analytics.recent_rates([s], window_s=900)
    assert usd_per_hour == pytest.approx(2.0 / 3 / 900 * 3600, rel=0.05)


def test_recent_rates_on_an_idle_corpus_are_zero():
    assert analytics.recent_rates([make_session()]) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Usage inside an absolute window (plan-limit attribution)
# ---------------------------------------------------------------------------


def test_window_usage_counts_only_calls_inside_the_window():
    now = datetime.now(UTC)
    lo, hi = now - timedelta(hours=5), now + timedelta(hours=1)
    s = make_session(project="proj", per_model={
        "claude-opus-5": stat(Usage(), cost=4.0, calls=2),
    })
    s.timeline = [
        ((now - timedelta(hours=6)).timestamp(), 100, 1000, 1.0, 1.0),
        ((now - timedelta(hours=1)).timestamp(), 200, 2000, 3.0, 3.0),
    ]
    w = analytics.window_usage([s], lo.timestamp(), hi.timestamp())

    assert w["cost"] == pytest.approx(3.0)
    assert w["tokens"] == 2200
    assert len(w["projects"]) == 1
    p = w["projects"][0]
    assert p["name"] == "proj"
    assert p["cost"] == pytest.approx(3.0)
    assert p["tokens"] == 2200
    assert p["share"] == pytest.approx(1.0)
    m = w["models"][0]
    assert m["name"] == "claude-opus-5"
    assert m["label"] == "Opus 5"
    assert m["cost"] == pytest.approx(3.0)


def test_window_usage_apportions_agents_by_their_overlap():
    now = datetime.now(UTC)
    lo, hi = now - timedelta(hours=1), now + timedelta(hours=1)
    s = make_session(project="proj")
    # Half of a two-hour agent falls inside the one-hour window.
    s.agents = [
        agent("a1", started=now - timedelta(hours=2), ended=now,
              cost=4.0, usage=Usage(output_tokens=1000),
              per_model={"claude-opus-5": stat(Usage(), cost=4.0)}),
        # An agent that finished before the window contributes nothing.
        agent("a2", started=now - timedelta(hours=3),
              ended=now - timedelta(hours=2), cost=10.0,
              usage=Usage(output_tokens=10_000),
              per_model={"claude-opus-5": stat(Usage(), cost=10.0)}),
    ]
    w = analytics.window_usage([s], lo.timestamp(), hi.timestamp())

    assert w["cost"] == pytest.approx(2.0)
    assert w["projects"][0]["cost"] == pytest.approx(2.0)
    assert w["projects"][0]["tokens"] == 500
    assert w["models"][0]["cost"] == pytest.approx(2.0)


def test_window_usage_counts_a_running_agent_as_ending_now():
    now = datetime.now(UTC)
    lo, hi = now - timedelta(hours=5), now + timedelta(hours=4)
    s = make_session(project="proj")
    s.agents = [agent("a1", started=now - timedelta(minutes=10), ended=None,
                      cost=2.0, usage=Usage(output_tokens=100),
                      per_model={"claude-opus-5": stat(Usage(), cost=2.0)})]
    w = analytics.window_usage([s], lo.timestamp(), hi.timestamp())
    assert w["cost"] == pytest.approx(2.0)


def test_window_usage_splits_multi_model_sessions_by_cost_share():
    now = datetime.now(UTC)
    lo, hi = now - timedelta(hours=5), now + timedelta(hours=1)
    s = make_session(project="proj", per_model={
        "claude-opus-5": stat(Usage(), cost=3.0, calls=1),
        "claude-haiku-4-5": stat(Usage(), cost=1.0, calls=9),
    })
    s.timeline = [((now - timedelta(hours=1)).timestamp(), 100, 900, 2.0, 2.0)]
    w = analytics.window_usage([s], lo.timestamp(), hi.timestamp())

    models = {m["name"]: m for m in w["models"]}
    assert models["claude-opus-5"]["cost"] == pytest.approx(1.5)
    assert models["claude-haiku-4-5"]["cost"] == pytest.approx(0.5)
    # Projects stay exact even when the model split is apportioned.
    assert w["projects"][0]["cost"] == pytest.approx(2.0)


def test_window_usage_on_an_empty_window_has_no_rows():
    now = datetime.now(UTC)
    w = analytics.window_usage(
        [make_session()], (now - timedelta(hours=5)).timestamp(),
        now.timestamp())
    assert w["cost"] == 0.0 and w["tokens"] == 0
    assert w["projects"] == [] and w["models"] == []


def test_token_economics_parts_sum_to_the_whole():
    s = make_session(per_model={
        "claude-opus-5": stat(Usage(input_tokens=1000, output_tokens=2000,
                                    cache_read=50_000, cache_write_5m=8000)),
        "claude-haiku-4-5": stat(Usage(input_tokens=500, output_tokens=100)),
    })
    econ = analytics.token_economics([s])

    assert econ["total_cost"] == pytest.approx(sum(econ["cost"].values()))
    assert econ["total_tokens"] == sum(econ["tokens"].values())
    assert econ["tokens"]["output"] == 2100
    assert 0 < econ["output_share"] < 1


def test_the_effective_output_rate_prices_a_token_of_real_work():
    # One million output tokens on Opus 5 costs $25 outright; the context
    # re-reads that produced them push the effective rate above list.
    s = make_session(per_model={
        "claude-opus-5": stat(Usage(output_tokens=1_000_000,
                                    cache_read=10_000_000)),
    })
    econ = analytics.token_economics([s])

    assert econ["list_output_rate"] == pytest.approx(25.0)
    assert econ["effective_output_rate"] == pytest.approx(30.0)
    assert econ["multiple_of_list"] == pytest.approx(1.2)


def test_token_economics_on_an_empty_corpus_is_all_zeroes():
    econ = analytics.token_economics([])
    assert econ["total_cost"] == 0.0
    assert econ["effective_output_rate"] == 0.0
    assert econ["multiple_of_list"] == 0.0


def test_velocity_is_bucketed_into_a_tokens_per_second_series():
    s = make_session(timeline=[
        (100.0, 30, 0, 0.0, 0.0),
        (110.0, 30, 0, 0.0, 0.0),      # same 30s bucket as the first
        (200.0, 60, 0, 0.0, 0.0),
    ])
    assert analytics.velocity_series(s, bucket_s=30) == [(90.0, 2.0), (180.0, 2.0)]


def test_a_session_with_one_data_point_has_no_velocity_series():
    assert analytics.velocity_series(make_session(timeline=[(1.0, 1, 0, 0.0, 0.0)])) == []


def test_context_and_cost_series_read_straight_off_the_timeline():
    s = make_session(timeline=[(10.0, 5, 1000, 1.0, 2.0),
                               (20.0, 5, 3000, 2.0, 4.0)])
    assert analytics.context_series(s) == [(10.0, 1000), (20.0, 3000)]
    # Cost is cumulative — it is a spend curve, not a per-call series.
    assert analytics.cost_series(s) == [(10.0, 1.0), (20.0, 3.0)]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_a_sparkline_is_always_exactly_the_requested_width():
    assert len(analytics.sparkline([], width=24)) == 24
    assert len(analytics.sparkline([1, 2, 3], width=24)) == 24
    assert len(analytics.sparkline(list(range(100)), width=24)) == 24


def test_a_sparkline_spans_the_block_range_from_low_to_high():
    line = analytics.sparkline([0, 1, 2, 3, 4, 5, 6, 7], width=8)
    assert line[0] == "▁" and line[-1] == "█"


def test_a_flat_series_renders_flat_rather_than_dividing_by_zero():
    assert analytics.sparkline([5, 5, 5, 5], width=4) == "▁▁▁▁"


def test_a_short_series_is_left_aligned_and_padded_with_empty_buckets():
    # The padding is zero, so it also sets the floor of the scale: a series
    # shorter than the chart reads as "nothing here yet" on the right.
    assert analytics.sparkline([5, 5, 5], width=4) == "███▁"


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (45, "45s"),
    (192, "3m 12s"),
    (3840, "1h 04m"),
    (200_000, "2d 7h"),
])
def test_fmt_duration(seconds, expected):
    assert analytics.fmt_duration(seconds) == expected


def test_fmt_duration_guards_against_infinity_and_nan():
    assert analytics.fmt_duration(float("inf")) == "—"
    assert analytics.fmt_duration(float("nan")) == "—"
    assert analytics.fmt_duration(None) == "—"


def test_fmt_ago():
    assert analytics.fmt_ago(None) == "—"
    assert analytics.fmt_ago(datetime.now(UTC) + timedelta(seconds=30)) == "now"
    assert analytics.fmt_ago(
        datetime.now(UTC) - timedelta(seconds=120)).endswith(" ago")
