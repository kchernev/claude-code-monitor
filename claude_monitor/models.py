"""Data model for parsed Claude Code transcripts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import pricing


def _utc(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from a transcript into an aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# Lines that mark the actual task in an agent prompt, in priority order.
_TASK_MARKERS = ("task:", "goal:", "objective:", "mission:", "your job:")
# Filesystem paths and long ids make topics unreadable; collapse them.
_PATH_RE = re.compile(r"(/[\w.\-]+){2,}/?")
_WS_RE = re.compile(r"\s+")


def _distill_topic(prompt: str, limit: int = 90) -> str:
    """Reduce an agent prompt to a readable one-line topic.

    Workflow subagents carry no description, so their prompt is all we have.
    Prefer an explicit ``TASK:`` line; otherwise use the first prose line with
    absolute paths collapsed so the subject stays visible.
    """
    lines = [ln.strip() for ln in (prompt or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""

    chosen = ""
    for line in lines[:40]:
        low = line.lower().lstrip("#* -")
        for marker in _TASK_MARKERS:
            if low.startswith(marker):
                chosen = line.lstrip("#* -")[len(marker):].strip()
                break
        if chosen:
            break
    if not chosen:
        # Skip pure markdown headings that carry no content of their own.
        for line in lines[:5]:
            stripped = line.lstrip("#* -").strip()
            if len(stripped) > 12:
                chosen = stripped
                break
        chosen = chosen or lines[0]

    chosen = _PATH_RE.sub("…", chosen)
    chosen = _WS_RE.sub(" ", chosen).strip(" .:-")
    # Cut at a sentence boundary when one falls in a reasonable place.
    for stop in (". ", "; ", " — ", ", "):
        idx = chosen.find(stop)
        if 25 <= idx <= limit:
            chosen = chosen[:idx]
            break
    return chosen[:limit].strip()


@dataclass
class Usage:
    """Accumulated token usage, split by cache tier so cost stays exact."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read
        self.web_search_requests += other.web_search_requests
        self.web_fetch_requests += other.web_fetch_requests

    @property
    def total_input(self) -> int:
        """Every token billed on the input side, cached or not."""
        return (
            self.input_tokens
            + self.cache_write_5m
            + self.cache_write_1h
            + self.cache_read
        )

    @property
    def total(self) -> int:
        return self.total_input + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Share of input tokens served from cache (0.0-1.0)."""
        if self.total_input == 0:
            return 0.0
        return self.cache_read / self.total_input

    @classmethod
    def from_api(cls, u: dict) -> "Usage":
        """Build from the ``message.usage`` object in a transcript record."""
        creation = u.get("cache_creation") or {}
        # Prefer the explicit 5m/1h split; fall back to the flat total, which
        # older transcript versions report as 5-minute writes.
        w5 = creation.get("ephemeral_5m_input_tokens")
        w1 = creation.get("ephemeral_1h_input_tokens")
        if w5 is None and w1 is None:
            w5 = u.get("cache_creation_input_tokens") or 0
            w1 = 0
        server = u.get("server_tool_use") or {}
        return cls(
            input_tokens=u.get("input_tokens") or 0,
            output_tokens=u.get("output_tokens") or 0,
            cache_write_5m=int(w5 or 0),
            cache_write_1h=int(w1 or 0),
            cache_read=u.get("cache_read_input_tokens") or 0,
            web_search_requests=server.get("web_search_requests") or 0,
            web_fetch_requests=server.get("web_fetch_requests") or 0,
        )


@dataclass
class ModelStat:
    """Exact per-model usage and cost, accumulated call by call.

    Attributing a multi-model session's cost by call-count share would be
    wrong — an Opus call and a Haiku call cost very different amounts — so
    every call is folded into its own model's bucket as it is parsed.
    """

    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    uncached_cost: float = 0.0

    def merge(self, other: "ModelStat") -> None:
        self.calls += other.calls
        self.usage.add(other.usage)
        self.cost += other.cost
        self.uncached_cost += other.uncached_cost


@dataclass
class ApiCall:
    """One assistant response — the unit that gets billed."""

    timestamp: Optional[datetime]
    model: str
    usage: Usage
    fast: bool = False
    stop_reason: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    # Total context size the model saw on this call.
    context_tokens: int = 0

    @property
    def cost(self) -> float:
        u = self.usage
        return pricing.cost_of(
            self.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_5m=u.cache_write_5m,
            cache_write_1h=u.cache_write_1h,
            cache_read=u.cache_read,
            fast=self.fast,
        )

    @property
    def uncached_cost(self) -> float:
        u = self.usage
        return pricing.uncached_cost_of(
            self.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_5m=u.cache_write_5m,
            cache_write_1h=u.cache_write_1h,
            cache_read=u.cache_read,
            fast=self.fast,
        )


@dataclass
class AgentRun:
    """A subagent invocation (the ``Agent`` tool / a sidechain transcript)."""

    agent_id: str
    session_id: str
    description: str = ""
    subagent_type: str = ""
    prompt: str = ""
    model: str = ""
    # Nesting level: 1 for an agent spawned by the main thread, 2+ for an
    # agent spawned by another agent.
    spawn_depth: int = 1
    # Set when the agent was spawned by the Workflow tool rather than directly.
    workflow_id: str = ""
    # Result text recorded in the workflow journal, when available.
    result: str = ""
    # True when this agent's meta.json description is shared with another
    # agent in the same session, which means the label cannot be trusted.
    label_ambiguous: bool = False
    started: Optional[datetime] = None
    ended: Optional[datetime] = None
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    uncached_cost: float = 0.0
    api_calls: int = 0
    tool_counts: Dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0
    per_model: Dict[str, "ModelStat"] = field(default_factory=dict)
    completed: bool = False
    # Final text the agent reported back, used to summarise its outcome.
    final_message: str = ""

    @property
    def duration_s(self) -> float:
        if self.started and self.ended:
            return max(0.0, (self.ended - self.started).total_seconds())
        return 0.0

    @property
    def topic(self) -> str:
        """Best available one-line description of what this agent worked on.

        The ``.meta.json`` description is authoritative when present. Workflow
        subagents have none, so fall back to distilling the prompt.
        """
        if self.description and not self.label_ambiguous:
            return self.description
        # A duplicated label means Claude Code carried a stale description
        # onto a resumed agent, so the prompt is the more reliable source.
        if self.prompt:
            return _distill_topic(self.prompt)
        return self.description or self.agent_id[:12]

    @property
    def output_tps(self) -> float:
        """Output tokens per second across the agent's lifetime."""
        d = self.duration_s
        return self.usage.output_tokens / d if d > 0 else 0.0

    def state(self, *, parent_live: bool, stale_after_s: float = 300.0) -> str:
        """Classify the run as ``done``, ``running`` or ``stopped``.

        An agent only records a final ``end_turn`` when it finishes cleanly.
        One that was interrupted, errored, or whose session was killed leaves
        no completion marker — so "not completed" alone would report ancient
        runs as still running forever. A run counts as live only while its
        parent session is alive *and* it has written something recently.
        """
        if self.completed:
            return "done"
        if not parent_live or self.ended is None:
            return "stopped"
        idle = (datetime.now(timezone.utc) - self.ended).total_seconds()
        return "running" if idle <= stale_after_s else "stopped"


@dataclass
class Session:
    """One Claude Code session — a single transcript file plus its subagents."""

    session_id: str
    path: str
    project_dir: str
    cwd: str = ""
    title: str = ""
    last_prompt: str = ""
    git_branch: str = ""
    version: str = ""
    started: Optional[datetime] = None
    ended: Optional[datetime] = None
    permission_mode: str = ""

    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    uncached_cost: float = 0.0
    api_calls: int = 0
    user_turns: int = 0

    models: Dict[str, int] = field(default_factory=dict)
    per_model: Dict[str, "ModelStat"] = field(default_factory=dict)
    tool_counts: Dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0

    agents: List[AgentRun] = field(default_factory=list)

    # Browsable history: the human prompts that drove the session, and the
    # files it edited (from Claude Code's own file-history records).
    prompts: List[dict] = field(default_factory=list)
    files_touched: Dict[str, int] = field(default_factory=dict)

    # Rolling series used for velocity, context-pressure and daily-spend
    # charts. Each entry is
    # (epoch_seconds, output_tokens, context_tokens, cost, uncached_cost).
    timeline: List[tuple] = field(default_factory=list)

    peak_context: int = 0
    # Wall-clock seconds Claude spent generating, from turn_duration records.
    active_seconds: float = 0.0

    # Populated at runtime by the resource monitor, not by the parser.
    pid: Optional[int] = None
    cpu_percent: float = 0.0
    rss_bytes: int = 0
    num_threads: int = 0
    proc_started: Optional[datetime] = None

    @property
    def is_live(self) -> bool:
        return self.pid is not None

    @property
    def duration_s(self) -> float:
        if self.started and self.ended:
            return max(0.0, (self.ended - self.started).total_seconds())
        return 0.0

    @property
    def idle_s(self) -> float:
        """Seconds since the last recorded activity."""
        if not self.ended:
            return math.inf
        return max(0.0, (datetime.now(timezone.utc) - self.ended).total_seconds())

    @property
    def project(self) -> str:
        """Short project name, preferring the real cwd over the encoded dir."""
        if self.cwd:
            return self.cwd.rstrip("/").split("/")[-1] or self.cwd
        return self.project_dir.strip("-").split("-")[-1]

    @property
    def primary_model(self) -> str:
        if not self.models:
            return ""
        return max(self.models.items(), key=lambda kv: kv[1])[0]

    @property
    def agent_cost(self) -> float:
        return sum(a.cost for a in self.agents)

    @property
    def total_cost(self) -> float:
        """Main-thread cost plus every subagent it spawned."""
        return self.cost + self.agent_cost

    @property
    def total_usage(self) -> Usage:
        u = Usage()
        u.add(self.usage)
        for a in self.agents:
            u.add(a.usage)
        return u

    @property
    def cache_savings(self) -> float:
        return max(0.0, self.uncached_cost - self.cost)

    @property
    def output_tps(self) -> float:
        """Output tokens/sec over generation time, not wall-clock."""
        if self.active_seconds > 0:
            return self.usage.output_tokens / self.active_seconds
        d = self.duration_s
        return self.usage.output_tokens / d if d > 0 else 0.0

    @property
    def burn_rate_hourly(self) -> float:
        """USD/hour across the session's wall-clock span."""
        d = self.duration_s
        return (self.total_cost / d * 3600.0) if d > 0 else 0.0

    def recent_velocity(self, window_s: float = 60.0) -> float:
        """Output tokens/sec over the trailing ``window_s`` of the timeline."""
        if not self.timeline:
            return 0.0
        cutoff = self.timeline[-1][0] - window_s
        pts = [p for p in self.timeline if p[0] >= cutoff]
        if len(pts) < 2:
            return 0.0
        span = pts[-1][0] - pts[0][0]
        if span <= 0:
            return 0.0
        return sum(p[1] for p in pts) / span
