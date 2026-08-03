"""Prompt export for a single session — structured JSON and standalone HTML.

Prompts are re-read from the transcript on demand rather than taken from the
parsed index. The index deliberately caps a session at 200 prompts and cuts
each to 2,000 characters, which is right for the browsing card and wrong for
an export that claims to be everything you typed.

The HTML file is self-contained — no external CSS, JS, fonts or images — so it
opens from disk, prints, and survives being emailed. It shares the palette and
typography of ``report.py`` so the two read as siblings.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .. import pricing
from ..analytics import fmt_duration
from ..models import Session, _utc
from ..parser import _is_human_turn, _prompt_text, iter_records

SCHEMA = "claude-code-monitor/session-prompts"
SCHEMA_VERSION = 1

# Beyond this a prompt is collapsed in the HTML page, with a control to open it.
CLAMP_CHARS = 1400
CLAMP_LINES = 22


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_prompts(path: Path) -> List[dict]:
    """Every human prompt in one transcript, in order, untruncated.

    A resumed session replays its earlier user records into the transcript, so
    records are deduplicated on ``uuid`` exactly as the parser does. Harness
    injections (system reminders, task notifications, command wrappers) are not
    prompts and are screened out by ``_is_human_turn``.
    """
    out: List[dict] = []
    seen: set = set()
    prev_ts: Optional[datetime] = None

    for rec, _off in iter_records(path):
        if rec.get("type") != "user":
            continue
        uid = rec.get("uuid")
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        if not _is_human_turn(rec):
            continue
        text = _prompt_text(rec).strip()
        if not text:
            continue

        ts = _utc(rec.get("timestamp"))
        gap = (ts - prev_ts).total_seconds() if ts and prev_ts else None
        if ts:
            prev_ts = ts
        out.append({
            "n": len(out) + 1,
            "ts": rec.get("timestamp"),
            "epoch": ts.timestamp() if ts else None,
            "gap_s": round(gap, 3) if gap and gap > 0 else None,
            "source": rec.get("promptSource") or "typed",
            "chars": len(text),
            "words": len(text.split()),
            "lines": text.count("\n") + 1,
            "text": text,
        })
    return out


def _session_meta(s: Session) -> dict:
    return {
        "id": s.session_id,
        "short": s.session_id[:8],
        "title": s.title or (s.last_prompt or "")[:90] or s.session_id[:8],
        "project": s.project,
        "cwd": s.cwd,
        "branch": s.git_branch,
        "model": s.primary_model,
        "model_label": pricing.display_name(s.primary_model),
        "cli_version": s.version,
        "permission_mode": s.permission_mode,
        "started": s.started.isoformat() if s.started else None,
        "ended": s.ended.isoformat() if s.ended else None,
        "duration_s": s.duration_s,
        "active_s": s.active_seconds,
        "user_turns": s.user_turns,
        "api_calls": s.api_calls,
        "agents": len(s.agents),
        "tokens": s.total_usage.total,
        "cost": round(s.total_cost, 6),
        "transcript": s.path,
    }


def _stats(prompts: List[dict]) -> dict:
    stamped = [p for p in prompts if p["ts"]]
    return {
        "prompts": len(prompts),
        "characters": sum(p["chars"] for p in prompts),
        "words": sum(p["words"] for p in prompts),
        "longest_chars": max((p["chars"] for p in prompts), default=0),
        "first": stamped[0]["ts"] if stamped else None,
        "last": stamped[-1]["ts"] if stamped else None,
    }


def build_json(s: Session, prompts: List[dict]) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "claude-code-monitor",
        "session": _session_meta(s),
        "stats": _stats(prompts),
        "prompts": prompts,
    }


def filename(s: Session, ext: str) -> str:
    """A download name that stays readable and cannot break the header."""
    stem = f"prompts-{s.project}-{s.session_id[:8]}"
    if s.started:
        stem = f"prompts-{s.started.astimezone():%Y%m%d}-{s.project}-{s.session_id[:8]}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "prompts"
    return f"{safe[:120]}.{ext}"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{
  color-scheme:light;
  --surface:#fcfcfb; --panel:#ffffff; --sunk:#eeeeec; --rule:#e2e2df;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#87867f;
  --accent:#2a78d6; --accent-soft:rgba(42,120,214,.10);
  --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 26px -14px rgba(0,0,0,.16);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI","Liberation Sans",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --surface:#1a1a19; --panel:#232322; --sunk:#2e2e2c; --rule:#3a3a37;
    --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e85;
    --accent:#5a9bec; --accent-soft:rgba(90,155,236,.13);
    --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 34px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface:#1a1a19; --panel:#232322; --sunk:#2e2e2c; --rule:#3a3a37;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8f8e85;
  --accent:#5a9bec; --accent-soft:rgba(90,155,236,.13);
  --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 34px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);font-family:var(--sans);
     line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:0 24px 96px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.eyebrow{font-family:var(--mono);font-size:.66rem;font-weight:600;letter-spacing:.16em;
         text-transform:uppercase;color:var(--ink-3)}

/* ── sticky bar ────────────────────────────────────────────────────── */
.bar{position:sticky;top:0;z-index:20;background:var(--surface);
     background:color-mix(in srgb,var(--surface) 88%,transparent);
     backdrop-filter:blur(10px);border-bottom:1px solid var(--rule)}
.bar .in{max-width:940px;margin:0 auto;padding:11px 24px;display:flex;align-items:center;
         gap:12px;flex-wrap:wrap}
.bar .who{font-weight:650;font-size:.86rem;letter-spacing:-.01em;margin-right:auto;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:46%}
.bar .who span{color:var(--ink-3);font-weight:400}
input[type=search]{font-family:var(--sans);font-size:.82rem;padding:7px 11px;flex:1 1 190px;
  min-width:150px;max-width:280px;border:1px solid var(--rule);border-radius:9px;
  background:var(--panel);color:var(--ink)}
input[type=search]:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button.b{font-family:var(--sans);font-size:.78rem;font-weight:600;padding:7px 12px;cursor:pointer;
  border:1px solid var(--rule);border-radius:9px;background:var(--panel);color:var(--ink);
  transition:background .12s}
button.b:hover{background:var(--sunk)}
button.b:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
#hits{font-family:var(--mono);font-size:.7rem;color:var(--ink-3);white-space:nowrap}

/* ── header ────────────────────────────────────────────────────────── */
header{padding:52px 0 0}
h1{font-size:clamp(1.6rem,3.6vw,2.3rem);letter-spacing:-.03em;margin:8px 0 8px;line-height:1.15;
   overflow-wrap:anywhere}
.sub{color:var(--ink-3);font-size:.88rem;margin:0;overflow-wrap:anywhere}
.sub b{color:var(--ink-2);font-weight:500}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:28px}
.tile{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:15px 17px;
      box-shadow:var(--shadow)}
.tile .v{display:block;font-family:var(--mono);font-variant-numeric:tabular-nums;
         font-size:1.42rem;font-weight:600;letter-spacing:-.03em;line-height:1.1}
.tile .k{display:block;margin-top:7px}
.tile.lead .v{color:var(--accent)}

/* ── prompt list ───────────────────────────────────────────────────── */
.list{margin-top:34px;display:flex;flex-direction:column}
.gap{display:flex;align-items:center;gap:10px;padding:9px 0 9px 46px;font-family:var(--mono);
     font-size:.68rem;color:var(--ink-3)}
.gap::before{content:"";width:1px;height:20px;background:var(--rule);margin-left:-30px;
             margin-right:20px;flex:none}
.p{position:relative;background:var(--panel);border:1px solid var(--rule);border-radius:10px;
   box-shadow:var(--shadow);scroll-margin-top:72px}
.p[hidden]{display:none}
.ph{display:flex;align-items:center;gap:10px;padding:11px 15px 11px 14px;
    border-bottom:1px solid var(--rule);flex-wrap:wrap}
.n{flex:none;width:30px;height:30px;border-radius:8px;background:var(--sunk);color:var(--ink-3);
   font-family:var(--mono);font-size:.74rem;font-weight:600;display:grid;place-items:center}
.when{font-family:var(--mono);font-size:.72rem;color:var(--ink-2)}
.chip{font-family:var(--mono);font-size:.63rem;letter-spacing:.06em;text-transform:uppercase;
      color:var(--ink-3);border:1px solid var(--rule);border-radius:99px;padding:2px 8px}
.size{margin-left:auto;font-family:var(--mono);font-size:.68rem;color:var(--ink-3);
      white-space:nowrap}
.copy{flex:none;font-family:var(--mono);font-size:.66rem;letter-spacing:.04em;text-transform:uppercase;
      padding:4px 9px;border:1px solid var(--rule);border-radius:99px;background:transparent;
      color:var(--ink-3);cursor:pointer;transition:.12s}
.copy:hover{background:var(--sunk);color:var(--ink)}
.copy.ok{color:var(--accent);border-color:var(--accent)}
.pb{padding:15px 18px 17px;position:relative}
/* A handful of prompts run to tens of thousands of characters; clamped so the
   page stays skimmable, and always opened for search hits and for print. */
.pb.clamp{max-height:360px;overflow:hidden}
.pb.clamp::after{content:"";position:absolute;left:0;right:0;bottom:0;height:88px;
  background:linear-gradient(to bottom,transparent,var(--panel));pointer-events:none}
.more{display:block;width:100%;padding:9px;border:0;border-top:1px solid var(--rule);
  border-radius:0 0 10px 10px;background:transparent;color:var(--ink-3);cursor:pointer;
  font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;
  transition:.12s}
.more:hover{background:var(--sunk);color:var(--ink)}
.pp{margin:0;font-size:.93rem;color:var(--ink);white-space:pre-wrap;overflow-wrap:anywhere}
.pp+.pp,.pp+pre,pre+.pp,pre+pre{margin-top:12px}
pre{margin:0;background:var(--sunk);border:1px solid var(--rule);border-radius:7px;
    padding:12px 14px;overflow-x:auto}
pre code{font-family:var(--mono);font-size:.79rem;line-height:1.55;white-space:pre}
pre .lang{display:block;font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;
          text-transform:uppercase;color:var(--ink-3);margin-bottom:7px}
mark{background:var(--accent-soft);color:inherit;border-radius:2px;
     box-shadow:inset 0 -1px 0 var(--accent)}
.empty{padding:44px;text-align:center;color:var(--ink-3);font-size:.88rem;
       border:1px dashed var(--rule);border-radius:10px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--rule);
       color:var(--ink-3);font-size:.76rem;line-height:1.65}
footer code{font-family:var(--mono);background:var(--sunk);padding:1px 5px;border-radius:3px}

@media (max-width:620px){
  .bar .who{max-width:100%;margin-right:0;flex:1 0 100%}
  .gap{padding-left:24px}
  .gap::before{margin-left:-14px;margin-right:10px}
}
@media print{
  .bar,.copy,.more{display:none}
  body{background:#fff}
  .p{break-inside:avoid;box-shadow:none}
  .pb.clamp{max-height:none}
  .pb.clamp::after{display:none}
  .wrap{max-width:none;padding:0}
}
"""

JS = """
(function(){
  var root=document.documentElement, list=document.getElementById('list');
  var items=[].slice.call(list.querySelectorAll('.p'));
  var gaps=[].slice.call(list.querySelectorAll('.gap'));
  var q=document.getElementById('q'), hits=document.getElementById('hits');
  var total=items.length;

  document.getElementById('theme').addEventListener('click',function(){
    var dark=getComputedStyle(root).getPropertyValue('color-scheme').trim()==='dark';
    root.setAttribute('data-theme',dark?'light':'dark');
  });

  function clear(el){
    var marks=el.querySelectorAll('mark');
    for(var i=0;i<marks.length;i++){
      var m=marks[i];
      m.parentNode.replaceChild(document.createTextNode(m.textContent),m);
    }
    el.normalize();
  }
  function mark(el,needle){
    var walk=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null,false),nodes=[],n;
    while((n=walk.nextNode()))nodes.push(n);
    for(var i=0;i<nodes.length;i++){
      var node=nodes[i], low=node.nodeValue.toLowerCase(), at=low.indexOf(needle);
      if(at<0)continue;
      var frag=document.createDocumentFragment(), text=node.nodeValue, cur=0;
      while(at>=0){
        if(at>cur)frag.appendChild(document.createTextNode(text.slice(cur,at)));
        var m=document.createElement('mark');
        m.textContent=text.slice(at,at+needle.length);
        frag.appendChild(m);
        cur=at+needle.length;
        at=low.indexOf(needle,cur);
      }
      if(cur<text.length)frag.appendChild(document.createTextNode(text.slice(cur)));
      node.parentNode.replaceChild(frag,node);
    }
  }

  list.addEventListener('click',function(e){
    var btn=e.target.closest('.more'); if(!btn)return;
    var body=btn.previousElementSibling, open=body.classList.toggle('clamp');
    btn.textContent=open?btn.dataset.more:'show less';
  });

  var timer;
  function filter(){
    var needle=q.value.trim().toLowerCase(), shown=0;
    for(var i=0;i<items.length;i++){
      var body=items[i].querySelector('.pb');
      clear(body);
      var hit=!needle||body.textContent.toLowerCase().indexOf(needle)>=0;
      items[i].hidden=!hit;
      // A hit hidden inside a clamped body would look like a false positive.
      if(needle&&hit&&body.classList.contains('clamp')){
        body.classList.remove('clamp');
        var b=body.nextElementSibling;
        if(b&&b.classList.contains('more'))b.textContent='show less';
      }
      if(hit){shown++; if(needle)mark(body,needle);}
    }
    for(var g=0;g<gaps.length;g++)gaps[g].hidden=!!needle;
    hits.textContent=needle?shown+' of '+total:total+(total===1?' prompt':' prompts');
    document.getElementById('none').hidden=shown>0;
  }
  q.addEventListener('input',function(){clearTimeout(timer);timer=setTimeout(filter,120);});
  q.addEventListener('keydown',function(e){if(e.key==='Escape'){q.value='';filter();}});
  addEventListener('keydown',function(e){
    if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}
  });

  function put(text,btn,label){
    var done=function(){
      var old=btn.textContent;
      btn.textContent=label; btn.classList.add('ok');
      setTimeout(function(){btn.textContent=old;btn.classList.remove('ok');},1400);
    };
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(done,function(){fallback(text,done);});
    }else fallback(text,done);
  }
  function fallback(text,done){
    var ta=document.createElement('textarea');
    ta.value=text; ta.setAttribute('readonly','');
    ta.style.cssText='position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta); ta.select();
    try{document.execCommand('copy');done();}catch(e){}
    document.body.removeChild(ta);
  }
  list.addEventListener('click',function(e){
    var btn=e.target.closest('.copy'); if(!btn)return;
    put(btn.closest('.p').querySelector('.pb').innerText,btn,'copied');
  });
  document.getElementById('copyall').addEventListener('click',function(){
    var out=[];
    for(var i=0;i<items.length;i++){
      if(items[i].hidden)continue;
      var h=items[i].querySelector('.when'), n=items[i].querySelector('.n');
      out.push('## '+(n?n.textContent:i+1)+(h?' · '+h.textContent:'')+'\\n\\n'
               +items[i].querySelector('.pb').innerText);
    }
    put(out.join('\\n\\n---\\n\\n'),this,'copied '+out.length);
  });

  filter();
})();
"""


def _render_body(text: str) -> str:
    """Prompt text as HTML: fenced blocks become ``<pre>``, prose keeps its
    line breaks. Everything is escaped; nothing in a prompt can become markup."""
    parts = text.split("```")
    out: List[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            body = part.strip("\n")
            if body.strip():
                out.append(f'<p class="pp">{esc(body)}</p>')
            continue
        lines = part.split("\n")
        lang = ""
        # A bare word on the fence line is a language tag, not code.
        if len(lines) > 1 and lines[0].strip() and " " not in lines[0].strip():
            lang, lines = lines[0].strip(), lines[1:]
        code = "\n".join(lines).strip("\n")
        if not code.strip():
            continue
        tag = f'<span class="lang">{esc(lang)}</span>' if lang else ""
        out.append(f"<pre>{tag}<code>{esc(code)}</code></pre>")
    return "".join(out) or '<p class="pp"></p>'


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _local(iso: Optional[str], *, secs: bool = False) -> str:
    """``Mar 4, 2026 · 14:07`` in the exporting machine's local time.

    Built by hand rather than with strftime because the no-pad directives
    (``%-d`` / ``%#d``) differ between platforms.
    """
    dt = _utc(iso)
    if dt is None:
        return "—"
    dt = dt.astimezone()
    clock = f"{dt.hour:02d}:{dt.minute:02d}" + (f":{dt.second:02d}" if secs else "")
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year} · {clock}"


def build_html(s: Session, prompts: List[dict]) -> str:
    meta = _session_meta(s)
    st = _stats(prompts)
    generated = datetime.now().astimezone()

    tiles = [
        (f"{st['prompts']:,}", "Prompts", True),
        (f"{st['words']:,}", "Words typed", False),
        (fmt_duration(meta["duration_s"]), "Session span", False),
        (pricing.fmt_tokens(meta["tokens"]), "Tokens", False),
        (pricing.fmt_usd(meta["cost"]), "Cost", False),
    ]
    line2 = " · ".join(
        esc(x) for x in [
            meta["project"], meta["branch"] or "no branch", meta["model_label"],
            f"CLI {meta['cli_version'] or '?'}", f"session {meta['short']}",
        ] if x
    )

    rows: List[str] = []
    for p in prompts:
        if p["gap_s"] and p["gap_s"] >= 60:
            rows.append(
                f'<div class="gap">{esc(fmt_duration(p["gap_s"]))} later</div>'
            )
        chip = ""
        if p["source"] and p["source"] != "typed":
            chip = f'<span class="chip">{esc(p["source"])}</span>'
        long = p["chars"] > CLAMP_CHARS or p["lines"] > CLAMP_LINES
        more = (
            f'<button type="button" class="more" data-more="show all '
            f'{p["lines"]:,} lines">show all {p["lines"]:,} lines</button>'
        ) if long else ""
        rows.append(
            f'<article class="p" id="p{p["n"]}">'
            f'<div class="ph"><span class="n">{p["n"]}</span>'
            f'<span class="when">{esc(_local(p["ts"], secs=True))}</span>'
            f'{chip}'
            f'<span class="size">{p["words"]:,} words · {p["chars"]:,} chars</span>'
            f'<button type="button" class="copy">copy</button></div>'
            f'<div class="pb{" clamp" if long else ""}">'
            f'{_render_body(p["text"])}</div>{more}</article>'
        )

    body = f"""
<div class="bar"><div class="in">
  <div class="who">{esc(meta['title'])} <span>· {esc(meta['project'])}</span></div>
  <input type="search" id="q" placeholder="Filter prompts…  ( / )"
         aria-label="Filter prompts">
  <span id="hits" aria-live="polite"></span>
  <button type="button" class="b" id="copyall">Copy all</button>
  <button type="button" class="b" id="theme" aria-label="Toggle theme">◐</button>
</div></div>

<div class="wrap">
<header>
  <p class="eyebrow">Session prompts</p>
  <h1>{esc(meta['title'])}</h1>
  <p class="sub">{line2}</p>
  <p class="sub" style="margin-top:4px">
    <b>{esc(_local(meta['started']))}</b> →
    <b>{esc(_local(meta['ended']))}</b>
    · {meta['api_calls']:,} API calls · {meta['agents']:,} subagents</p>
  <div class="tiles">
    {''.join(
        f'<div class="tile{" lead" if lead else ""}"><span class="v">{esc(v)}</span>'
        f'<span class="k eyebrow">{esc(k)}</span></div>'
        for v, k, lead in tiles
    )}
  </div>
</header>

<main class="list" id="list">{''.join(rows)}</main>
<p class="empty" id="none"{' hidden' if prompts else ''}>{
  'No prompt matches that filter.' if prompts
  else 'This session recorded no prompts of its own — it may have been resumed '
       'from another session, or driven entirely by a hook or scheduled run.'
}</p>

<footer>
  <p>Every prompt you typed in this session, in order, exactly as sent — harness
  injections (system reminders, tool results, command wrappers) are excluded, and
  replayed records from session resumes are counted once.
  Working directory <code>{esc(meta['cwd'] or '—')}</code>.</p>
  <p>Exported by Claude Code Monitor on
  {esc(generated.strftime('%Y-%m-%d %H:%M:%S %Z'))} ·
  {st['characters']:,} characters across {st['prompts']:,} prompts.</p>
</footer>
</div>
"""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Prompts · {esc(meta['title'])}</title>"
        f"<style>{CSS}</style></head><body>{body}"
        f"<script>{JS}</script></body></html>"
    )


def build(s: Session, fmt: str) -> tuple:
    """``(body, mimetype, filename)`` for the requested export format."""
    prompts = extract_prompts(Path(s.path))
    if fmt == "html":
        return (build_html(s, prompts), "text/html; charset=utf-8",
                filename(s, "html"))
    payload = build_json(s, prompts)
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            "application/json; charset=utf-8", filename(s, "json"))
