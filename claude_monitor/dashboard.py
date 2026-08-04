"""Live terminal dashboard for Claude Code sessions."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import analytics, pricing
from .analytics import fmt_ago, fmt_duration, sparkline
from .models import Session
from .parser import Corpus
from .resources import ResourceMonitor, fmt_bytes, system_snapshot

# --- Palette ---------------------------------------------------------------
C_PRIMARY = "#7aa2f7"
C_AGENT = "#bb9af7"
C_LIVE = "#9ece6a"
C_COST = "#e0af68"
C_ERR = "#f7768e"
C_TEAL = "#2ac3de"
C_DIM = "grey42"
C_TEXT = "#c0caf5"


def _model_style(model: str) -> str:
    """Colour-code a model id by tier so the mix is readable at a glance."""
    base = pricing.normalize_model(model)
    if "fable" in base or "mythos" in base:
        return C_ERR
    if "opus" in base:
        return C_AGENT
    if "sonnet" in base:
        return C_TEAL
    if "haiku" in base:
        return C_LIVE
    return C_DIM


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class Dashboard:
    """Renders and refreshes the live view."""

    def __init__(
        self,
        corpus: Corpus,
        sessions: List[Session],
        *,
        interval: float = 2.0,
        window_hours: float = 24.0,
        live_only: bool = False,
    ):
        self.corpus = corpus
        self.sessions = sessions
        # Floor, not trust: --interval 0 would busy-loop re-parsing the corpus.
        self.interval = max(0.5, interval)
        self.window_hours = window_hours
        self.live_only = live_only
        self.monitor = ResourceMonitor()
        self.started = time.time()
        self.ticks = 0
        # session_id -> deque-ish list of (epoch, output_tokens) for live velocity
        self._pulse: Dict[str, List[tuple]] = {}
        self._prev_output: Dict[str, int] = {}

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        self.sessions, changed = self.corpus.refresh(self.sessions)
        self.monitor.attach(self.sessions)
        self.ticks += 1

        now = time.time()
        # Drop velocity state for sessions that are no longer live; without
        # this the dicts grow by one entry per session ever seen live, for the
        # whole life of the dashboard.
        live_ids = {s.session_id for s in self.sessions if s.is_live}
        for sid in list(self._pulse):
            if sid not in live_ids:
                del self._pulse[sid]
        for sid in list(self._prev_output):
            if sid not in live_ids:
                del self._prev_output[sid]

        for s in self.sessions:
            if not s.is_live:
                continue
            total_out = s.usage.output_tokens + sum(a.usage.output_tokens for a in s.agents)
            prev = self._prev_output.get(s.session_id)
            if prev is not None and total_out > prev:
                self._pulse.setdefault(s.session_id, []).append((now, total_out - prev))
            self._prev_output[s.session_id] = total_out
            # Keep a 5-minute trailing window.
            bucket = self._pulse.get(s.session_id)
            if bucket:
                self._pulse[s.session_id] = [p for p in bucket if p[0] >= now - 300]

    def live_sessions(self) -> List[Session]:
        return [s for s in self.sessions if s.is_live]

    def window_sessions(self) -> List[Session]:
        """Sessions active within the configured trailing window."""
        cutoff = datetime.now(timezone.utc).timestamp() - self.window_hours * 3600
        return [
            s for s in self.sessions if s.ended and s.ended.timestamp() >= cutoff
        ]

    def live_tps(self, sess: Session) -> float:
        """Observed output tokens/sec over the last 60s of polling.

        Falls back to the session's own recent timeline until at least two
        polls have been observed, so a freshly started dashboard doesn't
        report a busy session as 0 tok/s.
        """
        pts = self._pulse.get(sess.session_id) or []
        now = time.time()
        recent = [p for p in pts if p[0] >= now - 60]
        if recent:
            span = max(now - recent[0][0], self.interval)
            return sum(p[1] for p in recent) / span
        return sess.recent_velocity(window_s=120.0)

    # -- rendering ----------------------------------------------------------

    def header(self) -> Panel:
        live = self.live_sessions()
        sysinfo = system_snapshot()
        # Spend from call timestamps inside the window — totals() over the
        # window's *sessions* would count a straddling session's whole bill.
        window_s = self.window_hours * 3600.0
        window_cost = analytics.recent_rates(self.sessions, window_s)[0] \
            * self.window_hours

        # Trailing-window rate, not a lifetime average: what these sessions are
        # costing right now, which is the same number the web UI reports.
        burn, _ = analytics.recent_rates(live, 900.0)
        live_tps = sum(self.live_tps(s) for s in live)

        grid = Table.grid(expand=True, padding=(0, 2))
        for _ in range(6):
            grid.add_column(justify="left", ratio=1)

        def cell(label: str, value: str, style: str) -> Text:
            t = Text()
            t.append(f"{label}\n", style=C_DIM)
            t.append(value, style=f"bold {style}")
            return t

        cpu = sysinfo.get("cpu_percent", 0.0)
        mem_pct = sysinfo.get("mem_percent", 0.0)
        agent_ct = sum(len(s.agents) for s in live)

        grid.add_row(
            cell("LIVE SESSIONS", f"{len(live)}", C_LIVE if live else C_DIM),
            cell("SUBAGENTS", f"{agent_ct}", C_AGENT),
            cell(f"SPEND / {int(self.window_hours)}H",
                 pricing.fmt_usd(window_cost), C_COST),
            cell("BURN RATE", f"{pricing.fmt_usd(burn)}/hr", C_COST),
            cell("OUTPUT", f"{live_tps:,.0f} tok/s", C_TEAL),
            cell("HOST", f"{cpu:.0f}% cpu · {mem_pct:.0f}% mem", C_PRIMARY),
        )

        title = Text()
        title.append("  Claude Code", style=f"bold {C_PRIMARY}")
        title.append(" Monitor  ", style=f"bold {C_TEXT}")
        title.append(f"· {datetime.now().strftime('%H:%M:%S')} ", style=C_DIM)
        title.append(f"· refresh {self.interval:g}s", style=C_DIM)

        return Panel(grid, title=title, title_align="left",
                     border_style=C_PRIMARY, box=box.HEAVY, padding=(0, 1))

    def sessions_table(self) -> Panel:
        live = self.live_sessions()
        rows = list(live)
        if not self.live_only:
            recent = [s for s in analytics.recent_sessions(self.sessions, 30)
                      if not s.is_live]
            rows.extend(recent[: max(0, 14 - len(live))])

        # The sessions table owns the full terminal width, so TOPIC can flex
        # while every numeric column stays fixed and aligned.
        t = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False,
                  header_style=f"bold {C_DIM}", padding=(0, 1))
        t.add_column("", width=1, no_wrap=True)
        t.add_column("PROJECT", style=C_TEXT, no_wrap=True, width=15,
                     overflow="ellipsis")
        t.add_column("TOPIC", ratio=1, no_wrap=True, overflow="ellipsis",
                     min_width=18)
        t.add_column("MODEL", no_wrap=True, width=10, overflow="ellipsis")
        t.add_column("TOKENS", justify="right", no_wrap=True, width=8)
        t.add_column("CTX", justify="right", no_wrap=True, width=7)
        t.add_column("TOK/S", justify="right", no_wrap=True, width=6)
        t.add_column("COST", justify="right", no_wrap=True, width=9)
        t.add_column("AG", justify="right", no_wrap=True, width=6)
        t.add_column("CPU", justify="right", no_wrap=True, width=4)
        t.add_column("MEM", justify="right", no_wrap=True, width=7)
        t.add_column("LAST", justify="right", no_wrap=True, width=9)

        if not rows:
            t.add_row("", Text("no sessions found", style=C_DIM), *[""] * 10)

        for s in rows:
            is_live = s.is_live
            dot = Text("●", style=C_LIVE if is_live else C_DIM)
            base = "" if is_live else C_DIM

            topic = s.title or _truncate(s.last_prompt, 60) or s.session_id[:8]
            # A live session shows what it is doing right now ahead of its
            # title — that is the thing you watch this dashboard for.
            act = s.activity() if is_live else None
            if act:
                doing = act["label"] + (f" · {act['detail']}" if act["detail"] else "")
                if act.get("since_s") is not None:
                    doing += f" ({fmt_duration(act['since_s'])})"
                topic_cell = Text()
                busy = act["state"] != "waiting"
                topic_cell.append("▸ ", style=C_LIVE if busy else C_COST)
                topic_cell.append(_truncate(doing, 52),
                                  style=C_LIVE if busy else C_COST)
                topic_cell.append(f"  {_truncate(topic, 40)}", style=C_DIM)
            else:
                topic_cell = Text(_truncate(topic, 70), style=base or C_TEXT)
            model = pricing.display_name(s.primary_model)
            u = s.total_usage

            tps = self.live_tps(s) if is_live else s.output_tps
            tps_txt = f"{tps:,.0f}" if tps >= 1 else ("—" if not is_live else "0")

            ctx = s.peak_context
            ctx_style = C_ERR if ctx > 800_000 else (C_COST if ctx > 400_000 else base or C_DIM)

            # "▶2/34" while agents are working, plain total once they're not.
            running = sum(
                1 for a in s.agents
                if a.state(parent_live=is_live) == "running"
            )
            if running:
                ag_cell = Text(f"▶{running}/{len(s.agents)}", style=C_LIVE)
            else:
                ag_cell = Text(str(len(s.agents)) if s.agents else "",
                               style=base or C_AGENT)

            t.add_row(
                dot,
                Text(_truncate(s.project, 16), style=base or C_TEXT),
                topic_cell,
                Text(_truncate(model, 12), style=C_DIM if base else _model_style(s.primary_model)),
                Text(pricing.fmt_tokens(u.total), style=base or C_TEXT),
                Text(pricing.fmt_tokens(ctx), style=ctx_style),
                Text(tps_txt, style=base or (C_TEAL if tps > 0 else C_DIM)),
                Text(pricing.fmt_usd(s.total_cost), style=base or C_COST),
                ag_cell,
                Text(f"{s.cpu_percent:.0f}" if is_live else "", style=base or C_TEXT),
                Text(fmt_bytes(s.rss_bytes) if is_live else "", style=base or C_TEXT),
                Text(fmt_ago(s.ended), style=C_DIM),
            )

        n_live = len(live)
        title = Text()
        title.append(" SESSIONS ", style=f"bold {C_PRIMARY}")
        title.append(f"{n_live} live", style=C_LIVE if n_live else C_DIM)
        return Panel(t, title=title, title_align="left",
                     border_style=C_DIM, box=box.ROUNDED, padding=(0, 0))

    def agents_panel(self) -> Panel:
        live_ids = {s.session_id for s in self.live_sessions()}
        # Running agents first — pinned before the cut, or an active agent
        # older than the twelve newest would drop out of the panel entirely.
        runs = analytics.pin_running(
            analytics.all_agents(self.window_sessions()), live_ids
        )[:12]

        t = Table(box=box.SIMPLE, expand=True, show_edge=False,
                  header_style=f"bold {C_DIM}", padding=(0, 1))
        t.add_column("", width=1, no_wrap=True)
        t.add_column("TOPIC", ratio=1, no_wrap=True, overflow="ellipsis",
                     min_width=20)
        t.add_column("TYPE", no_wrap=True, width=13, overflow="ellipsis")
        t.add_column("MODEL", no_wrap=True, width=9, overflow="ellipsis")
        t.add_column("TOKENS", justify="right", no_wrap=True, width=8)
        t.add_column("TIME", justify="right", no_wrap=True, width=7)
        t.add_column("COST", justify="right", no_wrap=True, width=8)
        t.add_column("STARTED", justify="right", no_wrap=True, width=11)

        if not runs:
            t.add_row("", Text("no subagents in window", style=C_DIM),
                      "", "", "", "", "", "")

        for a in runs:
            state = a.state(parent_live=a.session_id in live_ids)
            if state == "running":
                mark, style = Text("▶", style=C_LIVE), ""
            elif state == "done":
                mark, style = Text("✓", style=C_AGENT), C_DIM
            else:
                mark, style = Text("⊘", style=C_DIM), C_DIM

            t.add_row(
                mark,
                Text(_truncate(a.topic, 46), style=style or C_TEXT),
                Text(_truncate(a.subagent_type or "—", 14), style=C_DIM),
                Text(_truncate(pricing.display_name(a.model), 10),
                     style=C_DIM if style else _model_style(a.model)),
                Text(pricing.fmt_tokens(a.usage.total), style=style or C_TEXT),
                Text(fmt_duration(a.duration_s), style=C_DIM),
                Text(pricing.fmt_usd(a.cost), style=style or C_COST),
                Text(fmt_ago(a.started), style=C_DIM),
            )

        n_running = sum(
            1 for s in self.live_sessions() for a in s.agents
            if a.state(parent_live=True) == "running"
        )
        title = Text()
        title.append(" AGENTS ", style=f"bold {C_AGENT}")
        if n_running:
            title.append(f"{n_running} running", style=C_LIVE)
        return Panel(t, title=title, title_align="left",
                     border_style=C_DIM, box=box.ROUNDED, padding=(0, 0))

    def models_panel(self) -> Panel:
        buckets = analytics.by_model(self.window_sessions())[:6]
        t = Table(box=box.SIMPLE, expand=True, show_edge=False,
                  header_style=f"bold {C_DIM}", padding=(0, 1))
        t.add_column("MODEL", no_wrap=True)
        t.add_column("CALLS", justify="right", no_wrap=True)
        t.add_column("TOKENS", justify="right", no_wrap=True)
        t.add_column("COST", justify="right", no_wrap=True)

        if not buckets:
            t.add_row(Text("—", style=C_DIM), "", "", "")
        for b in buckets:
            t.add_row(
                Text(pricing.display_name(b.key), style=_model_style(b.key)),
                Text(f"{b.api_calls:,}", style=C_TEXT),
                Text(pricing.fmt_tokens(b.usage.total), style=C_TEXT),
                Text(pricing.fmt_usd(b.cost), style=C_COST),
            )
        return Panel(t, title=Text(" MODELS ", style=f"bold {C_TEAL}"),
                     title_align="left", border_style=C_DIM,
                     box=box.ROUNDED, padding=(0, 0))

    def tools_panel(self) -> Panel:
        rows = analytics.tool_summary(self.window_sessions())[:9]
        total = sum(m + s for _, m, s in rows) or 1

        t = Table(box=box.SIMPLE, expand=True, show_edge=False,
                  header_style=f"bold {C_DIM}", padding=(0, 1))
        t.add_column("TOOL", no_wrap=True, max_width=14)
        t.add_column("", ratio=2, no_wrap=True)
        t.add_column("MAIN", justify="right", no_wrap=True)
        t.add_column("AGENT", justify="right", no_wrap=True)

        if not rows:
            t.add_row(Text("—", style=C_DIM), "", "", "")
        for name, main, sub in rows:
            frac = (main + sub) / total
            bar_w = max(1, int(frac * 18))
            bar = Text("█" * bar_w, style=C_PRIMARY)
            t.add_row(
                Text(_truncate(name, 14), style=C_TEXT),
                bar,
                Text(f"{main:,}" if main else "·", style=C_TEXT),
                Text(f"{sub:,}" if sub else "·", style=C_AGENT),
            )
        return Panel(t, title=Text(" TOOLS ", style=f"bold {C_PRIMARY}"),
                     title_align="left", border_style=C_DIM,
                     box=box.ROUNDED, padding=(0, 0))

    def footer(self) -> Panel:
        window = self.window_sessions()
        cache = analytics.cache_efficiency(window)
        days = analytics.by_day(self.sessions, days=30)
        spark = sparkline([b.cost for _d, b in days], width=30)

        hit = cache["hit_rate"] * 100.0
        hit_style = C_LIVE if hit > 70 else (C_COST if hit > 40 else C_ERR)
        bar_w = 20
        filled = int(hit / 100 * bar_w)

        grid = Table.grid(expand=True, padding=(0, 3))
        grid.add_column(ratio=2)
        grid.add_column(ratio=2)
        grid.add_column(ratio=3)

        cache_txt = Text()
        cache_txt.append("cache hit  ", style=C_DIM)
        cache_txt.append("█" * filled, style=hit_style)
        cache_txt.append("░" * (bar_w - filled), style=C_DIM)
        cache_txt.append(f" {hit:.0f}%", style=f"bold {hit_style}")

        save_txt = Text()
        save_txt.append("cache saved  ", style=C_DIM)
        save_txt.append(pricing.fmt_usd(cache["savings"]), style=f"bold {C_LIVE}")
        save_txt.append(f"  ({cache['savings_pct']:.0f}% off ", style=C_DIM)
        save_txt.append(pricing.fmt_usd(cache["uncached_cost"]), style=C_DIM)
        save_txt.append(")", style=C_DIM)

        spark_txt = Text()
        spark_txt.append("30d spend  ", style=C_DIM)
        spark_txt.append(spark, style=C_COST)
        spark_txt.append(
            f"  {pricing.fmt_usd(sum(b.cost for _d, b in days))}", style=f"bold {C_COST}"
        )

        grid.add_row(cache_txt, save_txt, spark_txt)
        return Panel(grid, border_style=C_DIM, box=box.ROUNDED, padding=(0, 1))

    def render(self) -> Layout:
        root = Layout()
        root.split_column(
            Layout(self.header(), name="header", size=5),
            Layout(name="body"),
            Layout(self.footer(), name="footer", size=3),
        )
        # Sessions span the full width so the topic stays readable; the
        # supporting panels share the row beneath.
        root["body"].split_column(
            Layout(self.sessions_table(), name="sessions", ratio=3),
            Layout(name="lower", ratio=2),
        )
        root["body"]["lower"].split_row(
            Layout(self.agents_panel(), name="agents", ratio=3),
            Layout(name="side", ratio=2),
        )
        root["body"]["lower"]["side"].split_column(
            Layout(self.models_panel(), name="models"),
            Layout(self.tools_panel(), name="tools"),
        )
        return root


def run(
    corpus: Corpus,
    sessions: List[Session],
    *,
    interval: float = 2.0,
    window_hours: float = 24.0,
    live_only: bool = False,
    console: Optional[Console] = None,
) -> None:
    """Run the dashboard until interrupted."""
    console = console or Console()
    dash = Dashboard(
        corpus, sessions,
        interval=interval, window_hours=window_hours, live_only=live_only,
    )
    dash.refresh()

    with Live(
        dash.render(),
        console=console,
        refresh_per_second=4,
        screen=True,
        transient=False,
    ) as live:
        try:
            while True:
                time.sleep(interval)
                dash.refresh()
                live.update(dash.render())
        except KeyboardInterrupt:
            pass
    # Refreshes only save the index periodically; flush on the way out so the
    # parsing done during this run isn't thrown away.
    corpus.save_cache()
    console.print(f"[{C_DIM}]Claude Code Monitor stopped.[/]")
