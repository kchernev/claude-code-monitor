"""Command-line interface for Claude Code Monitor."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from . import analytics, dashboard, pricing
from .analytics import fmt_ago, fmt_duration, sparkline
from .models import Session
from .parser import Corpus
from .resources import ResourceMonitor, fmt_bytes

C_PRIMARY = dashboard.C_PRIMARY
C_AGENT = dashboard.C_AGENT
C_LIVE = dashboard.C_LIVE
C_COST = dashboard.C_COST
C_ERR = dashboard.C_ERR
C_TEAL = dashboard.C_TEAL
C_DIM = dashboard.C_DIM
C_TEXT = dashboard.C_TEXT


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_sessions(
    console: Console, args, *, quiet: bool = False
) -> tuple:
    """Load the corpus, showing a progress bar when a full parse is needed."""
    corpus = Corpus(claude_dir=args.claude_dir)
    if not corpus.projects_dir.is_dir():
        console.print(
            f"[{C_ERR}]No transcripts found at {corpus.projects_dir}[/]\n"
            f"[{C_DIM}]Set --claude-dir or CLAUDE_CONFIG_DIR if Claude Code "
            f"stores its data elsewhere.[/]"
        )
        raise SystemExit(1)

    paths = corpus.session_paths()
    if not paths:
        console.print(f"[{C_ERR}]No session transcripts under {corpus.projects_dir}[/]")
        raise SystemExit(1)

    sessions: List[Session] = []
    if quiet:
        sessions = corpus.load(force=args.reindex)
    else:
        with Progress(
            SpinnerColumn(style=C_PRIMARY),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(complete_style=C_PRIMARY, finished_style=C_LIVE),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
            transient=True,
        ) as prog:
            task = prog.add_task("indexing transcripts", total=len(paths))

            def cb(done: int, total: int, name: str) -> None:
                prog.update(task, completed=done, description=f"indexing {name[:8]}")

            sessions = corpus.load(progress=cb, force=args.reindex)

    sessions = apply_filters(sessions, args)
    return corpus, sessions


def apply_filters(sessions: List[Session], args) -> List[Session]:
    """Apply --days / --project / --model filters."""
    out = sessions
    days = getattr(args, "days", None)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out = [s for s in out if s.ended and s.ended >= cutoff]
    project = getattr(args, "project", None)
    if project:
        needle = project.lower()
        out = [s for s in out if needle in s.project.lower() or needle in s.cwd.lower()]
    model = getattr(args, "model", None)
    if model:
        needle = model.lower()
        out = [s for s in out if any(needle in m.lower() for m in s.models)]
    return out


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def print_totals_banner(console: Console, sessions: List[Session], label: str) -> None:
    t = analytics.totals(sessions)
    live = sum(1 for s in sessions if s.is_live)

    grid = Table.grid(expand=True, padding=(0, 3))
    for _ in range(6):
        grid.add_column(justify="left")

    def cell(k: str, v: str, style: str) -> Text:
        txt = Text()
        txt.append(f"{k}\n", style=C_DIM)
        txt.append(v, style=f"bold {style}")
        return txt

    grid.add_row(
        cell("SESSIONS", f"{t.sessions:,}" + (f"  ({live} live)" if live else ""), C_TEXT),
        cell("SUBAGENTS", f"{t.agents:,}", C_AGENT),
        cell("API CALLS", f"{t.api_calls:,}", C_TEXT),
        cell("TOKENS", pricing.fmt_tokens(t.usage.total), C_TEAL),
        cell("COST", pricing.fmt_usd(t.cost), C_COST),
        cell("CACHE SAVED", f"{pricing.fmt_usd(t.savings)} ({t.savings_pct:.0f}%)", C_LIVE),
    )
    console.print(
        Panel(grid, title=Text(f" {label} ", style=f"bold {C_PRIMARY}"),
              title_align="left", border_style=C_PRIMARY, box=box.HEAVY,
              padding=(0, 1))
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_live(console: Console, args) -> int:
    corpus, sessions = load_sessions(console, args)
    dashboard.run(
        corpus, sessions,
        interval=args.interval,
        window_hours=args.window,
        live_only=args.live_only,
        console=console,
    )
    return 0


def cmd_sessions(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    ResourceMonitor().attach(sessions)

    if args.sort == "recent":
        rows = analytics.recent_sessions(sessions, args.limit)
    else:
        rows = analytics.top_sessions(sessions, args.limit, key=args.sort)

    if args.json:
        print(json.dumps([_session_json(s) for s in rows], indent=2))
        return 0

    print_totals_banner(console, sessions, "SESSIONS")

    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C_DIM}")
    t.add_column("", width=2)
    t.add_column("PROJECT", no_wrap=True, max_width=18)
    t.add_column("TOPIC", ratio=3, no_wrap=True)
    t.add_column("MODEL", no_wrap=True, max_width=12)
    t.add_column("TURNS", justify="right")
    t.add_column("TOKENS", justify="right")
    t.add_column("PEAK CTX", justify="right")
    t.add_column("TOK/S", justify="right")
    t.add_column("AG", justify="right", width=6, no_wrap=True)
    t.add_column("COST", justify="right")
    t.add_column("WHEN", justify="right", no_wrap=True)

    for s in rows:
        u = s.total_usage
        # "▶2/34" while agents are working, plain total once they're not.
        running = sum(
            1 for a in s.agents
            if a.state(parent_live=s.is_live) == "running"
        )
        if running:
            ag_cell = Text(f"▶{running}/{len(s.agents)}", style=C_LIVE)
        else:
            ag_cell = Text(str(len(s.agents)) if s.agents else "", style=C_AGENT)
        t.add_row(
            Text("●", style=C_LIVE if s.is_live else C_DIM),
            Text(_truncate(s.project, 18), style=C_TEXT),
            Text(_truncate(s.title or s.last_prompt or s.session_id[:8], 70)),
            Text(pricing.display_name(s.primary_model),
                 style=dashboard._model_style(s.primary_model)),
            f"{s.user_turns}",
            pricing.fmt_tokens(u.total),
            pricing.fmt_tokens(s.peak_context),
            f"{s.output_tps:,.0f}" if s.output_tps else "—",
            ag_cell,
            Text(pricing.fmt_usd(s.total_cost), style=C_COST),
            Text(fmt_ago(s.ended), style=C_DIM),
        )
    console.print(t)
    console.print(
        f"[{C_DIM}]  Showing {len(rows)} of {len(sessions)} sessions · "
        f"sort={args.sort} · use `cmon show <session-id>` for detail[/]"
    )
    return 0


def cmd_agents(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    ResourceMonitor().attach(sessions)
    runs = analytics.all_agents(sessions)

    if args.type:
        runs = [a for a in runs if args.type.lower() in (a.subagent_type or "").lower()]
    # Running agents first, before the cut, so active work is always visible.
    live_ids = {s.session_id for s in sessions if s.is_live}
    runs = analytics.pin_running(runs, live_ids)
    runs_shown = runs[: args.limit]

    if args.json:
        print(json.dumps([_agent_json(a) for a in runs_shown], indent=2))
        return 0

    total_cost = sum(a.cost for a in runs)
    total_tokens = sum(a.usage.total for a in runs)
    done = sum(1 for a in runs if a.completed)

    grid = Table.grid(expand=True, padding=(0, 3))
    for _ in range(5):
        grid.add_column()

    def cell(k: str, v: str, style: str) -> Text:
        txt = Text()
        txt.append(f"{k}\n", style=C_DIM)
        txt.append(v, style=f"bold {style}")
        return txt

    grid.add_row(
        cell("AGENT RUNS", f"{len(runs):,}", C_AGENT),
        cell("COMPLETED", f"{done:,}", C_LIVE),
        cell("TOKENS", pricing.fmt_tokens(total_tokens), C_TEAL),
        cell("COST", pricing.fmt_usd(total_cost), C_COST),
        cell("AVG / AGENT", pricing.fmt_usd(total_cost / len(runs)) if runs else "—", C_COST),
    )
    console.print(Panel(grid, title=Text(" SUBAGENTS ", style=f"bold {C_AGENT}"),
                        title_align="left", border_style=C_AGENT,
                        box=box.HEAVY, padding=(0, 1)))

    # Breakdown by agent type.
    types = analytics.agent_type_summary(sessions)
    if types:
        tt = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
                   title=Text("by type", style=C_DIM), title_justify="left")
        tt.add_column("TYPE", no_wrap=True)
        tt.add_column("RUNS", justify="right")
        tt.add_column("TOKENS", justify="right")
        tt.add_column("AVG TIME", justify="right")
        tt.add_column("COST", justify="right")
        tt.add_column("SHARE", justify="right")
        for b in types:
            share = (b.cost / total_cost * 100.0) if total_cost else 0.0
            tt.add_row(
                Text(b.key, style=C_AGENT),
                f"{b.agents:,}",
                pricing.fmt_tokens(b.usage.total),
                fmt_duration(b.active_seconds / b.agents if b.agents else 0),
                Text(pricing.fmt_usd(b.cost), style=C_COST),
                f"{share:.0f}%",
            )
        console.print(tt)

    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C_DIM}")
    t.add_column("", width=2)
    t.add_column("TOPIC", ratio=3, no_wrap=True)
    t.add_column("TYPE", no_wrap=True, max_width=16)
    t.add_column("PROJECT", no_wrap=True, max_width=14)
    t.add_column("MODEL", no_wrap=True, max_width=11)
    t.add_column("CALLS", justify="right")
    t.add_column("TOKENS", justify="right")
    t.add_column("TIME", justify="right")
    t.add_column("TOK/S", justify="right")
    t.add_column("COST", justify="right")
    t.add_column("WHEN", justify="right", no_wrap=True)

    by_sid = {s.session_id: s for s in sessions}
    glyphs = {"done": ("✓", C_LIVE), "running": ("▶", C_COST), "stopped": ("⊘", C_DIM)}
    for a in runs_shown:
        parent = by_sid.get(a.session_id)
        state = a.state(parent_live=parent.is_live if parent else False)
        ch, st = glyphs[state]
        mark = Text(ch, style=st)
        t.add_row(
            mark,
            Text(_truncate(a.topic, 60)),
            Text(_truncate(a.subagent_type or "—", 16), style=C_AGENT),
            Text(_truncate(parent.project if parent else "—", 14), style=C_DIM),
            Text(pricing.display_name(a.model), style=dashboard._model_style(a.model)),
            f"{a.api_calls}",
            pricing.fmt_tokens(a.usage.total),
            fmt_duration(a.duration_s),
            f"{a.output_tps:,.0f}" if a.output_tps else "—",
            Text(pricing.fmt_usd(a.cost), style=C_COST),
            Text(fmt_ago(a.started), style=C_DIM),
        )
    console.print(t)
    console.print(f"[{C_DIM}]  Showing {len(runs_shown)} of {len(runs)} agent runs[/]")
    return 0


def cmd_cost(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    if args.json:
        print(json.dumps({
            "totals": _bucket_json(analytics.totals(sessions)),
            "by_project": [_bucket_json(b) for b in analytics.by_project(sessions)],
            "by_model": [_bucket_json(b) for b in analytics.by_model(sessions)],
            "by_day": [
                {"date": d.isoformat(), **_bucket_json(b)}
                for d, b in analytics.by_day(sessions, args.days or 30)
            ],
            "cache": analytics.cache_efficiency(sessions),
        }, indent=2))
        return 0

    print_totals_banner(console, sessions, "COST")

    # Daily spend with an inline bar chart.
    days = analytics.by_day(sessions, args.days or 30)
    if days:
        peak = max((b.cost for _d, b in days), default=0.0) or 1.0
        dt = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
                   title=Text("daily spend", style=C_DIM), title_justify="left")
        dt.add_column("DATE", no_wrap=True)
        dt.add_column("", ratio=3, no_wrap=True)
        dt.add_column("SESSIONS", justify="right")
        dt.add_column("TOKENS", justify="right")
        dt.add_column("COST", justify="right")
        for d, b in days:
            if b.sessions == 0 and b.cost == 0:
                continue
            width = max(1, int(b.cost / peak * 38))
            dt.add_row(
                Text(d.strftime("%a %d %b"), style=C_TEXT),
                Text("█" * width, style=C_COST),
                f"{b.sessions}",
                pricing.fmt_tokens(b.usage.total),
                Text(pricing.fmt_usd(b.cost), style=f"bold {C_COST}"),
            )
        console.print(dt)

    # By project.
    projects = analytics.by_project(sessions)[:15]
    if projects:
        total = sum(b.cost for b in projects) or 1.0
        pt = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
                   title=Text("by project", style=C_DIM), title_justify="left")
        pt.add_column("PROJECT", no_wrap=True, max_width=24)
        pt.add_column("", ratio=2, no_wrap=True)
        pt.add_column("SESSIONS", justify="right")
        pt.add_column("AGENTS", justify="right")
        pt.add_column("TOKENS", justify="right")
        pt.add_column("CACHE", justify="right")
        pt.add_column("COST", justify="right")
        for b in projects:
            width = max(1, int(b.cost / total * 26))
            pt.add_row(
                Text(_truncate(b.key, 24), style=C_TEXT),
                Text("█" * width, style=C_PRIMARY),
                f"{b.sessions}",
                Text(f"{b.agents}" if b.agents else "·", style=C_AGENT),
                pricing.fmt_tokens(b.usage.total),
                f"{b.usage.cache_hit_rate * 100:.0f}%",
                Text(pricing.fmt_usd(b.cost), style=f"bold {C_COST}"),
            )
        console.print(pt)

    # By model, with the effective blended rate.
    models = analytics.by_model(sessions)
    if models:
        mt = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
                   title=Text("by model", style=C_DIM), title_justify="left")
        mt.add_column("MODEL", no_wrap=True)
        mt.add_column("RATE in/out per MTok", no_wrap=True)
        mt.add_column("CALLS", justify="right")
        mt.add_column("INPUT", justify="right")
        mt.add_column("OUTPUT", justify="right")
        mt.add_column("CACHE HIT", justify="right")
        mt.add_column("COST", justify="right")
        for b in models:
            r = pricing.rate_for(b.key)
            label = f"${r.input:g} / ${r.output:g}"
            if r.legacy:
                label += " *"
            mt.add_row(
                Text(pricing.display_name(b.key), style=dashboard._model_style(b.key)),
                Text(label, style=C_DIM),
                f"{b.api_calls:,}",
                pricing.fmt_tokens(b.usage.total_input),
                pricing.fmt_tokens(b.usage.output_tokens),
                f"{b.usage.cache_hit_rate * 100:.0f}%",
                Text(pricing.fmt_usd(b.cost), style=f"bold {C_COST}"),
            )
        console.print(mt)
        if any(pricing.rate_for(b.key).legacy for b in models):
            console.print(f"[{C_DIM}]  * legacy/retired model — rate is a historical estimate[/]")

    # Where the money actually goes, by token type — priced per model with
    # its own cache multipliers so the parts sum to the exact total.
    econ = analytics.token_economics(sessions)
    et = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
               title=Text("by token type", style=C_DIM), title_justify="left")
    et.add_column("TYPE", no_wrap=True)
    et.add_column("", ratio=2, no_wrap=True)
    et.add_column("TOKENS", justify="right")
    et.add_column("COST", justify="right")
    et.add_column("SHARE", justify="right")

    ROWS = [
        ("cache read", "cache_read", C_LIVE),
        ("cache write · 5m", "cache_write_5m", C_COST),
        ("cache write · 1h", "cache_write_1h", C_COST),
        ("output", "output", C_ERR),
        ("input, uncached", "input", C_TEAL),
    ]
    ecost = econ["cost"]
    peak_c = max(ecost.values()) or 1.0
    for label, key, style in sorted(ROWS, key=lambda r: -ecost[r[1]]):
        c = ecost[key]
        share = (c / econ["total_cost"] * 100.0) if econ["total_cost"] else 0.0
        et.add_row(
            Text(label, style=C_TEXT),
            Text("█" * max(1, int(c / peak_c * 30)), style=style),
            pricing.fmt_tokens(econ["tokens"][key]),
            Text(pricing.fmt_usd(c), style=f"bold {C_COST}"),
            f"{share:.1f}%",
        )
    console.print(et)

    mult = econ["multiple_of_list"]
    console.print(
        f"[{C_DIM}]  output is [/][{C_ERR}]{econ['output_share'] * 100:.2f}%[/]"
        f"[{C_DIM}] of all tokens — every output token is paid for by re-reading "
        f"the transcript that produced it.[/]"
    )
    console.print(
        f"[{C_DIM}]  effective cost per 1M output tokens: [/]"
        f"[bold {C_ERR}]{pricing.fmt_usd(econ['effective_output_rate'])}[/]"
        f"[{C_DIM}]  ({mult:.0f}× the "
        f"{pricing.fmt_usd(econ['list_output_rate'])} blended list rate)[/]\n"
    )

    # Cache economics.
    cache = analytics.cache_efficiency(sessions)
    cp = Table.grid(expand=True, padding=(0, 3))
    for _ in range(4):
        cp.add_column()
    def cell(k, v, style):
        t = Text(); t.append(f"{k}\n", style=C_DIM); t.append(v, style=f"bold {style}")
        return t
    cp.add_row(
        cell("CACHE HIT RATE", f"{cache['hit_rate'] * 100:.1f}%", C_LIVE),
        cell("READ FROM CACHE", pricing.fmt_tokens(cache["cache_read"]), C_TEAL),
        cell("WRITTEN TO CACHE",
             f"{pricing.fmt_tokens(cache['cache_write_5m'])} 5m / "
             f"{pricing.fmt_tokens(cache['cache_write_1h'])} 1h", C_TEAL),
        cell("SAVED VS NO CACHE",
             f"{pricing.fmt_usd(cache['savings'])} ({cache['savings_pct']:.0f}%)", C_LIVE),
    )
    console.print(Panel(cp, title=Text(" PROMPT CACHE ", style=f"bold {C_LIVE}"),
                        title_align="left", border_style=C_DIM, box=box.ROUNDED,
                        padding=(0, 1)))
    return 0


def cmd_tools(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    rows = analytics.tool_summary(sessions)
    if args.json:
        print(json.dumps(
            [{"tool": n, "main": m, "agent": s, "total": m + s} for n, m, s in rows],
            indent=2))
        return 0

    total = sum(m + s for _n, m, s in rows) or 1
    errors = sum(s.tool_errors for s in sessions) + sum(
        a.tool_errors for s in sessions for a in s.agents
    )

    print_totals_banner(console, sessions, "TOOL USE")

    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C_DIM}")
    t.add_column("TOOL", no_wrap=True, max_width=22)
    t.add_column("", ratio=3, no_wrap=True)
    t.add_column("MAIN", justify="right")
    t.add_column("AGENT", justify="right")
    t.add_column("TOTAL", justify="right")
    t.add_column("SHARE", justify="right")
    for name, main, sub in rows[: args.limit]:
        n = main + sub
        width = max(1, int(n / total * 40))
        main_w = int(width * (main / n)) if n else 0
        bar = Text("█" * main_w, style=C_PRIMARY)
        bar.append("█" * (width - main_w), style=C_AGENT)
        t.add_row(
            Text(name, style=C_TEXT), bar,
            f"{main:,}" if main else "·",
            Text(f"{sub:,}", style=C_AGENT) if sub else "·",
            f"{n:,}",
            f"{n / total * 100:.1f}%",
        )
    console.print(t)
    console.print(
        f"[{C_DIM}]  {total:,} tool calls · [/][{C_PRIMARY}]█ main thread[/]"
        f"[{C_DIM}]  [/][{C_AGENT}]█ subagents[/]"
        f"[{C_DIM}]  ·  [/][{C_ERR}]{errors:,} errored[/]"
    )
    return 0


def cmd_show(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    ResourceMonitor().attach(sessions)

    needle = args.session_id.lower()
    matches = [s for s in sessions if s.session_id.lower().startswith(needle)]
    if not matches:
        matches = [
            s for s in sessions
            if needle in (s.title or "").lower() or needle in s.project.lower()
        ]
    if not matches:
        console.print(f"[{C_ERR}]No session matching '{args.session_id}'[/]")
        return 1
    sess = max(matches, key=lambda s: s.ended or datetime.min.replace(tzinfo=timezone.utc))

    u = sess.total_usage
    header = Table.grid(expand=True, padding=(0, 3))
    for _ in range(4):
        header.add_column()

    def cell(k, v, style=C_TEXT):
        t = Text(); t.append(f"{k}\n", style=C_DIM); t.append(str(v), style=f"bold {style}")
        return t

    status = "● LIVE" if sess.is_live else "idle"
    header.add_row(
        cell("STATUS", status, C_LIVE if sess.is_live else C_DIM),
        cell("PROJECT", sess.project),
        cell("BRANCH", sess.git_branch or "—"),
        cell("CLI VERSION", sess.version or "—"),
    )
    header.add_row(
        cell("STARTED", sess.started.astimezone().strftime("%Y-%m-%d %H:%M") if sess.started else "—"),
        cell("DURATION", fmt_duration(sess.duration_s)),
        cell("GENERATING", fmt_duration(sess.active_seconds), C_TEAL),
        cell("LAST ACTIVITY", fmt_ago(sess.ended)),
    )
    header.add_row(
        cell("USER TURNS", sess.user_turns),
        cell("API CALLS", f"{sess.api_calls:,}"),
        cell("SUBAGENTS", len(sess.agents), C_AGENT),
        cell("PERMISSION", sess.permission_mode or "—"),
    )
    if sess.is_live:
        header.add_row(
            cell("PID", sess.pid, C_LIVE),
            cell("CPU", f"{sess.cpu_percent:.1f}%", C_LIVE),
            cell("MEMORY", fmt_bytes(sess.rss_bytes), C_LIVE),
            cell("THREADS", sess.num_threads, C_LIVE),
        )

    title = Text()
    title.append(" ", style="")
    title.append(sess.title or sess.session_id, style=f"bold {C_PRIMARY}")
    title.append(f"  {sess.session_id[:8]} ", style=C_DIM)
    console.print(Panel(header, title=title, title_align="left",
                        border_style=C_PRIMARY, box=box.HEAVY, padding=(0, 1)))

    # Token & cost breakdown.
    bt = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
               title=Text("tokens & cost", style=C_DIM), title_justify="left")
    bt.add_column("BUCKET", no_wrap=True)
    bt.add_column("TOKENS", justify="right")
    bt.add_column("RATE MULT", justify="right")
    bt.add_column("COST", justify="right")

    primary = sess.primary_model
    r = pricing.rate_for(primary)
    def money(tok, mult, is_output=False):
        base = r.output if is_output else r.input
        return tok * base * mult / 1e6

    bt.add_row("fresh input", pricing.fmt_tokens(u.input_tokens), "1.00×",
               Text(pricing.fmt_usd(money(u.input_tokens, 1.0)), style=C_COST))
    bt.add_row("cache write 5m", pricing.fmt_tokens(u.cache_write_5m), "1.25×",
               Text(pricing.fmt_usd(money(u.cache_write_5m, 1.25)), style=C_COST))
    bt.add_row("cache write 1h", pricing.fmt_tokens(u.cache_write_1h), "2.00×",
               Text(pricing.fmt_usd(money(u.cache_write_1h, 2.0)), style=C_COST))
    bt.add_row(Text("cache read", style=C_LIVE), pricing.fmt_tokens(u.cache_read), "0.10×",
               Text(pricing.fmt_usd(money(u.cache_read, 0.1)), style=C_LIVE))
    bt.add_row("output", pricing.fmt_tokens(u.output_tokens), "1.00×",
               Text(pricing.fmt_usd(money(u.output_tokens, 1.0, True)), style=C_COST))
    bt.add_row(
        Text("TOTAL", style=f"bold {C_TEXT}"),
        Text(pricing.fmt_tokens(u.total), style=f"bold {C_TEXT}"), "",
        Text(pricing.fmt_usd(sess.total_cost), style=f"bold {C_COST}"),
    )
    console.print(bt)

    saved = max(0.0, sess.uncached_cost - sess.cost)
    console.print(
        f"[{C_DIM}]  cache hit rate [/][{C_LIVE}]{u.cache_hit_rate * 100:.1f}%[/]"
        f"[{C_DIM}] · saved [/][{C_LIVE}]{pricing.fmt_usd(saved)}[/]"
        f"[{C_DIM}] vs {pricing.fmt_usd(sess.uncached_cost)} uncached"
        f" · peak context {pricing.fmt_tokens(sess.peak_context)}"
        f" · avg output {sess.output_tps:,.0f} tok/s[/]"
    )

    # Velocity + context sparklines.
    vel = analytics.velocity_series(sess, bucket_s=60.0)
    ctx = analytics.context_series(sess)
    if vel:
        console.print(
            f"\n[{C_DIM}]  output tok/s  [/][{C_TEAL}]"
            f"{sparkline([v for _t, v in vel], width=60)}[/]"
            f"[{C_DIM}]  peak {max(v for _t, v in vel):,.0f}/s[/]"
        )
    if ctx:
        console.print(
            f"[{C_DIM}]  context size  [/][{C_COST}]"
            f"{sparkline([float(c) for _t, c in ctx], width=60)}[/]"
            f"[{C_DIM}]  peak {pricing.fmt_tokens(sess.peak_context)}[/]"
        )
    cs = analytics.cost_series(sess)
    if cs:
        console.print(
            f"[{C_DIM}]  cumulative $ [/][{C_COST}]"
            f"{sparkline([c for _t, c in cs], width=60)}[/]"
            f"[{C_DIM}]  {pricing.fmt_usd(cs[-1][1])}[/]\n"
        )

    # Tools.
    if sess.tool_counts:
        tools = sorted(sess.tool_counts.items(), key=lambda kv: -kv[1])
        line = "  ".join(f"[{C_TEXT}]{n}[/][{C_DIM}]×{c}[/]" for n, c in tools[:14])
        console.print(f"[{C_DIM}]  tools  [/]{line}")
        if sess.tool_errors:
            console.print(f"[{C_ERR}]  {sess.tool_errors} tool errors[/]")

    # Subagents.
    if sess.agents:
        at = Table(box=box.SIMPLE, expand=True, header_style=f"bold {C_DIM}",
                   title=Text("subagents", style=C_AGENT), title_justify="left")
        at.add_column("", width=2)
        at.add_column("TOPIC", ratio=3, no_wrap=True)
        at.add_column("TYPE", no_wrap=True, max_width=16)
        at.add_column("MODEL", no_wrap=True, max_width=11)
        at.add_column("CALLS", justify="right")
        at.add_column("TOKENS", justify="right")
        at.add_column("TIME", justify="right")
        at.add_column("TOK/S", justify="right")
        at.add_column("COST", justify="right")
        at.add_column("STARTED", justify="right", no_wrap=True)
        glyphs = {"done": ("✓", C_LIVE), "running": ("▶", C_COST), "stopped": ("⊘", C_DIM)}
        # Running first, then by cost.
        ordered = sorted(
            sess.agents,
            key=lambda x: (x.state(parent_live=sess.is_live) != "running", -x.cost),
        )
        for a in ordered:
            ch, st = glyphs[a.state(parent_live=sess.is_live)]
            at.add_row(
                Text(ch, style=st),
                Text(_truncate(a.topic, 56)),
                Text(_truncate(a.subagent_type or "—", 16), style=C_AGENT),
                Text(pricing.display_name(a.model), style=dashboard._model_style(a.model)),
                f"{a.api_calls}",
                pricing.fmt_tokens(a.usage.total),
                fmt_duration(a.duration_s),
                f"{a.output_tps:,.0f}" if a.output_tps else "—",
                Text(pricing.fmt_usd(a.cost), style=C_COST),
                Text(fmt_ago(a.started), style=C_DIM),
            )
        console.print(at)
        console.print(
            f"[{C_DIM}]  main thread [/][{C_COST}]{pricing.fmt_usd(sess.cost)}[/]"
            f"[{C_DIM}]  ·  subagents [/][{C_AGENT}]{pricing.fmt_usd(sess.agent_cost)}[/]"
            f"[{C_DIM}]  ({sess.agent_cost / sess.total_cost * 100:.0f}% of total)[/]"
            if sess.total_cost else ""
        )

    if args.prompt and sess.last_prompt:
        console.print(Panel(Text(sess.last_prompt, style=C_TEXT),
                            title=Text(" last prompt ", style=C_DIM),
                            border_style=C_DIM, box=box.ROUNDED))
    return 0


def cmd_workflows(console: Console, args) -> int:
    _corpus, sessions = load_sessions(console, args)
    ResourceMonitor().attach(sessions)
    runs = analytics.all_workflows(sessions)
    shown = runs[: args.limit]

    if args.json:
        print(json.dumps([{
            "workflow_id": w.workflow_id,
            "session_id": w.session_id,
            "project": w.project,
            "topic": w.topic,
            "agents": len(w.agents),
            "completed": w.completed,
            "peak_parallelism": w.peak_parallelism,
            "duration_s": w.duration_s,
            "tokens": w.usage.total,
            "cost_usd": w.cost,
        } for w in shown], indent=2))
        return 0

    total_cost = sum(w.cost for w in runs)
    total_agents = sum(len(w.agents) for w in runs)

    grid = Table.grid(expand=True, padding=(0, 3))
    for _ in range(5):
        grid.add_column()

    def cell(k, v, style):
        t = Text(); t.append(f"{k}\n", style=C_DIM); t.append(v, style=f"bold {style}")
        return t

    grid.add_row(
        cell("WORKFLOWS", f"{len(runs):,}", C_TEAL),
        cell("AGENTS FANNED OUT", f"{total_agents:,}", C_AGENT),
        cell("PEAK PARALLELISM",
             f"{max((w.peak_parallelism for w in runs), default=0)}", C_LIVE),
        cell("COST", pricing.fmt_usd(total_cost), C_COST),
        cell("AVG / WORKFLOW",
             pricing.fmt_usd(total_cost / len(runs)) if runs else "—", C_COST),
    )
    console.print(Panel(grid, title=Text(" WORKFLOWS ", style=f"bold {C_TEAL}"),
                        title_align="left", border_style=C_TEAL,
                        box=box.HEAVY, padding=(0, 1)))

    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C_DIM}")
    t.add_column("", width=2)
    t.add_column("WORKFLOW", no_wrap=True, max_width=16)
    t.add_column("PROJECT", no_wrap=True, max_width=14)
    t.add_column("TOPIC", ratio=3, no_wrap=True)
    t.add_column("AGENTS", justify="right")
    t.add_column("PAR", justify="right", width=4)
    t.add_column("TOKENS", justify="right")
    t.add_column("TIME", justify="right")
    t.add_column("COST", justify="right")
    t.add_column("WHEN", justify="right", no_wrap=True)

    for w in shown:
        if w.running:
            mark = Text("▶", style=C_COST)
        elif w.completed == len(w.agents):
            mark = Text("✓", style=C_LIVE)
        else:
            mark = Text("⊘", style=C_DIM)
        t.add_row(
            mark,
            Text(w.workflow_id.replace("wf_", ""), style=C_TEAL),
            Text(_truncate(w.project, 14), style=C_DIM),
            Text(_truncate(w.topic, 58)),
            Text(f"{w.completed}/{len(w.agents)}", style=C_AGENT),
            Text(f"×{w.peak_parallelism}", style=C_LIVE),
            pricing.fmt_tokens(w.usage.total),
            fmt_duration(w.duration_s),
            Text(pricing.fmt_usd(w.cost), style=C_COST),
            Text(fmt_ago(w.started), style=C_DIM),
        )
    console.print(t)
    console.print(
        f"[{C_DIM}]  Showing {len(shown)} of {len(runs)} workflow runs · "
        f"PAR = peak agents running concurrently[/]"
    )
    return 0


def cmd_web(console: Console, args) -> int:
    try:
        from .web.server import create_app
    except ImportError:
        console.print(
            f"[{C_ERR}]Flask is required for the web UI:[/] pip install flask"
        )
        return 1

    # There is no authentication: anyone who can reach the port can read every
    # prompt, path and project name in the corpus. Loopback keeps that to this
    # machine; anything else is a decision the user should make knowingly.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[{C_ERR}]⚠ Serving on {args.host} — the UI has no authentication.[/]\n"
            f"[{C_DIM}]  Everyone who can reach this port can read your prompts, "
            f"file paths and project names.[/]"
        )

    app = create_app(claude_dir=args.claude_dir, allow_network=not args.no_network)
    store = app.config["STORE"]

    paths = store.corpus.session_paths()
    with Progress(
        SpinnerColumn(style=C_PRIMARY),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=C_PRIMARY, finished_style=C_LIVE),
        TextColumn("{task.completed}/{task.total}"),
        console=console, transient=True,
    ) as prog:
        task = prog.add_task("indexing transcripts", total=len(paths))
        store.load_initial(
            progress=lambda done, total, name: prog.update(
                task, completed=done, description=f"indexing {name[:8]}"
            )
        )

    app.config["GITMON"].start()
    n = len(store.sessions())
    url = f"http://{args.host}:{args.port}"
    console.print(
        f"[{C_LIVE}]●[/] Claude Code Monitor  [{C_PRIMARY}]{url}[/]\n"
        f"[{C_DIM}]  {n} sessions indexed · Ctrl+C to stop[/]"
    )
    if args.open:
        import webbrowser
        webbrowser.open(url)

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        # Persist whatever the throttled background saves have not written yet,
        # so the next start doesn't re-parse work already done.
        store.stop()
        app.config["GITMON"].stop()
        store.corpus.save_cache()
    return 0


def cmd_report(console: Console, args) -> int:
    from . import report

    _corpus, sessions = load_sessions(console, args)
    out = Path(args.output).expanduser().resolve()
    report.write_report(sessions, out, window_days=args.days or 30)
    console.print(f"[{C_LIVE}]✓[/] wrote report to [{C_PRIMARY}]{out}[/]")
    console.print(f"[{C_DIM}]  open with: xdg-open {out}[/]")
    return 0


def cmd_reindex(console: Console, args) -> int:
    args.reindex = True
    corpus, sessions = load_sessions(console, args)
    console.print(
        f"[{C_LIVE}]✓[/] reindexed {len(sessions)} sessions "
        f"[{C_DIM}]→ {corpus.cache_path}[/]"
    )
    return 0


# ---------------------------------------------------------------------------
# JSON serialisation for --json
# ---------------------------------------------------------------------------


def _session_json(s: Session) -> dict:
    u = s.total_usage
    return {
        "session_id": s.session_id,
        "project": s.project,
        "cwd": s.cwd,
        "title": s.title,
        "model": s.primary_model,
        "models": s.models,
        "started": s.started.isoformat() if s.started else None,
        "ended": s.ended.isoformat() if s.ended else None,
        "duration_s": s.duration_s,
        "active_seconds": s.active_seconds,
        "user_turns": s.user_turns,
        "api_calls": s.api_calls,
        "tokens": {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_write_5m": u.cache_write_5m,
            "cache_write_1h": u.cache_write_1h,
            "cache_read": u.cache_read,
            "total": u.total,
        },
        "peak_context": s.peak_context,
        "cost_usd": s.total_cost,
        "cost_main_usd": s.cost,
        "cost_agents_usd": s.agent_cost,
        "uncached_cost_usd": s.uncached_cost,
        "cache_hit_rate": u.cache_hit_rate,
        "output_tps": s.output_tps,
        "tools": s.tool_counts,
        "tool_errors": s.tool_errors,
        "agents": len(s.agents),
        "live": s.is_live,
        "pid": s.pid,
    }


def _agent_json(a) -> dict:
    return {
        "agent_id": a.agent_id,
        "session_id": a.session_id,
        "topic": a.topic,
        "subagent_type": a.subagent_type,
        "model": a.model,
        "started": a.started.isoformat() if a.started else None,
        "duration_s": a.duration_s,
        "api_calls": a.api_calls,
        "tokens": a.usage.total,
        "output_tokens": a.usage.output_tokens,
        "cost_usd": a.cost,
        "completed": a.completed,
        "tools": a.tool_counts,
        "tool_errors": a.tool_errors,
    }


def _bucket_json(b) -> dict:
    return {
        "key": b.key,
        "sessions": b.sessions,
        "agents": b.agents,
        "api_calls": b.api_calls,
        "tokens": b.usage.total,
        "output_tokens": b.usage.output_tokens,
        "cache_hit_rate": b.usage.cache_hit_rate,
        "cost_usd": b.cost,
        "uncached_cost_usd": b.uncached_cost,
        "savings_usd": b.savings,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cmon",
        description="Claude Code Monitor — sessions, subagents, cost and throughput.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  cmon                      live dashboard
  cmon live --window 6      dashboard scoped to the last 6 hours
  cmon sessions --days 7    sessions from the past week
  cmon agents --limit 40    recent subagent runs
  cmon cost --days 14       spend breakdown
  cmon show 434cae12        deep dive on one session
  cmon web                  browsable web UI at localhost:8787
  cmon report -o out.html   standalone HTML report
""",
    )
    p.add_argument("--claude-dir", type=Path, default=None,
                   help="path to the Claude config dir (default: ~/.claude)")
    p.add_argument("--reindex", action="store_true",
                   help="ignore the cache and re-parse every transcript")
    p.add_argument("--no-color", action="store_true", help="disable colour output")

    sub = p.add_subparsers(dest="command")

    def common(sp, days_default=None):
        sp.add_argument("--days", type=int, default=days_default,
                        help="only include sessions active in the last N days")
        sp.add_argument("--project", help="filter by project name or path substring")
        sp.add_argument("--model", help="filter by model id substring")
        return sp

    sp = sub.add_parser("live", help="live dashboard (default)")
    common(sp)
    sp.add_argument("--interval", type=float, default=2.0, help="refresh seconds")
    sp.add_argument("--window", type=float, default=24.0,
                    help="trailing window in hours for aggregate panels")
    sp.add_argument("--live-only", action="store_true",
                    help="show only running sessions")
    sp.set_defaults(func=cmd_live)

    sp = common(sub.add_parser("sessions", help="list sessions"))
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--sort", default="recent",
                    choices=["recent", "cost", "tokens", "duration", "agents"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sessions)

    sp = common(sub.add_parser("agents", help="list subagent runs"))
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--type", help="filter by subagent_type substring")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_agents)

    sp = common(sub.add_parser("cost", help="cost breakdown"), days_default=30)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_cost)

    sp = common(sub.add_parser("tools", help="tool usage breakdown"))
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_tools)

    sp = sub.add_parser("web", help="browsable web UI (recommended)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--open", action="store_true", help="open a browser window")
    sp.add_argument("--no-network", action="store_true",
                    help="never call Anthropic for live plan limits; use only "
                         "Claude Code's cached copy")
    sp.set_defaults(func=cmd_web)

    sp = common(sub.add_parser("workflows", help="workflow fan-outs and parallelism"))
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_workflows)

    sp = common(sub.add_parser("show", help="deep dive on one session"))
    sp.add_argument("session_id", help="session id prefix, title or project")
    sp.add_argument("--prompt", action="store_true", help="also print the last prompt")
    sp.set_defaults(func=cmd_show)

    sp = common(sub.add_parser("report", help="write a standalone HTML report"),
                days_default=30)
    sp.add_argument("-o", "--output", default="claude-report.html")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("reindex", help="rebuild the parse cache")
    sp.set_defaults(func=cmd_reindex)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Resolve argv up front: the re-parse below must see the same arguments.
    # `argv or []` here would silently drop global flags from a bare
    # `cmon --no-color`, because argv is None when invoked from the shell.
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    # Bare `cmon` launches the dashboard.
    if not getattr(args, "command", None):
        args = parser.parse_args(list(argv) + ["live"])

    console = Console(no_color=args.no_color, soft_wrap=False)
    # Subcommands that don't take the filter flags still need the attributes.
    for attr, default in (("days", None), ("project", None), ("model", None),
                          ("reindex", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    try:
        return args.func(console, args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
