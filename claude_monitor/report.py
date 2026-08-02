"""Standalone HTML report generator.

Emits a single self-contained file — no external CSS, JS, fonts or images — so
it can be opened from disk or emailed. Charts are plain HTML/CSS bars: every
value is direct-labelled and every chart is backed by a table, which is also
what satisfies the contrast-relief rule for the lighter palette slots.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from . import analytics, pricing
from .analytics import fmt_duration
from .models import Session

# Categorical slots 1-5 from the validated reference palette. Assigned in
# fixed order and never cycled; light and dark are separately stepped sets,
# both validated (adjacent CVD ΔE 9.1 light / 8.4 dark).
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]

CSS = """
:root{
  color-scheme:light;
  --surface:#fcfcfb; --panel:#ffffff; --sunk:#eeeeec; --rule:#e2e2df;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#87867f;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4;
  --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 26px -14px rgba(0,0,0,.16);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI","Liberation Sans",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --surface:#1a1a19; --panel:#232322; --sunk:#2e2e2c; --rule:#3a3a37;
    --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e85;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
    --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 34px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface:#1a1a19; --panel:#232322; --sunk:#2e2e2c; --rule:#3a3a37;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e85;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 34px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);font-family:var(--sans);
     line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:44px 24px 90px}
h1{font-size:clamp(1.7rem,4vw,2.5rem);letter-spacing:-.03em;margin:0 0 6px;line-height:1.1}
h2{font-size:1.22rem;letter-spacing:-.02em;margin:0}
.sub{color:var(--ink-3);font-size:.9rem;margin:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font-family:var(--mono);font-size:.66rem;font-weight:600;letter-spacing:.16em;
         text-transform:uppercase;color:var(--ink-3)}
section{margin-top:52px}
.shead{display:flex;align-items:baseline;gap:14px;margin-bottom:6px}
.shead .line{flex:1;height:1px;background:var(--rule)}
.lead{color:var(--ink-2);font-size:.94rem;margin:10px 0 0;max-width:70ch}

/* stat tiles */
.tiles{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));margin-top:26px}
.tile{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:18px 20px;
      box-shadow:var(--shadow)}
.tile .v{display:block;font-family:var(--mono);font-variant-numeric:tabular-nums;
         font-size:1.62rem;font-weight:600;letter-spacing:-.03em;line-height:1.1}
.tile .k{display:block;margin-top:8px}
.tile .n{display:block;margin-top:4px;font-size:.76rem;color:var(--ink-3)}
.tile.lead .v{color:var(--s1)}

/* bar charts */
.chart{margin-top:22px;display:flex;flex-direction:column;gap:9px}
.row{display:grid;grid-template-columns:minmax(96px,190px) 1fr minmax(104px,auto);
     gap:14px;align-items:center}
.row .lab{font-size:.845rem;color:var(--ink-2);text-align:right;line-height:1.25;
          overflow-wrap:anywhere}
.track{position:relative;height:26px;background:var(--sunk);border-radius:3px}
.bar{height:100%;border-radius:0 4px 4px 0;min-width:2px}
.val{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.83rem;
     font-weight:600;white-space:nowrap}
.val .pct{color:var(--ink-3);font-weight:400;margin-left:7px;font-size:.75rem}
.track[data-tip]{cursor:default}
.track[data-tip]::after{content:attr(data-tip);position:absolute;left:0;bottom:calc(100% + 7px);
  background:var(--ink);color:var(--surface);font-family:var(--mono);font-size:.7rem;
  line-height:1.45;padding:7px 10px;border-radius:4px;white-space:pre;opacity:0;
  pointer-events:none;transition:opacity .12s;z-index:9;box-shadow:var(--shadow)}
.track[data-tip]:hover::after,.row:focus-within .track[data-tip]::after{opacity:1}

/* column chart */
.cols{margin-top:22px;display:flex;align-items:flex-end;gap:2px;height:170px;
      padding:0 2px;border-bottom:1px solid var(--rule)}
.col{flex:1;height:100%;position:relative;display:flex;align-items:flex-end;min-width:4px}
.col i{display:block;width:100%;background:var(--s1);border-radius:3px 3px 0 0;min-height:1px}
.col[data-tip]::after{content:attr(data-tip);position:absolute;left:50%;transform:translateX(-50%);
  bottom:calc(100% + 6px);background:var(--ink);color:var(--surface);font-family:var(--mono);
  font-size:.7rem;line-height:1.45;padding:7px 10px;border-radius:4px;white-space:pre;opacity:0;
  pointer-events:none;transition:opacity .12s;z-index:9;box-shadow:var(--shadow)}
.col:hover::after{opacity:1}
.colaxis{display:flex;justify-content:space-between;margin-top:7px;font-family:var(--mono);
         font-size:.68rem;color:var(--ink-3)}

/* legend */
.legend{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:18px}
.lg{display:flex;align-items:center;gap:8px;font-size:.8rem;color:var(--ink-2)}
.lg .sw{width:11px;height:11px;border-radius:2px;flex:none}

/* tables */
.tw{margin-top:22px;overflow-x:auto;border:1px solid var(--rule);border-radius:8px;
    background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:.845rem;min-width:560px}
th,td{padding:9px 14px;text-align:left;border-bottom:1px solid var(--rule)}
thead th{font-family:var(--mono);font-size:.64rem;letter-spacing:.11em;text-transform:uppercase;
         color:var(--ink-3);font-weight:600;background:var(--sunk);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr.total td{font-weight:600;background:var(--sunk)}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
tbody tr:hover td{background:var(--sunk)}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:8px;
     vertical-align:baseline}
details{margin-top:14px}
summary{cursor:pointer;font-size:.82rem;color:var(--ink-3);font-family:var(--mono)}
summary:hover{color:var(--ink-2)}
.note{margin-top:26px;padding:20px 24px;background:var(--panel);border:1px solid var(--rule);
      border-radius:8px}
.note p{font-size:.85rem;color:var(--ink-2);line-height:1.6;margin:0}
.note p+p{margin-top:10px}
.note code{font-family:var(--mono);font-size:.86em;background:var(--sunk);padding:2px 5px;
           border-radius:3px}
.note strong{color:var(--ink)}
footer{margin-top:60px;padding-top:22px;border-top:1px solid var(--rule);
       color:var(--ink-3);font-size:.78rem;line-height:1.6}
@media (max-width:640px){
  .row{grid-template-columns:1fr;gap:4px}
  .row .lab{text-align:left}
}
"""


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _bar_rows(
    items: Sequence[tuple],
    *,
    colors: Optional[Sequence[str]] = None,
    single: str = "var(--s1)",
) -> str:
    """Render ranked horizontal bars.

    ``items`` is ``(label, value, value_text, share_text, tooltip)``. Colour is
    a single hue unless ``colors`` supplies per-row identity — a value ramp
    over nominal categories would double-encode length as hue.
    """
    if not items:
        return '<p class="sub">No data.</p>'
    peak = max((v for _l, v, *_ in items), default=0.0) or 1.0
    out = ['<div class="chart">']
    for i, (label, value, vtext, share, tip) in enumerate(items):
        colour = colors[i] if colors else single
        width = max(0.4, value / peak * 100.0)
        out.append(
            f'<div class="row" tabindex="0">'
            f'<span class="lab">{esc(label)}</span>'
            f'<div class="track" data-tip="{esc(tip)}">'
            f'<div class="bar" style="width:{width:.2f}%;background:{colour}"></div></div>'
            f'<span class="val">{esc(vtext)}'
            + (f'<span class="pct">{esc(share)}</span>' if share else "")
            + "</span></div>"
        )
    out.append("</div>")
    return "".join(out)


def _legend(pairs: Sequence[tuple]) -> str:
    items = "".join(
        f'<span class="lg"><span class="sw" style="background:{c}"></span>{esc(n)}</span>'
        for n, c in pairs
    )
    return f'<div class="legend">{items}</div>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           numeric: Sequence[int] = (), total_row: bool = False) -> str:
    head = "".join(
        f'<th scope="col"{" class=n" if i in numeric else ""}>{esc(h)}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for ri, row in enumerate(rows):
        cls = ' class="total"' if (total_row and ri == len(rows) - 1) else ""
        cells = "".join(
            f'<td{" class=n" if i in numeric else ""}>{c}</td>'
            for i, c in enumerate(row)
        )
        body.append(f"<tr{cls}>{cells}</tr>")
    return (
        f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _section(eyebrow: str, title: str, lead: str = "") -> str:
    lead_html = f'<p class="lead">{lead}</p>' if lead else ""
    return (
        f'<section><div class="shead"><span class="eyebrow">{esc(eyebrow)}</span>'
        f'<span class="line"></span></div><h2>{esc(title)}</h2>{lead_html}'
    )


def build_html(sessions: List[Session], *, window_days: int = 30) -> str:
    tot = analytics.totals(sessions)
    econ = analytics.token_economics(sessions)
    cache = analytics.cache_efficiency(sessions)
    projects = analytics.by_project(sessions)
    models = analytics.by_model(sessions)
    agents = analytics.all_agents(sessions)
    workflows = analytics.all_workflows(sessions)
    days = analytics.by_day(sessions, days=window_days)
    generated = datetime.now().astimezone()

    parts: List[str] = []
    A = parts.append

    A('<div class="wrap">')

    # ---- header + hero tiles ---------------------------------------------
    A(f"<h1>Claude Code Monitor</h1>")
    A(
        f'<p class="sub">{tot.sessions:,} sessions · {tot.agents:,} subagent runs · '
        f'generated {esc(generated.strftime("%Y-%m-%d %H:%M %Z"))}</p>'
    )
    A('<div class="tiles">')
    A(
        f'<div class="tile lead"><span class="v">{esc(pricing.fmt_usd(tot.cost))}</span>'
        f'<span class="k eyebrow">Total cost</span>'
        f'<span class="n">API list-price equivalent</span></div>'
    )
    A(
        f'<div class="tile"><span class="v">{esc(pricing.fmt_tokens(tot.usage.total))}</span>'
        f'<span class="k eyebrow">Tokens</span>'
        f'<span class="n">{cache["hit_rate"] * 100:.1f}% served from cache</span></div>'
    )
    A(
        f'<div class="tile"><span class="v">{tot.api_calls:,}</span>'
        f'<span class="k eyebrow">API calls</span>'
        f'<span class="n">deduplicated</span></div>'
    )
    A(
        f'<div class="tile"><span class="v">'
        f'{esc(pricing.fmt_tokens(econ["tokens"]["output"]))}</span>'
        f'<span class="k eyebrow">Output tokens</span>'
        f'<span class="n">{econ["output_share"] * 100:.2f}% of all tokens</span></div>'
    )
    A(
        f'<div class="tile"><span class="v">'
        f'{esc(pricing.fmt_usd(econ["effective_output_rate"]))}</span>'
        f'<span class="k eyebrow">Effective / 1M out</span>'
        f'<span class="n">{econ["multiple_of_list"]:.0f}× list rate</span></div>'
    )
    A("</div>")

    # ---- 01 composition ---------------------------------------------------
    A(_section(
        "01 — Composition",
        "Where the money actually goes",
        "Every tool call re-sends the whole conversation, so the same context is "
        "billed again on every call. Splitting spend by token type shows how much "
        "of the bill is re-reading versus producing new work. Each bucket is priced "
        "with its own model's rate and cache multiplier, so the parts sum to the exact total.",
    ))
    TYPE_ROWS = [
        ("Cache read", "cache_read", "var(--s1)", pricing.CACHE_READ_MULT),
        ("Cache write · 5 min", "cache_write_5m", "var(--s2)", pricing.CACHE_WRITE_5M_MULT),
        ("Cache write · 1 hr", "cache_write_1h", "var(--s3)", pricing.CACHE_WRITE_1H_MULT),
        ("Output", "output", "var(--s4)", 1.0),
        ("Input, uncached", "input", "var(--s5)", 1.0),
    ]
    ordered = sorted(TYPE_ROWS, key=lambda r: -econ["cost"][r[1]])
    items, colors = [], []
    for label, key, colour, mult in ordered:
        c = econ["cost"][key]
        tk = econ["tokens"][key]
        share = (c / econ["total_cost"] * 100.0) if econ["total_cost"] else 0.0
        items.append((label, c, pricing.fmt_usd(c), f"{share:.1f}%",
                      f"{tk:,} tokens\n{mult:g}× the input rate"))
        colors.append(colour)
    A(_bar_rows(items, colors=colors))
    A(_legend([(l, c) for (l, _k, c, _m) in TYPE_ROWS]))
    A("<details><summary>Show as table</summary>")
    A(_table(
        ["Token type", "Tokens", "Rate multiple", "Cost", "Share"],
        [
            [
                f'<span class="dot" style="background:{c}"></span>{esc(l)}',
                f'{econ["tokens"][k]:,}',
                f"{m:g}×",
                esc(pricing.fmt_usd(econ["cost"][k])),
                f'{econ["cost"][k] / econ["total_cost"] * 100:.1f}%'
                if econ["total_cost"] else "0%",
            ]
            for l, k, c, m in ordered
        ] + [[
            "<strong>Total</strong>",
            f'{econ["total_tokens"]:,}', "",
            f'<strong>{esc(pricing.fmt_usd(econ["total_cost"]))}</strong>', "100%",
        ]],
        numeric=(1, 2, 3, 4), total_row=True,
    ))
    A("</details>")
    A("</section>")

    # ---- 02 cache counterfactual -----------------------------------------
    saved = cache["savings"]
    mult = (cache["uncached_cost"] / cache["cost"]) if cache["cost"] else 0.0
    A(_section(
        "02 — Counterfactual",
        "What prompt caching is worth",
        "Cache reads bill at one tenth of the input rate and cache writes at "
        "1.25× (5-minute TTL) or 2× (1-hour TTL). Repricing every cached token at "
        "the full input rate gives the counterfactual bill.",
    ))
    A(_bar_rows(
        [
            ("With caching", cache["cost"], pricing.fmt_usd(cache["cost"]), "",
             "what these sessions actually cost"),
            ("Without caching", cache["uncached_cost"],
             pricing.fmt_usd(cache["uncached_cost"]), "",
             "identical tokens at the full input rate"),
        ],
        colors=["var(--s3)", "var(--s2)"],
    ))
    A(
        f'<p class="lead"><strong>{mult:.1f}× more expensive without it.</strong> '
        f'Caching saved {esc(pricing.fmt_usd(saved))} '
        f'({cache["savings_pct"]:.0f}% of the counterfactual bill).</p>'
    )
    A("</section>")

    # ---- 03 daily spend ---------------------------------------------------
    if days:
        peak_day = max((b.cost for _d, b in days), default=0.0) or 1.0
        A(_section(
            f"03 — Last {window_days} days",
            "Daily spend",
            "One column per day, by session start date.",
        ))
        cols = "".join(
            f'<div class="col" data-tip="{esc(d.strftime("%a %d %b"))}\n'
            f'{esc(pricing.fmt_usd(b.cost))} · {b.sessions} sessions">'
            f'<i style="height:{max(0.6, b.cost / peak_day * 100):.1f}%"></i></div>'
            for d, b in days
        )
        A(f'<div class="cols">{cols}</div>')
        A(
            f'<div class="colaxis"><span>{esc(days[0][0].strftime("%d %b"))}</span>'
            f'<span>peak {esc(pricing.fmt_usd(peak_day))}/day</span>'
            f'<span>{esc(days[-1][0].strftime("%d %b"))}</span></div>'
        )
        A("</section>")

    # ---- 04 projects ------------------------------------------------------
    if projects:
        A(_section("04 — Projects", "Spend by project"))
        top = projects[:14]
        A(_bar_rows([
            (b.key, b.cost, pricing.fmt_usd(b.cost),
             f"{b.cost / tot.cost * 100:.0f}%" if tot.cost else "",
             f"{b.sessions} sessions · {b.agents} agents\n"
             f"{b.usage.total:,} tokens")
            for b in top
        ]))
        A("</section>")

    # ---- 05 models --------------------------------------------------------
    if models:
        A(_section(
            "05 — Models",
            "Spend by model",
            "Attributed per API call, not apportioned — tiers differ by up to 10× "
            "in price, so a session that switches models cannot be split by call count.",
        ))
        palette = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"]
        shown = models[:5]
        A(_bar_rows(
            [
                (pricing.display_name(b.key), b.cost, pricing.fmt_usd(b.cost),
                 f"{b.cost / tot.cost * 100:.0f}%" if tot.cost else "",
                 f"{b.api_calls:,} calls · {b.usage.total:,} tokens\n"
                 f"${pricing.rate_for(b.key).input:g} in / "
                 f"${pricing.rate_for(b.key).output:g} out per MTok")
                for b in shown
            ],
            colors=palette[: len(shown)],
        ))
        A(_legend([
            (pricing.display_name(b.key), palette[i]) for i, b in enumerate(shown)
        ]))
        A(_table(
            ["Model", "Calls", "Input tokens", "Output tokens", "Cache hit", "Cost"],
            [
                [
                    f'<span class="dot" style="background:{palette[i]}"></span>'
                    f'{esc(pricing.display_name(b.key))}',
                    f"{b.api_calls:,}",
                    f"{b.usage.total_input:,}",
                    f"{b.usage.output_tokens:,}",
                    f"{b.usage.cache_hit_rate * 100:.1f}%",
                    esc(pricing.fmt_usd(b.cost)),
                ]
                for i, b in enumerate(shown)
            ],
            numeric=(1, 2, 3, 4, 5),
        ))
        A("</section>")

    # ---- 06 agents --------------------------------------------------------
    if agents:
        by_cost = sorted(agents, key=lambda a: -a.cost)[:18]
        types = []
        for a in agents:
            t = a.subagent_type or "(unspecified)"
            if t not in types:
                types.append(t)
        # Fixed hue per agent type, never by rank — a filter must not repaint.
        palette = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"]
        tcolor = {t: palette[i % len(palette)] for i, t in enumerate(types[:5])}
        default = "var(--s1)"

        agent_cost = sum(a.cost for a in agents)
        A(_section(
            "06 — Subagents",
            "The most expensive agent runs",
            f"{len(agents):,} subagent runs cost {pricing.fmt_usd(agent_cost)} — "
            f"{agent_cost / tot.cost * 100:.0f}% of all spend. Colour is the agent "
            f"type, so identity never depends on rank.",
        ))
        A(_bar_rows(
            [
                (a.topic[:52], a.cost, pricing.fmt_usd(a.cost), "",
                 f"{a.subagent_type or 'unspecified'}\n"
                 f"{a.api_calls} calls · {a.usage.total:,} tokens\n"
                 f"{fmt_duration(a.duration_s)}")
                for a in by_cost
            ],
            colors=[tcolor.get(a.subagent_type or "(unspecified)", default)
                    for a in by_cost],
        ))
        A(_legend(list(tcolor.items())))
        A("</section>")

    # ---- 07 workflows -----------------------------------------------------
    if workflows:
        A(_section(
            "07 — Workflows",
            "Fan-out and parallelism",
            "Each workflow run and the agents it spawned. Peak parallelism is the "
            "maximum number of agents alive at the same instant, from a sweep over "
            "their start and end times.",
        ))
        A(_table(
            ["Workflow", "Project", "Topic", "Agents", "Peak parallel",
             "Duration", "Tokens", "Cost"],
            [
                [
                    f'<span class="num">{esc(w.workflow_id.replace("wf_", ""))}</span>',
                    esc(w.project),
                    esc(w.topic[:58]),
                    f"{w.completed}/{len(w.agents)}",
                    f"×{w.peak_parallelism}",
                    esc(fmt_duration(w.duration_s)),
                    f"{w.usage.total:,}",
                    esc(pricing.fmt_usd(w.cost)),
                ]
                for w in workflows[:16]
            ],
            numeric=(3, 4, 5, 6, 7),
        ))
        A("</section>")

    # ---- 08 sessions ------------------------------------------------------
    top_sessions = analytics.top_sessions(sessions, 20)
    A(_section("08 — Sessions", "Most expensive sessions"))
    A(_table(
        ["Project", "Topic", "Model", "Turns", "Agents", "Tokens",
         "Peak context", "Cost"],
        [
            [
                esc(s.project),
                esc((s.title or s.last_prompt or s.session_id)[:56]),
                esc(pricing.display_name(s.primary_model)),
                f"{s.user_turns}",
                f"{len(s.agents)}",
                f"{s.total_usage.total:,}",
                f"{s.peak_context:,}",
                esc(pricing.fmt_usd(s.total_cost)),
            ]
            for s in top_sessions
        ],
        numeric=(3, 4, 5, 6, 7),
    ))
    A("</section>")

    # ---- method -----------------------------------------------------------
    A(_section("09 — Method", "How these numbers were produced"))
    A('<div class="note">')
    A(
        "<p><strong>Source.</strong> Parsed directly from the Claude Code "
        "transcripts under <code>~/.claude/projects/</code> — one file per session "
        "plus one per subagent, including agents spawned by the Workflow tool. "
        "Every <code>usage</code> block is summed; nothing is estimated or sampled.</p>"
    )
    A(
        "<p><strong>Deduplication.</strong> A resumed session or agent replays its "
        "earlier messages into the transcript, so the same billed call can appear "
        "many times. Records are keyed on <code>message.id</code> + "
        "<code>requestId</code> and counted once per session. Without this, output "
        "tokens over-count by roughly 5×.</p>"
    )
    A(
        "<p><strong>Rates.</strong> Published per-million-token list prices, with "
        "cache reads at 0.1× the input rate and cache writes at 1.25× (5-minute TTL) "
        "or 2× (1-hour TTL). Retired models are priced at their last known rates and "
        "marked as estimates. Figures are list-price equivalents and not necessarily "
        "amounts billed — a subscription plan will differ.</p>"
    )
    A(
        "<p><strong>Token totals.</strong> Cache reads are counted once per API call, "
        "because that is how they are billed. A long session therefore reports far "
        "more tokens than its transcript contains — the same context is re-read on "
        "every call.</p>"
    )
    A("</div></section>")

    A(
        f'<footer>Generated by Claude Code Monitor on '
        f'{esc(generated.strftime("%Y-%m-%d %H:%M:%S %Z"))} · '
        f'covering {tot.sessions:,} sessions and {tot.agents:,} subagent runs.</footer>'
    )
    A("</div>")

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Claude Code Monitor · report</title>"
        f"<style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
    )


def write_report(sessions: List[Session], out: Path, *, window_days: int = 30) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(sessions, window_days=window_days), encoding="utf-8")
    return out
