"""Token composition: the per-call input split and every place it surfaces.

The dashboard leads with tokens by kind — fresh input, cache write, cache
read, output — so the split has to survive the whole path: transcript →
timeline point → daily/hourly buckets → JSON.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_monitor import analytics
from claude_monitor.models import AgentRun, Session, Usage, timeline_split
from claude_monitor.parser import parse_session_file
from claude_monitor.web import server as srv

UTC = timezone.utc


def _session(**kw) -> Session:
    kw.setdefault("session_id", "s1")
    kw.setdefault("path", "/tmp/s1.jsonl")
    kw.setdefault("project_dir", "-home-dev-proj")
    return Session(**kw)


# ---------------------------------------------------------------------------
# Timeline points
# ---------------------------------------------------------------------------


def test_a_timeline_point_carries_the_input_split(corpus_dir):
    s = corpus_dir.session()
    s.assistant(input=500, cache_read=120_000, write_5m=3_000, write_1h=1_000,
                output=250)
    (p,) = parse_session_file(s.build()).timeline
    # context total still adds up, and the split is the same numbers again:
    # a first call has nothing to re-send, so stub + writes are all fresh
    assert p[2] == 500 + 120_000 + 3_000 + 1_000
    assert timeline_split(p) == (4_500, 0, 120_000)
    assert p[1] == 250


def test_an_old_five_field_point_counts_its_whole_context_as_fresh():
    # Pre-v12 cache entries know only the context total. Calling all of it
    # fresh keeps every total right; the split is simply unknown.
    assert timeline_split((1.0, 10, 900, 0.1, 0.2)) == (900, 0, 0)
    # v12 points had the raw API split and no re-sent classification.
    assert timeline_split((1.0, 10, 1600, 0.1, 0.2, 100, 1000, 500)) == (600, 0, 1000)


# ---------------------------------------------------------------------------
# Re-sent classification
# ---------------------------------------------------------------------------


def test_a_cache_write_beyond_the_contexts_growth_is_a_resend(corpus_dir):
    # Claude Code caches the whole prompt, so the API's "cache write" mixes
    # genuinely new content with context written again after the cache
    # lapsed. On a full hit the write equals the context growth; the excess
    # is what the model had already read.
    s = corpus_dir.session()
    s.assistant(at=0, input=2, write_5m=30_000, output=100)            # cold start
    s.assistant(at=10, input=100, write_5m=2_000, cache_read=30_000,
                output=50)                                             # full hit
    s.assistant(at=900, input=100, write_5m=33_000, output=50)         # lapsed
    sess = parse_session_file(s.build())
    u = sess.usage
    assert u.resent == 32_000
    assert u.cache_misses == 1
    assert u.fresh_input == (2 + 30_000) + (100 + 2_000) + (100 + 33_000 - 32_000)
    assert [timeline_split(p) for p in sess.timeline] == [
        (30_002, 0, 0), (2_100, 0, 30_000), (1_100, 32_000, 0)]


def test_a_compaction_restarts_the_comparison(corpus_dir):
    s = corpus_dir.session()
    s.assistant(at=0, input=2, write_5m=90_000, output=100)
    s.assistant(at=10, input=100, write_5m=1_000, cache_read=90_000, output=50)
    # context drops to a summary: everything written is new, not re-sent
    s.assistant(at=20, input=100, write_5m=12_000, output=50)
    u = parse_session_file(s.build()).usage
    assert u.resent == 0 and u.cache_misses == 0


def test_a_shifted_breakpoint_is_attributed_but_not_counted_as_a_miss(corpus_dir):
    s = corpus_dir.session()
    s.assistant(at=0, input=2, write_5m=30_000, output=100)
    # 500 tokens re-written: an earlier prefix changed, not a lapsed cache
    s.assistant(at=10, input=100, write_5m=1_500, cache_read=29_500, output=50)
    u = parse_session_file(s.build()).usage
    assert u.resent == 500 and u.cache_misses == 0


def test_agents_classify_against_their_own_history(corpus_dir):
    s = corpus_dir.session()
    s.assistant(at=0, input=2, write_5m=50_000, output=10)
    a = s.agent("agent1", meta={"agentType": "Explore", "description": "x"})
    a.assistant(at=1, input=2, write_5m=8_000, output=10)
    a.assistant(at=2, input=50, write_5m=9_000, cache_read=0, output=10,
                stop_reason="end_turn")
    sess = parse_session_file(s.build())
    assert sess.usage.resent == 0
    # the agent's second call re-sent its own 8k, not the session's 50k
    assert sess.agents[0].usage.resent == 8_000


# ---------------------------------------------------------------------------
# Daily and window aggregation
# ---------------------------------------------------------------------------


def _at(day_offset: int, hour: int = 12) -> datetime:
    d = datetime.now(UTC).date() + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def test_the_daily_series_keeps_the_split_apart():
    s = _session(timeline=[
        (_at(0, 1).timestamp(), 50, 10_600, 1.0, 2.0, 100, 10_000, 500, 0),
        (_at(0, 2).timestamp(), 70, 20_900, 1.0, 2.0, 200, 20_000, 700, 600),
    ])
    b = dict(analytics.by_day([s], days=3))[datetime.now(UTC).date()]
    # point 1: 100 stub + 500 written, nothing re-sent  -> 600 fresh
    # point 2: 200 stub + 700 written, 600 of it re-sent -> 300 fresh
    assert b.usage.fresh_input == 900
    assert b.usage.resent == 600
    assert b.usage.cache_read == 30_000
    assert b.usage.output_tokens == 120
    assert b.usage.total == 900 + 600 + 30_000 + 120
    assert b.usage.cache_misses == 0          # below the 1024 floor

def test_an_agents_split_is_apportioned_across_the_days_it_ran():
    s = _session()
    s.agents = [AgentRun(agent_id="a", session_id="s1",
                         started=_at(-1, 23), ended=_at(0, 1),
                         usage=Usage(input_tokens=1000, cache_read=8000,
                                     cache_write_5m=500, cache_write_1h=500,
                                     output_tokens=200, resent=400,
                                     cache_misses=2))]
    series = dict(analytics.by_day([s], days=3))
    today = datetime.now(UTC).date()
    for d in (today, today - timedelta(days=1)):
        u = series[d].usage
        assert (u.fresh_input, u.resent, u.cache_read, u.output_tokens,
                u.cache_misses) == (800, 200, 4000, 100, 1)


def test_recent_tokens_reads_the_trailing_window_by_kind():
    now = datetime.now(UTC).timestamp()
    s = _session(timeline=[
        (now - 48 * 3600, 99, 9999, 0.0, 0.0, 9999, 0, 0, 0),      # outside
        (now - 3 * 3600, 10, 1100, 0.0, 0.0, 100, 1000, 0, 0),     # inside 24h
    ])
    s.agents = [AgentRun(agent_id="a", session_id="s1",
                         started=datetime.fromtimestamp(now - 7200, UTC),
                         ended=datetime.fromtimestamp(now - 3600, UTC),
                         usage=Usage(input_tokens=40, cache_read=400,
                                     cache_write_5m=10, output_tokens=8,
                                     resent=6))]
    t = analytics.recent_tokens([s], 86400.0)
    assert t == {"fresh": 144, "resent": 6, "cache_read": 1400, "output": 18}


def test_an_agent_with_a_single_call_still_counts_inside_the_window():
    # started == ended, so an overlap fraction would be 0/0 — it used to be
    # dropped from every trailing-window number.
    now = datetime.now(UTC).timestamp()
    s = _session()
    s.agents = [AgentRun(agent_id="a", session_id="s1", cost=2.0,
                         started=datetime.fromtimestamp(now - 60, UTC),
                         ended=datetime.fromtimestamp(now - 60, UTC),
                         usage=Usage(input_tokens=7, output_tokens=3))]
    assert analytics.recent_tokens([s], 3600.0)["fresh"] == 7
    assert analytics.recent_rates([s], 3600.0)[0] == pytest.approx(2.0)
    # and one that finished before the window contributes nothing
    s.agents[0].started = s.agents[0].ended = datetime.fromtimestamp(now - 7200, UTC)
    assert analytics.recent_tokens([s], 3600.0)["fresh"] == 0


def test_recent_tokens_on_nothing_is_all_zero():
    assert analytics.recent_tokens([], 60.0) == {
        "fresh": 0, "resent": 0, "cache_read": 0, "output": 0}


# ---------------------------------------------------------------------------
# JSON surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client(corpus_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "CLAUDE_STATE", tmp_path / "claude.json")
    monkeypatch.setattr(srv, "CREDENTIALS", tmp_path / "credentials.json")
    monkeypatch.setattr(srv.ResourceMonitor, "attach", lambda self, sessions: None)
    s = corpus_dir.session(project="monitor", cwd="/home/dev/monitor",
                           session_id="aaaaaaaa-1111-2222-3333-444444444444")
    s.assistant(at=0, input=500, cache_read=120_000, write_5m=3_000,
                write_1h=1_000, output=250)
    s.assistant(at=10, input=100, cache_read=124_000, write_5m=0, output=50)
    s.agent("agent1", meta={"agentType": "Explore", "description": "Map"}
            ).assistant(input=20, cache_read=2000, output=10,
                        stop_reason="end_turn")
    s.build()
    app = srv.create_app(claude_dir=corpus_dir.root, allow_network=False)
    app.config["TESTING"] = True
    store = app.config["STORE"]
    store._publish(store.corpus.load())
    return app.test_client()


def test_the_session_brief_and_detail_carry_the_split(client):
    brief = client.get("/api/sessions").get_json()["sessions"][0]
    # main: 4,500 fresh then 100 fresh; agent: 20 fresh — nothing re-sent
    assert brief["split"] == {"fresh": 4_620, "resent": 0,
                              "cache_read": 246_000, "output": 310}
    assert brief["tokens"] == sum(brief["split"].values())
    assert brief["insights"]["reread_x"] == pytest.approx(246_000 / 4_620)

    d = client.get(f"/api/sessions/{brief['id']}").get_json()
    p = d["timeline"][0]
    assert (p["fresh"], p["resent"], p["cache_read"], p["out"]) == (
        4_500, 0, 120_000, 250)
    assert d["agents"][0]["split"] == {"fresh": 20, "resent": 0,
                                       "cache_read": 2000, "output": 10}
    hourly = [b for b in d["hourly"] if b["out"]]
    assert sum(b["fresh"] for b in hourly) == 4_620
    assert sum(b["cache_read"] for b in hourly) == 246_000


def test_downsampling_sums_every_kind_and_keeps_the_context_peak():
    pts = [(float(i), 1, 1000 + i, 0.01, 0.02, 10, 900, 90 + i, 5) for i in range(20)]
    out = srv._downsample(pts, 4)
    assert len(out) == 4
    # fresh per point = 10 stub + (90 + i) written - 5 re-sent
    assert sum(p["fresh"] for p in out) == sum(95 + i for i in range(20))
    assert sum(p["resent"] for p in out) == 100
    assert sum(p["cache_read"] for p in out) == 18_000
    assert out[-1]["ctx"] == 1019


def test_the_summary_daily_series_and_totals_carry_the_split(client):
    d = client.get("/api/summary").get_json()
    today = [r for r in d["daily"] if r["output"]]
    assert sum(r["fresh"] for r in today) == 4_620
    assert sum(r["resent"] for r in today) == 0
    assert sum(r["cache_read"] for r in today) == 246_000
    assert sum(r["output"] for r in today) == 310
    assert d["totals"]["split"]["cache_read"] == 246_000
    assert d["projects"][0]["split"]["fresh"] == 4_620
    assert d["insights"]["cache_misses"] == 0
    assert all("tokens" in c for c in d["heatmap"])
    # the flow card quotes cache tiers as multiples of the input rate, and
    # prices one model's streams from its cost by token type
    assert d["cache_mult"] == {"write_5m": 1.25, "write_1h": 2.0, "read": 0.1}
    m = d["models"][0]
    assert sum(m["cost_by_type"].values()) == pytest.approx(m["cost"])
    assert m["cache_misses"] == 0


def test_the_live_poll_reports_tokens_in_the_last_day(client):
    t = client.get("/api/live").get_json()["tokens_24h"]
    assert t == {"fresh": 4_620, "resent": 0, "cache_read": 246_000,
                 "output": 310}
