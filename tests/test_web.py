"""The local web server: origin guards, filtering and the JSON API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_monitor.models import ModelStat, Session
from claude_monitor.web import server as srv
from claude_monitor.web.server import (
    _hostname_of, _limit_entry, _plan_label, host_is_trusted, plan_payload,
)

from conftest import tool_use


@pytest.fixture(autouse=True)
def _no_real_credentials(tmp_path, monkeypatch):
    """Point the plan card at throwaway files.

    Both paths are module constants resolved from ``Path.home()`` at import
    time, so redirecting home in a fixture comes too late — a test would read
    the developer's real ``.credentials.json``.
    """
    monkeypatch.setattr(srv, "CLAUDE_STATE", tmp_path / "claude.json")
    monkeypatch.setattr(srv, "CREDENTIALS", tmp_path / "credentials.json")


@pytest.fixture(autouse=True)
def _no_process_scan(monkeypatch):
    """Keep tests off the machine's real process table."""
    monkeypatch.setattr(
        "claude_monitor.resources.ResourceMonitor.attach",
        lambda self, sessions: [],
    )


@pytest.fixture
def populated(corpus_dir):
    """A corpus with two sessions, a subagent and a workflow fan-out."""
    a = corpus_dir.session(project="monitor", cwd="/home/dev/monitor",
                           session_id="aaaaaaaa-1111-2222-3333-444444444444")
    a.meta(branch="main", version="3.0.0")
    a.title("Rebuild the parse cache")
    a.assistant(model="claude-opus-5", output=1_000_000,
                tools=[tool_use("Bash", {"command": "pytest"}, "tu_1")])
    a.user_result("tu_1")
    a.user("make the cache incremental")
    a.agent("agent1", meta={"agentType": "Explore",
                            "description": "Map the parser"}
            ).assistant(model="claude-opus-5", output=10, stop_reason="end_turn")
    a.agent("wfa1", workflow="wf_deadbeef", meta={"description": "Review"}
            ).assistant(output=5, stop_reason="end_turn")
    a.workflow_script("wf_deadbeef", "review-changes")
    a.build()

    b = corpus_dir.session(project="other", cwd="/home/dev/other",
                           session_id="bbbbbbbb-1111-2222-3333-444444444444")
    b.title("Something else")
    b.assistant(model="claude-haiku-4-5", output=1000)
    b.user("a different topic entirely")
    b.build()
    return corpus_dir


@pytest.fixture
def client(populated):
    app = srv.create_app(claude_dir=populated.root, allow_network=False)
    app.config["TESTING"] = True
    store = app.config["STORE"]
    # Publish a snapshot without starting the background refresh thread.
    store._publish(store.corpus.load())
    return app.test_client()


# ---------------------------------------------------------------------------
# Host parsing and the DNS-rebinding guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [
    ("localhost:8787", "localhost"),
    ("127.0.0.1:8787", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"),
    ("[::1]:8787", "::1"),
    ("[::1]", "::1"),
    ("", ""),
    ("  localhost  ", "localhost"),
])
def test_the_hostname_is_taken_apart_from_the_port_and_brackets(header, expected):
    assert _hostname_of(header) == expected


@pytest.mark.parametrize("host", [
    "localhost:8787", "localhost", "app.localhost",
    "127.0.0.1:8787", "0.0.0.0", "192.168.1.20:8787", "[::1]:8787",
])
def test_ip_literals_and_localhost_are_trusted(host):
    assert host_is_trusted(host) is True


@pytest.mark.parametrize("host", [
    "evil.example",             # a name the attacker controls
    "evil.example:8787",
    "localhost.evil.example",   # localhost as a *prefix* proves nothing
    "cmon.internal",
    "",
])
def test_any_other_name_is_refused_because_it_could_be_rebound(host):
    # Rebinding needs a name: an IP literal cannot be re-pointed at 127.0.0.1
    # once the browser has it, but a hostname can, and then the attacker's
    # page is same-origin with a server serving your whole prompt history.
    assert host_is_trusted(host) is False


def test_a_request_with_an_untrusted_host_is_rejected(client):
    resp = client.get("/api/sessions", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    assert "untrusted Host" in resp.get_json()["error"]


def test_a_request_from_another_origin_is_rejected(client):
    resp = client.get("/api/sessions",
                      headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert "cross-origin" in resp.get_json()["error"]


def test_a_post_from_another_origin_cannot_trigger_a_reindex(client):
    assert client.post("/api/reindex",
                       headers={"Origin": "http://evil.example"}
                       ).status_code == 403


def test_our_own_origin_is_accepted(client):
    assert client.get("/api/sessions",
                      headers={"Origin": "http://localhost"}).status_code == 200


def test_a_plain_same_origin_get_with_no_origin_header_is_accepted(client):
    assert client.get("/api/sessions").status_code == 200


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


def test_the_index_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    assert resp.headers["Cache-Control"] == "no-store"


def test_the_asset_version_placeholder_is_substituted(client):
    body = client.get("/").get_data(as_text=True)
    assert "__V__" not in body
    assert "/static/style.css?v=" in body


def test_the_index_page_survives_non_ascii_content(client):
    # The template is UTF-8; reading it with the platform default encoding
    # returned a 500 on any Windows box.
    body = client.get("/").get_data(as_text=True)
    assert "═" in body or "─" in body


@pytest.mark.parametrize("path", ["/", "/api/sessions"])
def test_every_response_carries_the_hardening_headers(client, path):
    resp = client.get(path)
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_api_responses_are_never_cached(client):
    assert client.get("/api/sessions").headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_the_session_list_reports_every_session(client):
    data = client.get("/api/sessions").get_json()
    assert data["total"] == 2
    assert {s["project"] for s in data["sessions"]} == {"monitor", "other"}


def test_a_session_brief_rolls_the_subagents_into_its_totals(client):
    data = client.get("/api/sessions?sort=cost").get_json()
    top = data["sessions"][0]

    assert top["project"] == "monitor"
    assert top["title"] == "Rebuild the parse cache"
    assert top["agents"] == 2
    assert top["cost"] == pytest.approx(top["cost_main"] + top["cost_agents"])
    assert top["cost_main"] == pytest.approx(25.0)     # 1M Opus output tokens
    assert top["live"] is False
    assert top["activity"] is None


def test_the_list_can_be_sorted_and_limited(client):
    assert len(client.get("/api/sessions?limit=1").get_json()["sessions"]) == 1
    cheap_first = client.get("/api/sessions?sort=cost").get_json()["sessions"]
    assert cheap_first[0]["cost"] > cheap_first[-1]["cost"]


def test_a_session_can_be_fetched_by_its_full_id(client):
    resp = client.get("/api/sessions/aaaaaaaa-1111-2222-3333-444444444444")
    assert resp.status_code == 200
    assert resp.get_json()["project"] == "monitor"


def test_an_unambiguous_prefix_is_enough(client):
    # This is what the UI puts in URLs.
    assert client.get("/api/sessions/aaaaaaaa").get_json()["short"] == "aaaaaaaa"


def test_an_unknown_session_is_a_404(client):
    assert client.get("/api/sessions/zzzzzzzz").status_code == 404


def test_an_ambiguous_prefix_resolves_to_nothing_rather_than_guessing(
        corpus_dir):
    # Two sessions matching one prefix must 404, never silently pick one.
    for suffix in ("1111", "2222"):
        s = corpus_dir.session(session_id=f"shared00-0000-0000-0000-{suffix}")
        s.assistant(output=1)
        s.build()

    app = srv.create_app(claude_dir=corpus_dir.root, allow_network=False)
    store = app.config["STORE"]
    store._publish(store.corpus.load())
    c = app.test_client()

    assert c.get("/api/sessions/shared00").status_code == 404
    # The full id still resolves.
    assert c.get("/api/sessions/shared00-0000-0000-0000-1111"
                 ).status_code == 200


def test_session_detail_carries_the_drill_down_payload(client):
    d = client.get("/api/sessions/aaaaaaaa").get_json()

    assert [p["text"] for p in d["prompts"]] == ["make the cache incremental"]
    assert d["tools"] == {"Bash": 1}
    assert {a["id"] for a in d["agents"]} == {"agent1", "wfa1"}
    assert d["models"][0]["model"] == "claude-opus-5"
    assert d["economics"]["total_cost"] > 0
    assert len(d["timeline"]) >= 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_the_project_filter_matches_a_substring(client):
    data = client.get("/api/sessions?project=monit").get_json()
    assert [s["project"] for s in data["sessions"]] == ["monitor"]


def test_the_model_filter_matches_a_substring(client):
    data = client.get("/api/sessions?model=haiku").get_json()
    assert [s["project"] for s in data["sessions"]] == ["other"]


def test_search_looks_inside_prompt_text_not_just_titles(client):
    data = client.get("/api/sessions?q=different topic").get_json()
    assert [s["project"] for s in data["sessions"]] == ["other"]


def test_search_matches_the_title_too(client):
    data = client.get("/api/sessions?q=parse cache").get_json()
    assert [s["project"] for s in data["sessions"]] == ["monitor"]


def test_the_day_window_keeps_recent_work_and_drops_the_rest(corpus_dir):
    recent = corpus_dir.session(project="recent")
    recent.assistant(output=10)
    recent.build()

    old = corpus_dir.session(project="ancient")
    old.assistant(at=-40 * 86400, output=10)      # forty days before the rest
    old.build()

    app = srv.create_app(claude_dir=corpus_dir.root, allow_network=False)
    store = app.config["STORE"]
    store._publish(store.corpus.load())
    c = app.test_client()

    assert c.get("/api/sessions").get_json()["total"] == 2
    assert [s["project"] for s in
            c.get("/api/sessions?days=7").get_json()["sessions"]] == ["recent"]


# ---------------------------------------------------------------------------
# Agents and workflows
# ---------------------------------------------------------------------------


def test_the_agent_list_includes_every_run_with_its_type(client):
    data = client.get("/api/agents").get_json()
    assert data["total"] == 2
    by_id = {a["id"]: a for a in data["agents"]}
    assert by_id["agent1"]["type"] == "Explore"
    assert by_id["agent1"]["topic"] == "Map the parser"
    assert by_id["wfa1"]["workflow_id"] == "wf_deadbeef"
    assert {b["key"] for b in data["by_type"]} >= {"Explore"}


def test_agents_can_be_filtered_by_type(client):
    data = client.get("/api/agents?type=explore").get_json()
    assert [a["id"] for a in data["agents"]] == ["agent1"]


def test_a_workflow_run_is_reported_with_its_script_name_and_lanes(client):
    wfs = client.get("/api/workflows").get_json()["workflows"]
    assert len(wfs) == 1
    wf = wfs[0]

    assert wf["id"] == "wf_deadbeef"
    assert wf["short"] == "deadbeef"
    assert wf["name"] == "review-changes"     # from the persisted script file
    assert wf["agents"] == 1
    assert wf["completed"] == 1
    assert [lane["id"] for lane in wf["lanes"]] == ["wfa1"]


def test_workflow_detail_combines_the_transcripts_journal_and_script(client):
    d = client.get("/api/workflows/aaaaaaaa/wf_deadbeef").get_json()

    assert d["name"] == "review-changes"
    assert d["counts"] == {"total": 1, "done": 1, "running": 0, "stopped": 0}
    assert [a["id"] for a in d["agents"]] == ["wfa1"]
    assert "export const meta" in d["script"]


@pytest.mark.parametrize("wfid", ["wf_x", "not-a-workflow", "wf_../../etc"])
def test_a_malformed_workflow_id_never_reaches_the_filesystem(client, wfid):
    assert client.get(f"/api/workflows/aaaaaaaa/{wfid}").status_code == 404


def test_a_single_agent_can_be_fetched(client):
    resp = client.get("/api/agents/aaaaaaaa/agent1")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == "agent1"


def test_an_unknown_agent_is_a_404(client):
    assert client.get("/api/agents/aaaaaaaa/nope").status_code == 404


# ---------------------------------------------------------------------------
# Summary and live
# ---------------------------------------------------------------------------


def test_the_summary_totals_the_whole_corpus(client):
    d = client.get("/api/summary").get_json()
    assert d["totals"]["sessions"] == 2
    assert d["totals"]["cost"] > 0
    assert len(d["daily"]) == 30


def test_the_summary_window_sizes_the_daily_series(client):
    assert len(client.get("/api/summary?days=7").get_json()["daily"]) == 7


def test_the_live_endpoint_reports_an_idle_machine_calmly(client):
    d = client.get("/api/live").get_json()
    assert d["live"] == []
    assert d["running_agents"] == []
    assert d["burn_rate_hourly"] == 0.0


def test_reindexing_rebuilds_the_snapshot(client):
    d = client.post("/api/reindex").get_json()
    assert d == {"ok": True, "sessions": 2}


def test_git_endpoints_answer_even_with_no_repos(client):
    assert client.get("/api/git").status_code == 200
    assert client.get("/api/git/repo/unknown").status_code == 404
    assert client.get("/api/git/repo/unknown/commits/notasha").status_code == 404


# ---------------------------------------------------------------------------
# The plan / limits card
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier,org,expected", [
    ("claude_max_20x", "", "Max 20×"),
    ("claude_max_5x", "", "Max 5×"),
    ("claude_pro", "", "Pro"),
    ("enterprise_tier", "", "Enterprise"),
    ("", "claude_max", "Max"),
    ("", "claude_team", "Team"),
    ("", "", "API"),
])
def test_the_plan_label_is_derived_from_tier_then_org_type(tier, org, expected):
    assert _plan_label(tier, org) == expected


def test_a_limit_entry_is_labelled_and_its_reset_counted_down():
    now = datetime.now(timezone.utc)
    entry = _limit_entry({
        "kind": "weekly_scoped",
        "scope": {"model": {"display_name": "Opus"}},
        "percent": 42, "severity": "warn", "is_active": True,
        "resets_at": (now + timedelta(hours=2)).isoformat(),
    }, now)

    assert entry["label"] == "Week · Opus"
    assert entry["percent"] == 42
    assert entry["active"] is True
    assert entry["resets_in"] == pytest.approx(7200, abs=5)


@pytest.mark.parametrize("kind,label", [
    ("session", "Session"),
    ("weekly_all", "Week · all models"),
    ("something_new", "something new"),
])
def test_known_limit_kinds_get_readable_labels(kind, label):
    assert _limit_entry({"kind": kind}, datetime.now(timezone.utc))["label"] == label


def test_a_reset_time_in_the_past_counts_down_to_nothing():
    now = datetime.now(timezone.utc)
    entry = _limit_entry(
        {"kind": "session", "resets_at": (now - timedelta(hours=1)).isoformat()},
        now)
    assert entry["resets_in"] is None


def test_an_unparseable_reset_time_is_ignored():
    entry = _limit_entry({"kind": "session", "resets_at": "not a date"},
                         datetime.now(timezone.utc))
    assert entry["resets_in"] is None


def test_with_no_local_claude_state_the_plan_card_is_unavailable():
    assert plan_payload(allow_network=False) == {"available": False}


def test_offline_the_plan_card_falls_back_to_claude_codes_own_cache(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "claude.json"
    state.write_text(json.dumps({
        "oauthAccount": {"organizationRateLimitTier": "claude_max_20x",
                         "billingType": "subscription"},
        "cachedUsageUtilization": {
            "fetchedAtMs": (now.timestamp() - 600) * 1000,
            "utilization": {"limits": [
                {"kind": "session", "percent": 30, "is_active": True}]},
        },
    }), encoding="utf-8")
    srv.CLAUDE_STATE = state
    try:
        payload = plan_payload(allow_network=False)
    finally:
        srv.CLAUDE_STATE = tmp_path / "claude.json"

    assert payload["available"] is True
    assert payload["plan"] == "Max 20×"
    assert payload["source"] == "cache"
    assert payload["age_s"] == pytest.approx(600, abs=5)
    assert payload["limits"][0]["label"] == "Session"


def test_a_corrupt_claude_state_file_does_not_break_the_card(tmp_path,
                                                              monkeypatch):
    state = tmp_path / "claude.json"
    state.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(srv, "CLAUDE_STATE", state)
    assert plan_payload(allow_network=False) == {"available": False}


# ---------------------------------------------------------------------------
# Session-window attribution
# ---------------------------------------------------------------------------


def _cached_state(tmp_path, monkeypatch, limits):
    now = datetime.now(timezone.utc)
    state = tmp_path / "claude.json"
    state.write_text(json.dumps({
        "oauthAccount": {"organizationRateLimitTier": "claude_max_20x"},
        "cachedUsageUtilization": {
            "fetchedAtMs": now.timestamp() * 1000,
            "utilization": {"limits": limits},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(srv, "CLAUDE_STATE", state)
    return now


def test_the_session_window_is_attributed_to_projects_and_models(
        tmp_path, monkeypatch):
    now = _cached_state(tmp_path, monkeypatch, [{
        "kind": "session", "percent": 40, "is_active": True,
        "resets_at": (datetime.now(timezone.utc)
                      + timedelta(hours=2)).isoformat(),
    }])
    s = Session(session_id="s1", path="/tmp/s1.jsonl",
                project_dir="-home-dev-proj", cwd="/home/dev/proj",
                per_model={"claude-opus-5": ModelStat(calls=1, cost=2.0)})
    s.timeline = [
        ((now - timedelta(hours=1)).timestamp(), 100, 900, 2.0, 2.0),
        ((now - timedelta(hours=6)).timestamp(), 100, 900, 9.0, 9.0),
    ]

    payload = plan_payload(allow_network=False, sessions=[s])
    w = payload["session_window"]

    assert w["percent"] == 40
    assert w["cost"] == pytest.approx(2.0)
    assert w["projects"][0]["name"] == "proj"
    assert w["projects"][0]["share"] == pytest.approx(1.0)
    assert w["models"][0]["name"] == "claude-opus-5"


def test_the_session_window_needs_a_reset_and_sessions(
        tmp_path, monkeypatch):
    _cached_state(tmp_path, monkeypatch, [
        {"kind": "session", "percent": 0, "is_active": False},  # no resets_at
        {"kind": "weekly_all", "percent": 10, "is_active": True,
         "resets_at": (datetime.now(timezone.utc)
                       + timedelta(days=3)).isoformat()},
    ])
    s = Session(session_id="s1", path="/tmp/s1.jsonl",
                project_dir="-home-dev-proj", cwd="/home/dev/proj")

    assert plan_payload(allow_network=False, sessions=[s])[
        "session_window"] is None
    assert plan_payload(allow_network=False)["session_window"] is None


def test_a_reset_already_in_the_past_opens_no_window(tmp_path, monkeypatch):
    _cached_state(tmp_path, monkeypatch, [{
        "kind": "session", "percent": 100, "is_active": True,
        "resets_at": (datetime.now(timezone.utc)
                      - timedelta(hours=1)).isoformat(),
    }])
    s = Session(session_id="s1", path="/tmp/s1.jsonl",
                project_dir="-home-dev-proj", cwd="/home/dev/proj")
    assert plan_payload(allow_network=False, sessions=[s])[
        "session_window"] is None
