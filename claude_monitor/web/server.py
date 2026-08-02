"""Flask JSON API + static frontend for Claude Code Monitor."""

from __future__ import annotations

import getpass
import ipaddress
import json
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, request

from .. import analytics, pricing
from ..models import AgentRun, Session, _distill_topic
from ..parser import Corpus
from ..resources import ResourceMonitor, system_snapshot

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Plan & rate-limit utilization (read from Claude Code's own local cache)
# ---------------------------------------------------------------------------

CLAUDE_STATE = Path.home() / ".claude.json"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# Live utilization is the same request Claude Code itself makes, authorized by
# the OAuth token it already stores. Memoized so a failing network is retried
# at most every FAIL_TTL and a good answer is reused for LIVE_TTL.
LIVE_TTL = 60.0
FAIL_TTL = 120.0
_live_cache: dict = {"at": 0.0, "data": None}


def _fetch_live_usage() -> Optional[dict]:
    now = time.time()
    age = now - _live_cache["at"]
    if _live_cache["data"] is not None and age < LIVE_TTL:
        return _live_cache["data"]
    if _live_cache["data"] is None and _live_cache["at"] and age < FAIL_TTL:
        return None
    result = None
    try:
        creds = json.loads(CREDENTIALS.read_text())["claudeAiOauth"]
        if creds.get("expiresAt", 0) / 1000 > now + 30:
            req = urllib.request.Request(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {creds['accessToken']}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                result = json.loads(resp.read())
    except Exception:
        result = None
    _live_cache["at"] = now
    _live_cache["data"] = result
    return result


def _plan_label(tier: str, org_type: str) -> str:
    t = (tier or "").lower()
    if "max_20x" in t:
        return "Max 20×"
    if "max_5x" in t:
        return "Max 5×"
    if "pro" in t:
        return "Pro"
    if "team" in t:
        return "Team"
    if "enterprise" in t:
        return "Enterprise"
    return {
        "claude_max": "Max", "claude_pro": "Pro",
        "claude_team": "Team", "claude_enterprise": "Enterprise",
    }.get((org_type or "").lower(), "API")


def _limit_entry(lim: dict, now: datetime) -> dict:
    kind = lim.get("kind") or ""
    if kind == "session":
        label = "Session"
    elif kind == "weekly_all":
        label = "Week · all models"
    elif kind == "weekly_scoped":
        model = ((lim.get("scope") or {}).get("model") or {})
        label = f"Week · {model.get('display_name') or 'model'}"
    else:
        label = kind.replace("_", " ")
    resets_in = None
    raw = lim.get("resets_at")
    if raw:
        try:
            delta = (datetime.fromisoformat(raw) - now).total_seconds()
            resets_in = max(0, int(delta)) if delta > 0 else None
        except (TypeError, ValueError):
            pass
    return {
        "key": kind,
        "label": label,
        "percent": lim.get("percent") or 0,
        "severity": lim.get("severity") or "normal",
        "active": bool(lim.get("is_active")),
        "resets_at": raw,
        "resets_in": resets_in,
    }


def plan_payload(*, allow_network: bool = True) -> dict:
    """Subscription tier and limit utilization.

    Utilization comes live from Anthropic's OAuth usage endpoint (the same
    request Claude Code makes, with the token it already stores); when that
    fails — offline, token expired, ``allow_network`` off — it falls back to
    Claude Code's own cached copy in ~/.claude.json, with age_s saying how
    stale it is. Transcript data is never sent anywhere.
    """
    try:
        state = json.loads(CLAUDE_STATE.read_text())
    except (OSError, ValueError):
        state = {}

    oauth = state.get("oauthAccount") or {}
    tier = oauth.get("organizationRateLimitTier") or oauth.get("userRateLimitTier") or ""
    if not state and not CREDENTIALS.exists():
        return {"available": False}
    payload: dict = {
        "available": True,
        "plan": _plan_label(tier, oauth.get("organizationType") or ""),
        "tier": tier,
        "billing": oauth.get("billingType") or "",
        "limits": [],
        "extra": None,
        "age_s": None,
        "source": "cache",
    }

    now = datetime.now(timezone.utc)
    util = _fetch_live_usage() if allow_network else None
    if util is not None:
        payload["source"] = "live"
        payload["age_s"] = 0
    else:
        cached = state.get("cachedUsageUtilization") or {}
        util = cached.get("utilization") or {}
        if cached.get("fetchedAtMs"):
            payload["age_s"] = max(0, int(now.timestamp() - cached["fetchedAtMs"] / 1000))

    payload["limits"] = [_limit_entry(l, now) for l in (util.get("limits") or [])]

    extra = util.get("extra_usage") or {}
    if extra:
        places = extra.get("decimal_places") or 2
        payload["extra"] = {
            "enabled": bool(extra.get("is_enabled")),
            "used": (extra.get("used_credits") or 0) / 10 ** places,
            "limit": (extra.get("monthly_limit") or 0) / 10 ** places,
            "currency": extra.get("currency") or "",
            "percent": extra.get("utilization") or 0,
            "reason": extra.get("disabled_reason") or "",
        }
    return payload


# ---------------------------------------------------------------------------
# Request origin guard
# ---------------------------------------------------------------------------


def _hostname_of(host_header: str) -> str:
    """Hostname part of a ``Host`` header, port and IPv6 brackets removed."""
    host = (host_header or "").strip()
    if host.startswith("["):                      # [::1]:8787
        end = host.find("]")
        return host[1:end] if end != -1 else ""
    if host.count(":") == 1:                      # name:port / v4:port
        host = host.rsplit(":", 1)[0]
    return host


def host_is_trusted(host_header: str) -> bool:
    """True when ``Host`` names this machine by IP literal or ``localhost``.

    DNS rebinding is why this exists. Everything served here — prompts, file
    paths, working directories — is readable by any page the browser loads if
    that page can make same-origin requests to us: the attacker points their
    own hostname at 127.0.0.1, waits for the TTL to flip, and the browser
    treats http://evil.example:8787 as same-origin with us.

    Rebinding needs a *name*, because an IP literal cannot be re-pointed. So
    accept only IP literals and localhost, which closes the attack while
    leaving `--host 0.0.0.0` reachable from the LAN by address.
    """
    host = _hostname_of(host_header)
    if not host:
        return False
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def install_origin_guard(app: Flask) -> None:
    """Reject requests that a browser on another origin could have forged."""

    @app.before_request
    def _guard():
        if not host_is_trusted(request.headers.get("Host", "")):
            return (
                jsonify({"error": "untrusted Host header — "
                                  "reach this server by IP or localhost"}),
                403,
            )
        # A same-origin fetch either omits Origin (plain GET) or sends exactly
        # our own origin. Anything else is another site talking to us, which
        # for a POST would be CSRF.
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "cross-origin request refused"}), 403
        return None


# ---------------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------------


class DataStore:
    """Holds the parsed corpus and refreshes it on a background thread.

    Refreshing is not cheap while sessions are live: a session being written
    to must be re-parsed in full, along with every subagent transcript beneath
    it, which measured at 360-870ms on an active corpus. Doing that on the
    request path made every few navigations block for most of a second, so a
    daemon thread owns refreshing and requests only ever read the latest
    completed snapshot.
    """

    def __init__(self, claude_dir: Optional[Path] = None, interval: float = 2.0):
        self.corpus = Corpus(claude_dir=claude_dir)
        self.monitor = ResourceMonitor()
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot: List[Session] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.generation = 0
        self.last_refresh = 0.0

    # -- lifecycle --------------------------------------------------------

    def load_initial(self, progress=None) -> None:
        sessions = self.corpus.load(progress=progress)
        self.monitor.attach(sessions)
        self._publish(sessions)
        self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cmon-refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                # Work on a copy so readers keep a consistent list throughout.
                current = self.sessions()
                refreshed, _changed = self.corpus.refresh(current)
                self.monitor.attach(refreshed)
                self._publish(refreshed)
            except Exception:
                # A transcript can be mid-write; try again on the next tick.
                continue

    def _publish(self, sessions: List[Session]) -> None:
        with self._lock:
            self._snapshot = sessions
            self.generation += 1
            self.last_refresh = time.time()

    # -- readers ----------------------------------------------------------

    def sessions(self, *, force: bool = False) -> List[Session]:
        """Return the latest snapshot. Never blocks on parsing."""
        if force:
            refreshed, _ = self.corpus.refresh(self._snapshot)
            self.monitor.attach(refreshed)
            self._publish(refreshed)
        with self._lock:
            return self._snapshot

    def session(self, sid: str) -> Optional[Session]:
        sessions = self.sessions()
        for s in sessions:
            if s.session_id == sid:
                return s
        # Allow an unambiguous prefix, which is what the UI puts in URLs.
        matches = [s for s in sessions if s.session_id.startswith(sid)]
        return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def usage_json(u) -> dict:
    return {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_write_5m": u.cache_write_5m,
        "cache_write_1h": u.cache_write_1h,
        "cache_read": u.cache_read,
        "total_input": u.total_input,
        "total": u.total,
        "cache_hit_rate": u.cache_hit_rate,
    }


def session_brief(s: Session) -> dict:
    u = s.total_usage
    return {
        "id": s.session_id,
        "short": s.session_id[:8],
        "project": s.project,
        "cwd": s.cwd,
        "title": s.title or (s.last_prompt or "")[:90] or s.session_id[:8],
        "model": s.primary_model,
        "model_label": pricing.display_name(s.primary_model),
        "branch": s.git_branch,
        "version": s.version,
        "started": iso(s.started),
        "ended": iso(s.ended),
        "duration_s": s.duration_s,
        "active_s": s.active_seconds,
        "turns": s.user_turns,
        "api_calls": s.api_calls,
        "agents": len(s.agents),
        "tokens": u.total,
        "output_tokens": u.output_tokens,
        "peak_context": s.peak_context,
        "current_context": s.timeline[-1][2] if s.timeline else 0,
        "cost": s.total_cost,
        "cost_main": s.cost,
        "cost_agents": s.agent_cost,
        "uncached_cost": s.uncached_cost,
        "cache_hit_rate": u.cache_hit_rate,
        "output_tps": s.output_tps,
        "tool_errors": s.tool_errors,
        "live": s.is_live,
        "pid": s.pid,
        "cpu": s.cpu_percent,
        "rss": s.rss_bytes,
        "threads": s.num_threads,
        "permission_mode": s.permission_mode,
    }


def agent_json(a: AgentRun, *, parent_live: bool = False, project: str = "") -> dict:
    return {
        "id": a.agent_id,
        "session_id": a.session_id,
        "project": project,
        "topic": a.topic,
        "description": a.description,
        "type": a.subagent_type or "(unspecified)",
        "workflow_id": a.workflow_id,
        "spawn_depth": a.spawn_depth,
        "model": a.model,
        "model_label": pricing.display_name(a.model),
        "started": iso(a.started),
        "ended": iso(a.ended),
        "duration_s": a.duration_s,
        "api_calls": a.api_calls,
        "tokens": a.usage.total,
        "output_tokens": a.usage.output_tokens,
        "cost": a.cost,
        "cache_hit_rate": a.usage.cache_hit_rate,
        "output_tps": a.output_tps,
        "tools": a.tool_counts,
        "tool_errors": a.tool_errors,
        "state": a.state(parent_live=parent_live),
        "completed": a.completed,
    }


def bucket_json(b) -> dict:
    return {
        "key": b.key,
        "label": pricing.display_name(b.key) if b.key.startswith("claude") else b.key,
        "sessions": b.sessions,
        "agents": b.agents,
        "api_calls": b.api_calls,
        "tokens": b.usage.total,
        "output_tokens": b.usage.output_tokens,
        "input_tokens": b.usage.total_input,
        "cache_hit_rate": b.usage.cache_hit_rate,
        "cost": b.cost,
        "uncached_cost": b.uncached_cost,
        "savings": b.savings,
    }


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filtered(store: DataStore) -> List[Session]:
    """Apply the query-string filters shared by every list endpoint."""
    sessions = store.sessions()
    days = request.args.get("days", type=int)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        sessions = [s for s in sessions if s.ended and s.ended >= cutoff]
    project = request.args.get("project")
    if project:
        needle = project.lower()
        sessions = [s for s in sessions if needle in s.project.lower()]
    model = request.args.get("model")
    if model:
        needle = model.lower()
        sessions = [s for s in sessions if any(needle in m.lower() for m in s.models)]
    q = (request.args.get("q") or "").strip().lower()
    if q:
        def hit(s: Session) -> bool:
            hay = " ".join([
                s.title or "", s.project, s.cwd, s.session_id,
                s.last_prompt or "", s.git_branch or "",
            ]).lower()
            if q in hay:
                return True
            return any(q in (p.get("text") or "").lower() for p in s.prompts)
        sessions = [s for s in sessions if hit(s)]
    return sessions


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(claude_dir: Optional[Path] = None, *,
               allow_network: bool = True) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(HERE / "static"),
        static_url_path="/static",
    )
    # Local tool: assets must never go stale in the browser after an upgrade.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["ALLOW_NETWORK"] = allow_network
    store = DataStore(claude_dir=claude_dir)
    app.config["STORE"] = store
    install_origin_guard(app)

    # -- frontend ---------------------------------------------------------
    @app.route("/")
    def index():
        static = HERE / "static"
        try:
            version = int(max(
                (static / name).stat().st_mtime for name in ("style.css", "app.js")
            ))
        except OSError:
            version = 0
        html = (HERE / "templates" / "index.html").read_text()
        resp = app.make_response(html.replace("__V__", str(version)))
        resp.mimetype = "text/html"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # -- plan & limits ----------------------------------------------------
    @app.route("/api/plan")
    def api_plan():
        return jsonify(plan_payload(allow_network=app.config["ALLOW_NETWORK"]))

    # -- global summary ---------------------------------------------------
    @app.route("/api/summary")
    def api_summary():
        sessions = filtered(store)
        days = request.args.get("days", type=int) or 30
        tot = analytics.totals(sessions)
        econ = analytics.token_economics(sessions)
        cache = analytics.cache_efficiency(sessions)
        live = [s for s in sessions if s.is_live]

        daily = [
            {
                "date": d.isoformat(),
                "cost": b.cost,
                "tokens": b.usage.total,
                "sessions": b.sessions,
                "agents": b.agents,
            }
            for d, b in analytics.by_day(sessions, days=days)
        ]

        # Activity heatmap: spend by weekday x hour, from API-call timestamps.
        heat: Dict[tuple, float] = defaultdict(float)
        for s in sessions:
            for ts, _out, _ctx, cost in s.timeline:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
                heat[(dt.weekday(), dt.hour)] += cost
        heatmap = [
            {"day": d, "hour": h, "cost": v} for (d, h), v in sorted(heat.items())
        ]

        peak_par = max(
            (w.peak_parallelism for w in analytics.all_workflows(sessions)),
            default=0,
        )
        return jsonify({
            "user": getpass.getuser(),
            "peak_parallelism": peak_par,
            "totals": {
                "sessions": tot.sessions,
                "agents": tot.agents,
                "api_calls": tot.api_calls,
                "tokens": tot.usage.total,
                "output_tokens": tot.usage.output_tokens,
                "cost": tot.cost,
                "uncached_cost": tot.uncached_cost,
                "savings": tot.savings,
                "savings_pct": tot.savings_pct,
                "live": len(live),
                "active_s": tot.active_seconds,
            },
            "economics": econ,
            "cache": cache,
            "daily": daily,
            "heatmap": heatmap,
            "projects": [bucket_json(b) for b in analytics.by_project(sessions)],
            "models": [bucket_json(b) for b in analytics.by_model(sessions)],
            "agent_types": [bucket_json(b) for b in analytics.agent_type_summary(sessions)],
            "tools": [
                {"tool": n, "main": m, "agent": a, "total": m + a}
                for n, m, a in analytics.tool_summary(sessions)
            ],
            "rates": {
                m: {
                    "input": pricing.rate_for(m).input,
                    "output": pricing.rate_for(m).output,
                    "label": pricing.display_name(m),
                    "legacy": pricing.rate_for(m).legacy,
                }
                for m in {mm for s in sessions for mm in s.per_model}
                if m
            },
        })

    # -- live poll --------------------------------------------------------
    @app.route("/api/live")
    def api_live():
        sessions = store.sessions()
        live = [s for s in sessions if s.is_live]
        burn, _ = analytics.recent_rates(live, 900.0)
        _, tps_now = analytics.recent_rates(live, 120.0)
        running_agents = [
            agent_json(a, parent_live=True, project=s.project)
            for s in live for a in s.agents
            if a.state(parent_live=True) == "running"
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        window = [s for s in sessions if s.ended and s.ended >= cutoff]
        return jsonify({
            "now": datetime.now(timezone.utc).isoformat(),
            "live": [session_brief(s) for s in live],
            "running_agents": running_agents,
            "burn_rate_hourly": burn,
            "tps_now": tps_now,
            "spend_24h": analytics.totals(window).cost,
            "system": system_snapshot(),
            "generation": store.generation,
            "age_s": max(0.0, time.time() - store.last_refresh),
        })

    # -- sessions ---------------------------------------------------------
    @app.route("/api/sessions")
    def api_sessions():
        sessions = filtered(store)
        sort = request.args.get("sort", "recent")
        keys = {
            "recent": lambda s: (s.is_live, s.ended or datetime.min.replace(tzinfo=timezone.utc)),
            "cost": lambda s: s.total_cost,
            "tokens": lambda s: s.total_usage.total,
            "duration": lambda s: s.duration_s,
            "agents": lambda s: len(s.agents),
            "turns": lambda s: s.user_turns,
            "context": lambda s: s.peak_context,
        }
        keyfn = keys.get(sort, keys["recent"])
        rows = sorted(sessions, key=keyfn, reverse=True)
        limit = max(1, min(request.args.get("limit", type=int) or 200, 2000))
        return jsonify({
            "total": len(rows),
            "sessions": [session_brief(s) for s in rows[:limit]],
        })

    @app.route("/api/sessions/<sid>")
    def api_session(sid: str):
        s = store.session(sid)
        if s is None:
            return jsonify({"error": "not found"}), 404

        base = session_brief(s)
        # Per-token-type cost, priced with this session's own models.
        econ = analytics.token_economics([s])
        base.update({
            "usage": usage_json(s.total_usage),
            "usage_main": usage_json(s.usage),
            "economics": econ,
            "tools": s.tool_counts,
            "models": [
                {
                    "model": m,
                    "label": pricing.display_name(m),
                    "calls": st.calls,
                    "tokens": st.usage.total,
                    "output": st.usage.output_tokens,
                    "cost": st.cost,
                }
                for m, st in sorted(
                    s.per_model.items(), key=lambda kv: -kv[1].cost
                )
            ],
            "agents": [
                agent_json(a, parent_live=s.is_live, project=s.project)
                for a in sorted(s.agents, key=lambda x: -x.cost)
            ],
            "prompts": s.prompts,
            "files": [
                {"path": p, "edits": n}
                for p, n in sorted(s.files_touched.items(), key=lambda kv: -kv[1])
            ],
            # Downsampled so a 4,000-call session still renders instantly.
            "timeline": _downsample(s.timeline, 600),
            "hourly": _hourly_buckets(s),
        })
        return jsonify(base)

    @app.route("/api/sessions/<sid>/tool-calls")
    def api_tool_calls(sid: str):
        tool = (request.args.get("tool") or "").strip()
        s = store.session(sid)
        if s is None or not tool:
            return jsonify({"error": "not found"}), 404
        return jsonify(_extract_tool_calls(store.corpus.projects_dir, s, tool))

    @app.route("/api/tool-calls")
    def api_tool_calls_global():
        """Calls of one tool across the window, newest sessions first.

        Bounded by a wall-clock budget so a click never hangs on a huge
        corpus; the response says how much of the window was searched.
        """
        tool = (request.args.get("tool") or "").strip()
        if not tool:
            return jsonify({"error": "tool required"}), 404
        sessions = sorted(
            filtered(store),
            key=lambda s: s.ended or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        deadline = time.time() + 3.0
        calls: List[dict] = []
        searched = 0
        for s in sessions:
            if time.time() > deadline or len(calls) >= 400:
                break
            part = _extract_tool_calls(store.corpus.projects_dir, s, tool, limit=400)
            for c in part["calls"]:
                c["project"] = s.project
            calls.extend(part["calls"])
            searched += 1
        calls.sort(key=lambda c: c["ts"] or "", reverse=True)
        total = len(calls)
        return jsonify({
            "tool": tool, "calls": calls[:400], "total": total,
            "truncated": total > 400,
            "sessions_searched": searched, "sessions_total": len(sessions),
        })

    # -- agents -----------------------------------------------------------
    @app.route("/api/agents")
    def api_agents():
        sessions = filtered(store)
        rows: List[dict] = []
        for s in sessions:
            for a in s.agents:
                rows.append(agent_json(a, parent_live=s.is_live, project=s.project))

        atype = request.args.get("type")
        if atype:
            rows = [r for r in rows if atype.lower() in r["type"].lower()]
        q = (request.args.get("q") or "").strip().lower()
        if q:
            rows = [r for r in rows if q in r["topic"].lower() or q in r["type"].lower()]

        sort = request.args.get("sort", "recent")
        keyfns = {
            "recent": lambda r: r["started"] or "",
            "cost": lambda r: r["cost"],
            "tokens": lambda r: r["tokens"],
            "duration": lambda r: r["duration_s"],
        }
        rows.sort(key=keyfns.get(sort, keyfns["recent"]), reverse=True)
        limit = max(1, min(request.args.get("limit", type=int) or 300, 3000))

        costs = sorted(r["cost"] for r in rows)
        return jsonify({
            "total": len(rows),
            "agents": rows[:limit],
            "distribution": _histogram([r["cost"] for r in rows], 24),
            "percentiles": {
                p: (costs[min(len(costs) - 1, int(len(costs) * p / 100))] if costs else 0)
                for p in (50, 75, 90, 95, 99)
            },
            "by_type": [
                bucket_json(b) for b in analytics.agent_type_summary(sessions)
            ],
        })

    @app.route("/api/agents/<sid>/<aid>")
    def api_agent(sid: str, aid: str):
        s = store.session(sid)
        if s is None:
            return jsonify({"error": "session not found"}), 404
        for a in s.agents:
            if a.agent_id == aid:
                d = agent_json(a, parent_live=s.is_live, project=s.project)
                d["prompt"] = a.prompt
                # The meta.json label is right in the large majority of cases
                # but is occasionally stale on a resumed agent, so ship the
                # prompt-derived task line too rather than guessing which wins.
                d["prompt_topic"] = _distill_topic(a.prompt)
                d["owns"] = next(
                    (l.strip() for l in (a.prompt or "").splitlines()
                     if "YOU OWN" in l.upper()), ""
                )[:200]
                d["result"] = a.result or a.final_message
                d["session_title"] = s.title
                d["usage"] = usage_json(a.usage)
                d["models"] = [
                    {"model": m, "label": pricing.display_name(m),
                     "calls": st.calls, "cost": st.cost, "tokens": st.usage.total}
                    for m, st in a.per_model.items()
                ]
                return jsonify(d)
        return jsonify({"error": "agent not found"}), 404

    # -- workflows --------------------------------------------------------
    @app.route("/api/workflows")
    def api_workflows():
        sessions = filtered(store)
        by_sid = {s.session_id: s for s in sessions}
        out = []
        for w in analytics.all_workflows(sessions):
            parent = by_sid.get(w.session_id)
            out.append({
                "id": w.workflow_id,
                "short": w.workflow_id.replace("wf_", ""),
                "session_id": w.session_id,
                "project": w.project,
                "topic": w.topic,
                "agents": len(w.agents),
                "completed": w.completed,
                "running": w.running,
                "stopped": w.stopped,
                "peak_parallelism": w.peak_parallelism,
                "started": iso(w.started),
                "ended": iso(w.ended),
                "duration_s": w.duration_s,
                "tokens": w.usage.total,
                "cost": w.cost,
                # Gantt lanes: one bar per agent, for the parallelism view.
                "lanes": sorted(
                    [
                        {
                            "id": a.agent_id,
                            "topic": a.topic,
                            "start": a.started.timestamp() if a.started else None,
                            "end": a.ended.timestamp() if a.ended else None,
                            "cost": a.cost,
                            "tokens": a.usage.total,
                            "state": a.state(
                                parent_live=parent.is_live if parent else False
                            ),
                        }
                        for a in w.agents
                    ],
                    key=lambda l: (l["start"] or 0),
                ),
            })
        return jsonify({"workflows": out})

    # -- misc -------------------------------------------------------------
    @app.route("/api/reindex", methods=["POST"])
    def api_reindex():
        sessions = store.corpus.load(force=True)
        store.monitor.attach(sessions)
        store._publish(sessions)
        return jsonify({"ok": True, "sessions": len(sessions)})

    @app.after_request
    def response_headers(resp):
        # The UI polls; stale API responses would freeze the live view.
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        # Belt-and-braces alongside the origin guard: never sniff a response
        # into a scriptable type, never render inside another site's frame
        # (the reindex button would be a clickjacking target), and never leak
        # local URLs out through Referer.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call_text(name: str, inp) -> tuple:
    """(primary, secondary) line summarizing a tool_use input for display."""
    if not isinstance(inp, dict):
        return (str(inp)[:400], "")
    if name == "Bash":
        return ((inp.get("command") or "").strip()[:600],
                (inp.get("description") or "")[:120])
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return (inp.get("file_path") or "", "")
    if name == "Glob":
        return (inp.get("pattern") or "", inp.get("path") or "")
    if name == "Grep":
        return (inp.get("pattern") or "", inp.get("path") or inp.get("glob") or "")
    if name == "WebFetch":
        return (inp.get("url") or "", (inp.get("prompt") or "")[:120])
    if name == "WebSearch":
        return (inp.get("query") or "", "")
    if name in ("Agent", "Task"):
        return (inp.get("description") or "", (inp.get("prompt") or "")[:160])
    if name == "Skill":
        return (inp.get("skill") or "", str(inp.get("args") or "")[:120])
    if name == "TodoWrite":
        return (f"{len(inp.get('todos') or [])} todo items", "")
    try:
        return (json.dumps(dict(list(inp.items())[:4]), default=str)[:300], "")
    except Exception:
        return (str(inp)[:300], "")


def _extract_tool_calls(projects_dir: Path, s: Session, tool: str,
                        limit: int = 400) -> dict:
    """Re-read one session's transcripts and pull every call of one tool.

    Done on demand rather than at index time: keeping full tool inputs for
    every session would balloon the cache for data that is rarely viewed.
    Replayed records (session resume) are deduped on the tool_use block id.
    """
    files: List[tuple] = []
    for main in projects_dir.glob(f"*/{s.session_id}.jsonl"):
        files.append((main, ""))
        topics = {
            a.agent_id: (getattr(a, "topic", "") or a.description
                         or a.subagent_type or a.agent_id[:8])
            for a in s.agents
        }
        subdir = main.parent / s.session_id / "subagents"
        if subdir.is_dir():
            for af in sorted(subdir.rglob("agent-*.jsonl")):
                aid = af.stem.replace("agent-", "", 1)
                files.append((af, topics.get(aid, aid[:8])))
        break

    calls: List[dict] = []
    errored: set = set()
    seen: set = set()
    # Cheap substring gate before json.loads — transcripts are written compact,
    # so this skips ~98% of lines and makes whole-corpus scans feasible.
    name_probe = f'"name":"{tool}"'
    for path, agent in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if name_probe not in line and '"is_error"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    rtype = rec.get("type")
                    if rtype == "assistant":
                        for block in content:
                            if (isinstance(block, dict)
                                    and block.get("type") == "tool_use"
                                    and block.get("name") == tool):
                                bid = block.get("id") or ""
                                if bid in seen:
                                    continue
                                seen.add(bid)
                                text, sub = _tool_call_text(tool, block.get("input"))
                                calls.append({
                                    "id": bid,
                                    "ts": rec.get("timestamp"),
                                    "text": text,
                                    "sub": sub,
                                    "agent": agent,
                                })
                    elif rtype == "user":
                        for block in content:
                            if (isinstance(block, dict)
                                    and block.get("type") == "tool_result"
                                    and block.get("is_error")):
                                errored.add(block.get("tool_use_id"))
        except OSError:
            continue

    for c in calls:
        c["error"] = c.pop("id") in errored
    calls.sort(key=lambda c: c["ts"] or "")
    total = len(calls)
    if total > limit:
        calls = calls[-limit:]
    calls.reverse()  # newest first for display
    return {"tool": tool, "calls": calls, "total": total,
            "truncated": total > limit}


def _hourly_buckets(s: Session) -> List[dict]:
    """24 one-hour buckets of spend and output tokens, ending at the session's
    last activity (for a live session that is literally the last 24h).

    Main-thread calls are exact; each agent's totals are apportioned uniformly
    across its runtime, since agent transcripts aren't kept as timelines.
    """
    ends = []
    if s.timeline:
        ends.append(s.timeline[-1][0])
    for a in s.agents:
        ref = a.ended or a.started
        if ref:
            ends.append(ref.timestamp())
    if not ends:
        return []
    anchor = int(max(ends) // 3600) * 3600
    start = anchor - 23 * 3600
    buckets = [{"t": start + i * 3600, "cost": 0.0, "out": 0} for i in range(24)]

    for ts, out, _ctx, cost in s.timeline:
        i = int((ts - start) // 3600)
        if 0 <= i < 24:
            buckets[i]["cost"] += cost
            buckets[i]["out"] += out

    for a in s.agents:
        if not a.started:
            continue
        t0 = a.started.timestamp()
        t1 = a.ended.timestamp() if a.ended else t0
        span = max(1.0, t1 - t0)
        for b in buckets:
            lo, hi = b["t"], b["t"] + 3600
            if t1 == t0:
                ov = span if lo <= t0 < hi else 0.0
            else:
                ov = min(hi, t1) - max(lo, t0)
            if ov > 0:
                frac = ov / span
                b["cost"] += a.cost * frac
                b["out"] += int(a.usage.output_tokens * frac)

    for b in buckets:
        b["cost"] = round(b["cost"], 6)
    return buckets


def _downsample(timeline: List[tuple], target: int) -> List[dict]:
    """Reduce a per-call timeline to at most ``target`` points.

    Buckets are aggregated rather than sampled, so totals and peaks survive:
    cost and output are summed, context takes the bucket maximum.
    """
    if not timeline:
        return []
    if len(timeline) <= target:
        return [
            {"t": ts, "out": out, "ctx": ctx, "cost": cost}
            for ts, out, ctx, cost in timeline
        ]
    step = len(timeline) / target
    out: List[dict] = []
    for i in range(target):
        chunk = timeline[int(i * step): max(int((i + 1) * step), int(i * step) + 1)]
        if not chunk:
            continue
        out.append({
            "t": chunk[0][0],
            "out": sum(c[1] for c in chunk),
            "ctx": max(c[2] for c in chunk),
            "cost": sum(c[3] for c in chunk),
        })
    return out


def _histogram(values: List[float], bins: int) -> List[dict]:
    """Log-spaced histogram — agent costs span several orders of magnitude."""
    vals = [v for v in values if v > 0]
    if not vals:
        return []
    import math

    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [{"lo": lo, "hi": hi, "count": len(vals)}]
    llo, lhi = math.log10(lo), math.log10(hi)
    width = (lhi - llo) / bins
    counts = [0] * bins
    for v in vals:
        idx = min(bins - 1, int((math.log10(v) - llo) / width))
        counts[idx] += 1
    return [
        {
            "lo": 10 ** (llo + i * width),
            "hi": 10 ** (llo + (i + 1) * width),
            "count": c,
        }
        for i, c in enumerate(counts)
    ]


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    claude_dir: Optional[Path] = None,
    allow_network: bool = True,
) -> None:
    # No debug switch on purpose: Werkzeug's debugger is an interactive shell
    # on a server that reads your whole transcript history.
    app = create_app(claude_dir=claude_dir, allow_network=allow_network)
    app.config["STORE"].load_initial()
    app.run(host=host, port=port, threaded=True, use_reloader=False)
