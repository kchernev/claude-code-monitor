"""Aggregations over parsed sessions: cost, velocity, agents, tools, cache."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import pricing
from .models import AgentRun, Session, Usage


@dataclass
class Bucket:
    """A named group of sessions with rolled-up totals."""

    key: str
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    uncached_cost: float = 0.0
    sessions: int = 0
    api_calls: int = 0
    agents: int = 0
    active_seconds: float = 0.0

    @property
    def savings(self) -> float:
        return max(0.0, self.uncached_cost - self.cost)

    @property
    def savings_pct(self) -> float:
        return (self.savings / self.uncached_cost * 100.0) if self.uncached_cost else 0.0

    @property
    def output_tps(self) -> float:
        return self.usage.output_tokens / self.active_seconds if self.active_seconds else 0.0


def _fold(bucket: Bucket, sess: Session, *, include_agents: bool = True) -> None:
    bucket.sessions += 1
    bucket.usage.add(sess.usage)
    bucket.cost += sess.cost
    bucket.uncached_cost += sess.uncached_cost
    bucket.api_calls += sess.api_calls
    bucket.active_seconds += sess.active_seconds
    if include_agents:
        for a in sess.agents:
            bucket.usage.add(a.usage)
            bucket.cost += a.cost
            bucket.uncached_cost += a.uncached_cost
            bucket.api_calls += a.api_calls
            bucket.agents += 1


def totals(sessions: Iterable[Session]) -> Bucket:
    """Grand total across every session, including subagents."""
    b = Bucket(key="all")
    for s in sessions:
        _fold(b, s)
    return b


def by_project(sessions: Iterable[Session]) -> List[Bucket]:
    out: Dict[str, Bucket] = {}
    for s in sessions:
        b = out.setdefault(s.project, Bucket(key=s.project))
        _fold(b, s)
    return sorted(out.values(), key=lambda b: b.cost, reverse=True)


def by_model(sessions: Iterable[Session]) -> List[Bucket]:
    """Group by model using exact per-call attribution.

    A session can switch models mid-run, and tiers differ by up to 10x in
    price, so cost comes from the per-model stats recorded at parse time
    rather than from apportioning the session total by call count.
    """
    out: Dict[str, Bucket] = {}

    def fold(per_model: Dict[str, "object"], is_agent: bool) -> None:
        for model, stat in per_model.items():
            b = out.setdefault(model, Bucket(key=model))
            b.usage.add(stat.usage)
            b.cost += stat.cost
            b.uncached_cost += stat.uncached_cost
            b.api_calls += stat.calls
            if is_agent:
                b.agents += 1
            else:
                b.sessions += 1

    for s in sessions:
        fold(s.per_model, False)
        for a in s.agents:
            fold(a.per_model, True)

    return sorted(out.values(), key=lambda b: b.cost, reverse=True)


def by_day(sessions: Iterable[Session], days: int = 30) -> List[Tuple[date, Bucket]]:
    """Daily totals for the trailing ``days``, oldest first, gaps filled.

    Cost is attributed to the day each API call actually happened, from the
    per-call timeline — never lumped onto the session's start date. Lumping
    put a week-long session's whole bill on day one and dropped sessions that
    started before the window entirely, which is why the daily chart used to
    disagree with the window total. Agents keep no timeline, so their totals
    are apportioned across their runtime by day overlap; an agent's calls and
    run-count land on its start day.
    """
    out: Dict[date, Bucket] = {}
    active: Dict[date, Set[str]] = defaultdict(set)

    def bucket(d: date) -> Bucket:
        b = out.get(d)
        if b is None:
            b = out[d] = Bucket(key=d.isoformat())
        return b

    for s in sessions:
        for ts, out_tok, ctx, cost, uncached in s.timeline:
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            b = bucket(d)
            b.cost += cost
            b.uncached_cost += uncached
            b.api_calls += 1
            b.usage.input_tokens += ctx
            b.usage.output_tokens += out_tok
            active[d].add(s.session_id)

        for a in s.agents:
            if not a.started:
                continue
            t0 = a.started.astimezone(timezone.utc)
            t1 = (a.ended or a.started).astimezone(timezone.utc)
            b0 = bucket(t0.date())
            b0.agents += 1
            b0.api_calls += a.api_calls
            span = (t1 - t0).total_seconds()
            if span <= 0:
                b0.cost += a.cost
                b0.uncached_cost += a.uncached_cost
                b0.usage.input_tokens += a.usage.total_input
                b0.usage.output_tokens += a.usage.output_tokens
                active[t0.date()].add(s.session_id)
                continue
            d = t0.date()
            while d <= t1.date():
                day_lo = datetime.combine(d, dt_time.min, tzinfo=timezone.utc)
                day_hi = day_lo + timedelta(days=1)
                ov = (min(t1, day_hi) - max(t0, day_lo)).total_seconds()
                if ov > 0:
                    frac = ov / span
                    b = bucket(d)
                    b.cost += a.cost * frac
                    b.uncached_cost += a.uncached_cost * frac
                    b.usage.input_tokens += int(a.usage.total_input * frac)
                    b.usage.output_tokens += int(a.usage.output_tokens * frac)
                    active[d].add(s.session_id)
                d += timedelta(days=1)

    for d, ids in active.items():
        out[d].sessions = len(ids)

    if not out:
        return []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    series: List[Tuple[date, Bucket]] = []
    for i in range(days):
        d = start + timedelta(days=i)
        series.append((d, out.get(d, Bucket(key=d.isoformat()))))
    return series


def all_agents(sessions: Iterable[Session]) -> List[AgentRun]:
    """Every subagent run across every session, newest first."""
    runs: List[AgentRun] = []
    for s in sessions:
        runs.extend(s.agents)
    runs.sort(
        key=lambda a: a.started or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return runs


def pin_running(
    runs: List[AgentRun], live_session_ids: Set[str]
) -> List[AgentRun]:
    """Stable-partition agent runs so the ones running right now come first.

    Whatever a list is sorted by — cost, recency — an agent that is actively
    working belongs at the top, not wherever its sort key happens to land.
    The sort is stable, so each partition keeps the caller's ordering.
    """
    return sorted(
        runs,
        key=lambda a: a.state(parent_live=a.session_id in live_session_ids)
        != "running",
    )


@dataclass
class WorkflowRun:
    """A ``Workflow`` tool invocation and every agent it fanned out."""

    workflow_id: str
    session_id: str
    project: str = ""
    agents: List[AgentRun] = field(default_factory=list)

    @property
    def started(self) -> Optional[datetime]:
        stamps = [a.started for a in self.agents if a.started]
        return min(stamps) if stamps else None

    @property
    def ended(self) -> Optional[datetime]:
        stamps = [a.ended for a in self.agents if a.ended]
        return max(stamps) if stamps else None

    @property
    def duration_s(self) -> float:
        s, e = self.started, self.ended
        return max(0.0, (e - s).total_seconds()) if s and e else 0.0

    @property
    def cost(self) -> float:
        return sum(a.cost for a in self.agents)

    @property
    def usage(self) -> Usage:
        u = Usage()
        for a in self.agents:
            u.add(a.usage)
        return u

    # Set by all_workflows() so agent state can account for session liveness.
    parent_live: bool = False

    @property
    def completed(self) -> int:
        return sum(1 for a in self.agents if a.completed)

    @property
    def running(self) -> int:
        return sum(
            1 for a in self.agents
            if a.state(parent_live=self.parent_live) == "running"
        )

    @property
    def stopped(self) -> int:
        return sum(
            1 for a in self.agents
            if a.state(parent_live=self.parent_live) == "stopped"
        )

    @property
    def peak_parallelism(self) -> int:
        """Maximum number of agents alive at the same moment.

        Computed with a sweep over start/end events, which is what makes the
        fan-out width visible rather than just the total agent count.
        """
        events: List[Tuple[float, int]] = []
        for a in self.agents:
            if a.started and a.ended:
                events.append((a.started.timestamp(), 1))
                events.append((a.ended.timestamp(), -1))
        if not events:
            return 0
        events.sort()
        cur = peak = 0
        for _ts, delta in events:
            cur += delta
            peak = max(peak, cur)
        return peak

    @property
    def topic(self) -> str:
        """Shared subject of the fan-out, from the most common agent topic."""
        topics = [a.topic for a in self.agents if a.topic]
        if not topics:
            return self.workflow_id
        # Most common topic names the fan-out. Sorted first so a tie always
        # breaks the same way — iterating a set would let the title change
        # between runs, since string hashing is salted per process.
        return max(sorted(set(topics)), key=topics.count)


def all_workflows(sessions: Iterable[Session]) -> List[WorkflowRun]:
    """Group workflow-spawned agents into their parent workflow runs."""
    out: Dict[Tuple[str, str], WorkflowRun] = {}
    for s in sessions:
        for a in s.agents:
            if not a.workflow_id:
                continue
            key = (s.session_id, a.workflow_id)
            wf = out.get(key)
            if wf is None:
                wf = WorkflowRun(
                    workflow_id=a.workflow_id,
                    session_id=s.session_id,
                    project=s.project,
                    parent_live=s.is_live,
                )
                out[key] = wf
            wf.agents.append(a)
    runs = list(out.values())
    runs.sort(
        key=lambda w: w.started or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return runs


def agent_type_summary(sessions: Iterable[Session]) -> List[Bucket]:
    """Roll up subagents by ``subagent_type``."""
    out: Dict[str, Bucket] = {}
    for s in sessions:
        for a in s.agents:
            key = a.subagent_type or "(unspecified)"
            b = out.setdefault(key, Bucket(key=key))
            b.agents += 1
            b.usage.add(a.usage)
            b.cost += a.cost
            b.uncached_cost += a.uncached_cost
            b.api_calls += a.api_calls
            b.active_seconds += a.duration_s
    return sorted(out.values(), key=lambda b: b.cost, reverse=True)


def tool_summary(sessions: Iterable[Session]) -> List[Tuple[str, int, int]]:
    """``(tool_name, main_thread_calls, subagent_calls)`` sorted by total."""
    main: Dict[str, int] = defaultdict(int)
    sub: Dict[str, int] = defaultdict(int)
    for s in sessions:
        for name, n in s.tool_counts.items():
            main[name] += n
        for a in s.agents:
            for name, n in a.tool_counts.items():
                sub[name] += n
    names = set(main) | set(sub)
    rows = [(n, main.get(n, 0), sub.get(n, 0)) for n in names]
    rows.sort(key=lambda r: r[1] + r[2], reverse=True)
    return rows


def cache_efficiency(sessions: Iterable[Session]) -> dict:
    """Corpus-wide prompt-cache performance and realised dollar savings."""
    u = Usage()
    cost = 0.0
    uncached = 0.0
    for s in sessions:
        u.add(s.usage)
        cost += s.cost
        uncached += s.uncached_cost
        for a in s.agents:
            u.add(a.usage)
            cost += a.cost
            uncached += a.uncached_cost
    return {
        "hit_rate": u.cache_hit_rate,
        "cache_read": u.cache_read,
        "cache_write_5m": u.cache_write_5m,
        "cache_write_1h": u.cache_write_1h,
        "fresh_input": u.input_tokens,
        "total_input": u.total_input,
        "cost": cost,
        "uncached_cost": uncached,
        "savings": max(0.0, uncached - cost),
        "savings_pct": ((uncached - cost) / uncached * 100.0) if uncached else 0.0,
    }


def token_economics(sessions: Iterable[Session]) -> dict:
    """Exact cost split by token type, plus the effective rate for real work.

    Every bucket is priced with its own model's rate and cache multiplier, so
    the parts sum to the true total rather than an apportioned estimate. The
    headline number is ``effective_output_rate``: total spend divided by output
    tokens, which is what a token of genuinely new work actually costs once the
    context re-reads that produced it are paid for.
    """
    cost = {"input": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0,
            "cache_read": 0.0, "output": 0.0}
    tokens = dict.fromkeys(cost, 0)

    def fold(per_model: Dict[str, object]) -> None:
        for model, stat in per_model.items():
            r = pricing.rate_for(model)
            u = stat.usage
            tokens["input"] += u.input_tokens
            tokens["cache_write_5m"] += u.cache_write_5m
            tokens["cache_write_1h"] += u.cache_write_1h
            tokens["cache_read"] += u.cache_read
            tokens["output"] += u.output_tokens
            cost["input"] += u.input_tokens * r.input / 1e6
            cost["cache_write_5m"] += (
                u.cache_write_5m * r.input * pricing.CACHE_WRITE_5M_MULT / 1e6
            )
            cost["cache_write_1h"] += (
                u.cache_write_1h * r.input * pricing.CACHE_WRITE_1H_MULT / 1e6
            )
            cost["cache_read"] += (
                u.cache_read * r.input * pricing.CACHE_READ_MULT / 1e6
            )
            cost["output"] += u.output_tokens * r.output / 1e6

    for s in sessions:
        fold(s.per_model)
        for a in s.agents:
            fold(a.per_model)

    total_cost = sum(cost.values())
    total_tokens = sum(tokens.values())
    out_tokens = tokens["output"]

    # Blended list rate for output across the models actually used, so the
    # multiple below compares like with like.
    list_rate = 0.0
    if out_tokens:
        weighted = 0.0
        for s in sessions:
            for pm in (s.per_model, *(a.per_model for a in s.agents)):
                for model, stat in pm.items():
                    weighted += stat.usage.output_tokens * pricing.rate_for(model).output
        list_rate = weighted / out_tokens

    effective = (total_cost / out_tokens * 1e6) if out_tokens else 0.0
    return {
        "cost": cost,
        "tokens": tokens,
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "output_share": (out_tokens / total_tokens) if total_tokens else 0.0,
        "effective_output_rate": effective,
        "list_output_rate": list_rate,
        "multiple_of_list": (effective / list_rate) if list_rate else 0.0,
    }


def recent_rates(
    sessions: Iterable[Session], window_s: float = 900.0
) -> Tuple[float, float]:
    """``(USD/hour, output tokens/sec)`` over the trailing window ending *now*.

    Anchoring at wall-clock now is the whole point: a lifetime average — or a
    window anchored at a session's own last write — keeps reporting an hours-old
    burst as if it were happening this minute. Main-thread calls are exact,
    since the timeline is per call; agent totals are apportioned by how much of
    their runtime falls inside the window.
    """
    now = datetime.now(timezone.utc).timestamp()
    lo = now - window_s
    cost = out = 0.0
    for s in sessions:
        for ts, o, _ctx, c, _unc in reversed(s.timeline):
            if ts < lo:
                break
            cost += c
            out += o
        for a in s.agents:
            if not a.started:
                continue
            t0 = a.started.timestamp()
            t1 = a.ended.timestamp() if a.ended else now
            overlap = min(now, t1) - max(lo, t0)
            if overlap <= 0:
                continue
            frac = overlap / max(1.0, t1 - t0)
            cost += a.cost * frac
            out += a.usage.output_tokens * frac
    return cost / window_s * 3600.0, out / window_s


def velocity_series(sess: Session, bucket_s: float = 30.0) -> List[Tuple[float, float]]:
    """Bucketed output tokens/sec for one session: ``[(epoch, tps), ...]``."""
    if len(sess.timeline) < 2:
        return []
    buckets: Dict[int, float] = defaultdict(float)
    for ts, out_tokens, _ctx, _cost, _unc in sess.timeline:
        buckets[int(ts // bucket_s)] += out_tokens
    return [(k * bucket_s, v / bucket_s) for k, v in sorted(buckets.items())]


def context_series(sess: Session) -> List[Tuple[float, int]]:
    """Context size per API call: ``[(epoch, context_tokens), ...]``."""
    return [(ts, ctx) for ts, _out, ctx, _cost, _unc in sess.timeline]


def cost_series(sess: Session) -> List[Tuple[float, float]]:
    """Cumulative spend over the session: ``[(epoch, usd_so_far), ...]``."""
    running = 0.0
    out = []
    for ts, _o, _c, cost, _unc in sess.timeline:
        running += cost
        out.append((ts, running))
    return out


def top_sessions(sessions: Iterable[Session], n: int = 10, key: str = "cost") -> List[Session]:
    """Highest sessions by ``cost``, ``tokens``, ``duration`` or ``agents``."""
    keyfn = {
        "cost": lambda s: s.total_cost,
        "tokens": lambda s: s.total_usage.total,
        "duration": lambda s: s.duration_s,
        "agents": lambda s: len(s.agents),
    }.get(key, lambda s: s.total_cost)
    return sorted(sessions, key=keyfn, reverse=True)[:n]


def recent_sessions(sessions: Iterable[Session], n: int = 20) -> List[Session]:
    """Most recently active sessions, live ones pinned to the top."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        sessions,
        key=lambda s: (s.is_live, s.ended or epoch),
        reverse=True,
    )[:n]


def sparkline(values: List[float], width: int = 24) -> str:
    """Render a numeric series as a unicode block sparkline."""
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return " " * width
    # Resample to the target width.
    if len(values) > width:
        step = len(values) / width
        resampled = [
            max(values[int(i * step) : max(int((i + 1) * step), int(i * step) + 1)] or [0])
            for i in range(width)
        ]
    else:
        resampled = list(values) + [0.0] * (width - len(values))
    lo, hi = min(resampled), max(resampled)
    if hi <= lo:
        return blocks[0] * width
    return "".join(
        blocks[min(len(blocks) - 1, int((v - lo) / (hi - lo) * (len(blocks) - 1)))]
        for v in resampled
    )


def fmt_duration(seconds: float) -> str:
    """Compact duration: ``1h 04m``, ``3m 12s``, ``45s``."""
    if seconds is None or seconds != seconds:  # NaN guard
        return "—"
    if seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    hours, rem = divmod(seconds, 3600)
    if hours < 48:
        return f"{hours}h {rem // 60:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def fmt_ago(dt: Optional[datetime]) -> str:
    """``2m ago``-style relative timestamp."""
    if dt is None:
        return "—"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 0:
        return "now"
    return fmt_duration(delta) + " ago"
