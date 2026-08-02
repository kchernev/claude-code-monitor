"""Incremental parser for Claude Code transcripts.

Transcripts live under ``~/.claude/projects/<encoded-cwd>/`` as newline-delimited
JSON:

    <encoded-cwd>/<session-id>.jsonl                     main thread
    <encoded-cwd>/<session-id>/subagents/agent-*.jsonl   one per subagent

The full corpus is easily gigabytes, so parsed results are cached on disk keyed
by (path, size, mtime). Only files that changed since the last run are re-read.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .models import AgentRun, ApiCall, ModelStat, Session, Usage, _utc

CACHE_VERSION = 8

# Unique-per-record fallback identity. Never reuse ``id(rec)`` here: CPython
# recycles addresses as records are garbage-collected between iterations, so
# two unrelated records can share one, and the second would be silently
# dropped as a duplicate. A counter can only ever err towards counting a
# record, which is the safe direction.
_FALLBACK_SEQ = itertools.count()


# ---------------------------------------------------------------------------
# Low-level record iteration
# ---------------------------------------------------------------------------


def iter_records(path: Path, start_offset: int = 0):
    """Yield ``(record, end_offset)`` for each JSON line from ``start_offset``.

    Malformed lines are skipped — a transcript being appended to concurrently
    can end in a partial line, which is normal rather than an error.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            for raw in fh:
                offset = fh.tell()
                if not raw.strip():
                    continue
                try:
                    yield json.loads(raw), offset
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except OSError:
        return


# ---------------------------------------------------------------------------
# Shared record handling
# ---------------------------------------------------------------------------


def billing_key(rec: dict) -> tuple:
    """Identity of an API call for deduplication.

    When a session or agent resumes, Claude Code replays earlier messages into
    the transcript, so the same billed call can appear many times — in one
    observed session, 7,229 raw assistant records collapsed to 4,009 distinct
    ones and naive summing inflated output tokens 4.8x. ``message.id`` paired
    with ``requestId`` identifies the actual API request; the record ``uuid``
    is the fallback when either is missing.
    """
    msg = rec.get("message") or {}
    mid = msg.get("id")
    rid = rec.get("requestId")
    if mid and rid:
        return ("call", mid, rid)
    if mid:
        return ("msg", mid)
    uid = rec.get("uuid")
    if uid:
        return ("uuid", uid)
    return ("seq", next(_FALLBACK_SEQ))


def _apply_assistant(
    rec: dict,
    sink,
    timeline: Optional[List] = None,
    seen: Optional[set] = None,
) -> Optional[ApiCall]:
    """Fold one assistant record into ``sink`` (a Session or AgentRun).

    ``seen`` carries deduplication state across the whole session — main
    thread plus every subagent — so a replayed call is billed exactly once.
    """
    msg = rec.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    raw_usage = msg.get("usage")
    if not isinstance(raw_usage, dict):
        return None

    if seen is not None:
        key = billing_key(rec)
        if key in seen:
            return None
        seen.add(key)

    model = msg.get("model") or ""
    usage = Usage.from_api(raw_usage)
    fast = raw_usage.get("speed") == "fast"
    ts = _utc(rec.get("timestamp"))

    tools: List[str] = []
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or "?"
                tools.append(name)
                sink.tool_counts[name] = sink.tool_counts.get(name, 0) + 1

    call = ApiCall(
        timestamp=ts,
        model=model,
        usage=usage,
        fast=fast,
        stop_reason=msg.get("stop_reason"),
        tools=tools,
        context_tokens=usage.total_input,
    )

    cost = call.cost
    uncached = call.uncached_cost

    sink.usage.add(usage)
    sink.cost += cost
    sink.uncached_cost += uncached
    sink.api_calls += 1

    # Exact per-model attribution — never apportion a session's cost across
    # models by call count, since tiers differ by up to 10x.
    stat = sink.per_model.get(model)
    if stat is None:
        stat = ModelStat()
        sink.per_model[model] = stat
    stat.calls += 1
    stat.usage.add(usage)
    stat.cost += cost
    stat.uncached_cost += uncached

    if isinstance(getattr(sink, "models", None), dict) and model:
        sink.models[model] = sink.models.get(model, 0) + 1
    elif hasattr(sink, "model") and model and not sink.model:
        sink.model = model

    if timeline is not None and ts is not None:
        timeline.append(
            (ts.timestamp(), usage.output_tokens, usage.total_input, call.cost)
        )
    return call


def _count_tool_errors(rec: dict) -> int:
    """Count ``tool_result`` blocks flagged as errors in a user record."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
    )


# Text the harness injects into the user turn, which is not something a
# person typed and should not appear when browsing a session's prompts.
_INJECTED_PREFIXES = (
    "<task-notification>", "<system-reminder>", "[Image:", "<local-command",
    "<command-name>", "Caveat: The messages below",
)


def _is_human_turn(rec: dict) -> bool:
    """True for a genuine user prompt, as opposed to a tool-result carrier."""
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return False
    origin = (rec.get("origin") or {}).get("kind")
    if origin and origin != "human":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith(_INJECTED_PREFIXES)
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _prompt_text(rec: dict) -> str:
    """Flatten a user record's content to plain text."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# ---------------------------------------------------------------------------
# Subagent transcripts
# ---------------------------------------------------------------------------


def _read_agent_meta(path: Path) -> dict:
    """Read the ``agent-<id>.meta.json`` sidecar written next to a transcript.

    Claude Code records the agent's type, description and spawn depth here,
    which is more reliable than inferring them from the parent's tool call.
    """
    meta_path = path.with_suffix("").with_suffix(".meta.json")
    if not meta_path.exists():
        meta_path = path.parent / (path.stem + ".meta.json")
    try:
        with open(meta_path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_workflow_journal(wf_dir: Path) -> Dict[str, str]:
    """Map ``agentId -> result text`` from a workflow's ``journal.jsonl``."""
    results: Dict[str, str] = {}
    journal = wf_dir / "journal.jsonl"
    if not journal.exists():
        return results
    for rec, _ in iter_records(journal):
        if rec.get("type") == "result" and rec.get("agentId"):
            value = rec.get("result")
            if isinstance(value, str):
                results[rec["agentId"]] = value[:2000]
            elif value is not None:
                results[rec["agentId"]] = json.dumps(value)[:2000]
    return results


def parse_agent_file(
    path: Path, session_id: str, seen: Optional[set] = None
) -> AgentRun:
    """Parse one ``subagents/**/agent-*.jsonl`` transcript."""
    # Strip only the leading marker: an id that itself contains "agent-" must
    # survive intact, or it stops matching the same file elsewhere.
    agent_id = path.stem.replace("agent-", "", 1)
    run = AgentRun(agent_id=agent_id, session_id=session_id)

    meta = _read_agent_meta(path)
    run.subagent_type = meta.get("agentType") or ""
    run.description = meta.get("description") or ""
    run.spawn_depth = int(meta.get("spawnDepth") or 1)

    # Workflow-spawned agents live under subagents/workflows/wf_<id>/.
    if path.parent.name.startswith("wf_"):
        run.workflow_id = path.parent.name

    for rec, _ in iter_records(path):
        rtype = rec.get("type")
        ts = _utc(rec.get("timestamp"))
        if ts:
            if run.started is None or ts < run.started:
                run.started = ts
            if run.ended is None or ts > run.ended:
                run.ended = ts

        if rtype == "assistant":
            call = _apply_assistant(rec, run, seen=seen)
            if call and call.stop_reason == "end_turn":
                run.completed = True
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            run.final_message = (block.get("text") or "")[:2000]
        elif rtype == "user":
            if seen is not None:
                ukey = ("u", rec.get("uuid"))
                if rec.get("uuid") and ukey in seen:
                    continue
                if rec.get("uuid"):
                    seen.add(ukey)
            run.tool_errors += _count_tool_errors(rec)
            # The first user record is the orchestrator's prompt to the agent.
            if not run.prompt:
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    run.prompt = content[:4000]

    return run


# ---------------------------------------------------------------------------
# Main session transcripts
# ---------------------------------------------------------------------------


def parse_session_file(path: Path) -> Session:
    """Parse a session transcript and every subagent transcript beneath it."""
    session_id = path.stem
    project_dir = path.parent.name
    sess = Session(session_id=session_id, path=str(path), project_dir=project_dir)

    # Shared across the main thread and every subagent: a replayed call must
    # be counted once for the whole session, not once per file it appears in.
    seen: set = set()

    # agentId -> metadata harvested from Agent tool calls in the main thread.
    agent_meta: Dict[str, dict] = {}
    # toolUseId -> Agent tool input, resolved to an agentId by the tool result.
    pending_agent_calls: Dict[str, dict] = {}

    for rec, _ in iter_records(path):
        rtype = rec.get("type")
        ts = _utc(rec.get("timestamp"))
        if ts:
            if sess.started is None or ts < sess.started:
                sess.started = ts
            if sess.ended is None or ts > sess.ended:
                sess.ended = ts

        if not sess.cwd and rec.get("cwd"):
            sess.cwd = rec["cwd"]
        if rec.get("gitBranch"):
            sess.git_branch = rec["gitBranch"]
        if rec.get("version"):
            sess.version = rec["version"]

        if rtype == "assistant":
            call = _apply_assistant(rec, sess, sess.timeline, seen=seen)
            if call:
                sess.peak_context = max(sess.peak_context, call.context_tokens)
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == "Agent"
                        ):
                            pending_agent_calls[block.get("id") or ""] = (
                                block.get("input") or {}
                            )

        elif rtype == "user":
            ukey = ("u", rec.get("uuid"))
            if rec.get("uuid") and ukey in seen:
                continue
            if rec.get("uuid"):
                seen.add(ukey)
            if _is_human_turn(rec):
                sess.user_turns += 1
                text = _prompt_text(rec).strip()
                if text and len(sess.prompts) < 200:
                    sess.prompts.append({
                        "ts": rec.get("timestamp"),
                        "text": text[:2000],
                        "source": rec.get("promptSource") or "typed",
                    })
            sess.tool_errors += _count_tool_errors(rec)

            # An Agent tool result carries the agentId that links the main
            # thread to the subagent transcript on disk.
            result = rec.get("toolUseResult")
            if isinstance(result, dict) and result.get("agentId"):
                aid = result["agentId"]
                agent_meta[aid] = {
                    "description": result.get("description") or "",
                    "prompt": (result.get("prompt") or "")[:4000],
                    "model": result.get("resolvedModel") or "",
                }
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            src = pending_agent_calls.pop(block.get("tool_use_id") or "", None)
                            if src:
                                agent_meta[aid].setdefault(
                                    "subagent_type", src.get("subagent_type") or ""
                                )

        elif rtype == "file-history-delta":
            tracked = rec.get("trackingPath")
            if tracked:
                sess.files_touched[tracked] = sess.files_touched.get(tracked, 0) + 1
        elif rtype == "ai-title":
            sess.title = rec.get("aiTitle") or sess.title
        elif rtype == "last-prompt":
            sess.last_prompt = rec.get("lastPrompt") or sess.last_prompt
        elif rtype == "permission-mode":
            sess.permission_mode = rec.get("permissionMode") or sess.permission_mode
        elif rtype == "system" and rec.get("subtype") == "turn_duration":
            skey = ("d", rec.get("uuid"))
            if rec.get("uuid") and skey in seen:
                continue
            if rec.get("uuid"):
                seen.add(skey)
            sess.active_seconds += (rec.get("durationMs") or 0) / 1000.0

    # Any Agent tool inputs we never matched to a result still describe work
    # that was launched, so fold them in by description.
    unmatched = list(pending_agent_calls.values())

    agent_dir = path.parent / session_id / "subagents"
    if agent_dir.is_dir():
        # rglob picks up both direct subagents and Workflow-spawned agents
        # nested under subagents/workflows/wf_<id>/.
        journals: Dict[Path, Dict[str, str]] = {}
        for agent_path in sorted(agent_dir.rglob("agent-*.jsonl")):
            run = parse_agent_file(agent_path, session_id, seen=seen)

            # The .meta.json sidecar is authoritative; fall back to whatever
            # the parent thread's Agent tool call recorded.
            meta = agent_meta.get(run.agent_id, {})
            if not run.description:
                run.description = meta.get("description", "")
            if not run.subagent_type:
                run.subagent_type = meta.get("subagent_type", "")
            if meta.get("prompt") and not run.prompt:
                run.prompt = meta["prompt"]
            if meta.get("model") and not run.model:
                run.model = meta["model"]

            if run.workflow_id:
                wf_dir = agent_path.parent
                if wf_dir not in journals:
                    journals[wf_dir] = _read_workflow_journal(wf_dir)
                run.result = journals[wf_dir].get(run.agent_id, "")
                if run.result and not run.completed:
                    run.completed = True

            sess.agents.append(run)

    for src in unmatched:
        if not any(a.description == src.get("description") for a in sess.agents):
            sess.agents.append(
                AgentRun(
                    agent_id="(pending)",
                    session_id=session_id,
                    description=src.get("description") or "",
                    subagent_type=src.get("subagent_type") or "",
                    prompt=(src.get("prompt") or "")[:4000],
                )
            )

    # Flag descriptions reused across agents — the observable symptom of a
    # stale label surviving a resume.
    label_counts: Dict[str, int] = {}
    for a in sess.agents:
        if a.description:
            label_counts[a.description] = label_counts.get(a.description, 0) + 1
    for a in sess.agents:
        if a.description and label_counts[a.description] > 1:
            a.label_ambiguous = True

    sess.timeline.sort(key=lambda p: p[0])
    return sess


# ---------------------------------------------------------------------------
# Serialisation for the on-disk cache
# ---------------------------------------------------------------------------


def _usage_to_dict(u: Usage) -> dict:
    return {
        "i": u.input_tokens,
        "o": u.output_tokens,
        "w5": u.cache_write_5m,
        "w1": u.cache_write_1h,
        "r": u.cache_read,
        "ws": u.web_search_requests,
        "wf": u.web_fetch_requests,
    }


def _usage_from_dict(d: dict) -> Usage:
    return Usage(
        input_tokens=d.get("i", 0),
        output_tokens=d.get("o", 0),
        cache_write_5m=d.get("w5", 0),
        cache_write_1h=d.get("w1", 0),
        cache_read=d.get("r", 0),
        web_search_requests=d.get("ws", 0),
        web_fetch_requests=d.get("wf", 0),
    )


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _per_model_to_dict(pm: Dict[str, ModelStat]) -> dict:
    return {
        k: {"c": v.calls, "u": _usage_to_dict(v.usage),
            "$": v.cost, "x": v.uncached_cost}
        for k, v in pm.items()
    }


def _per_model_from_dict(d: dict) -> Dict[str, ModelStat]:
    return {
        k: ModelStat(
            calls=v.get("c", 0),
            usage=_usage_from_dict(v.get("u", {})),
            cost=v.get("$", 0.0),
            uncached_cost=v.get("x", 0.0),
        )
        for k, v in (d or {}).items()
    }


def session_to_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "path": s.path,
        "project_dir": s.project_dir,
        "cwd": s.cwd,
        "title": s.title,
        "last_prompt": s.last_prompt,
        "git_branch": s.git_branch,
        "version": s.version,
        "started": _iso(s.started),
        "ended": _iso(s.ended),
        "permission_mode": s.permission_mode,
        "usage": _usage_to_dict(s.usage),
        "cost": s.cost,
        "uncached_cost": s.uncached_cost,
        "api_calls": s.api_calls,
        "user_turns": s.user_turns,
        "models": s.models,
        "per_model": _per_model_to_dict(s.per_model),
        "tool_counts": s.tool_counts,
        "tool_errors": s.tool_errors,
        "peak_context": s.peak_context,
        "active_seconds": s.active_seconds,
        "prompts": s.prompts,
        "files_touched": s.files_touched,
        "timeline": s.timeline,
        "agents": [
            {
                "agent_id": a.agent_id,
                "description": a.description,
                "subagent_type": a.subagent_type,
                "prompt": a.prompt,
                "model": a.model,
                "spawn_depth": a.spawn_depth,
                "workflow_id": a.workflow_id,
                "result": a.result,
                "started": _iso(a.started),
                "ended": _iso(a.ended),
                "usage": _usage_to_dict(a.usage),
                "cost": a.cost,
                "uncached_cost": a.uncached_cost,
                "api_calls": a.api_calls,
                "tool_counts": a.tool_counts,
                "tool_errors": a.tool_errors,
                "per_model": _per_model_to_dict(a.per_model),
                "completed": a.completed,
                "label_ambiguous": a.label_ambiguous,
                "final_message": a.final_message,
            }
            for a in s.agents
        ],
    }


def session_from_dict(d: dict) -> Session:
    s = Session(
        session_id=d["session_id"],
        path=d["path"],
        project_dir=d["project_dir"],
        cwd=d.get("cwd", ""),
        title=d.get("title", ""),
        last_prompt=d.get("last_prompt", ""),
        git_branch=d.get("git_branch", ""),
        version=d.get("version", ""),
        started=_utc(d.get("started")),
        ended=_utc(d.get("ended")),
        permission_mode=d.get("permission_mode", ""),
        usage=_usage_from_dict(d.get("usage", {})),
        cost=d.get("cost", 0.0),
        uncached_cost=d.get("uncached_cost", 0.0),
        api_calls=d.get("api_calls", 0),
        user_turns=d.get("user_turns", 0),
        models=d.get("models", {}),
        per_model=_per_model_from_dict(d.get("per_model")),
        tool_counts=d.get("tool_counts", {}),
        tool_errors=d.get("tool_errors", 0),
        peak_context=d.get("peak_context", 0),
        active_seconds=d.get("active_seconds", 0.0),
        prompts=d.get("prompts", []),
        files_touched=d.get("files_touched", {}),
        timeline=[tuple(p) for p in d.get("timeline", [])],
    )
    for a in d.get("agents", []):
        s.agents.append(
            AgentRun(
                agent_id=a["agent_id"],
                session_id=s.session_id,
                description=a.get("description", ""),
                subagent_type=a.get("subagent_type", ""),
                prompt=a.get("prompt", ""),
                model=a.get("model", ""),
                spawn_depth=a.get("spawn_depth", 1),
                workflow_id=a.get("workflow_id", ""),
                result=a.get("result", ""),
                started=_utc(a.get("started")),
                ended=_utc(a.get("ended")),
                usage=_usage_from_dict(a.get("usage", {})),
                cost=a.get("cost", 0.0),
                uncached_cost=a.get("uncached_cost", 0.0),
                api_calls=a.get("api_calls", 0),
                tool_counts=a.get("tool_counts", {}),
                tool_errors=a.get("tool_errors", 0),
                per_model=_per_model_from_dict(a.get("per_model")),
                completed=a.get("completed", False),
                label_ambiguous=a.get("label_ambiguous", False),
                final_message=a.get("final_message", ""),
            )
        )
    return s


# ---------------------------------------------------------------------------
# Corpus loading with caching
# ---------------------------------------------------------------------------


def _fingerprint(path: Path) -> str:
    """Cheap change detector covering the file and its subagent directory."""
    try:
        st = path.stat()
    except OSError:
        return "0:0"
    agent_dir = path.parent / path.stem / "subagents"
    agent_sig = 0
    if agent_dir.is_dir():
        try:
            # Recurse: Workflow-spawned agents sit under workflows/wf_<id>/.
            for p in agent_dir.rglob("*.jsonl"):
                ps = p.stat()
                agent_sig += ps.st_size + int(ps.st_mtime)
        except OSError:
            pass
    return f"{st.st_size}:{int(st.st_mtime)}:{agent_sig}"


# The cache holds prompt text, working directories and edited file paths, so
# it is written 0600 inside a 0700 directory rather than at whatever the
# ambient umask happens to be.
_CACHE_DIR_MODE = 0o700
_CACHE_FILE_MODE = 0o600
# Live sessions change their transcript constantly and the index is measured
# in megabytes; persisting on every 2-second refresh would mean rewriting the
# whole file ~30 times a minute for a cache that is only an optimisation.
_SAVE_MIN_INTERVAL_S = 30.0


class Corpus:
    """Loads and caches every session transcript on disk.

    Not owned by one thread: the web server refreshes on a background thread
    while a reindex request re-parses on the request thread, so the index dict
    and the file it is written to are guarded by a reentrant lock. Without it,
    serialising the index while another thread inserts into it raises
    "dictionary changed size during iteration" and loses the save.
    """

    def __init__(self, claude_dir: Optional[Path] = None, cache_dir: Optional[Path] = None):
        self.claude_dir = Path(
            claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        )
        self.projects_dir = self.claude_dir / "projects"
        self.cache_dir = Path(
            cache_dir or Path.home() / ".cache" / "claude-monitor"
        )
        self.cache_path = self.cache_dir / f"index-v{CACHE_VERSION}.json"
        self._cache: Dict[str, dict] = {}
        self._last_save = 0.0
        self._unsaved = False
        self._lock = threading.RLock()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path) as fh:
                blob = json.load(fh)
            if blob.get("version") == CACHE_VERSION:
                self._cache = blob.get("entries", {})
        except (OSError, json.JSONDecodeError, ValueError):
            self._cache = {}

    def _prune_old_versions(self) -> None:
        """Delete index files written by earlier cache formats."""
        try:
            for old in self.cache_dir.glob("index-v*.json"):
                if old != self.cache_path:
                    old.unlink()
        except OSError:
            pass

    def save_cache(self, *, force: bool = True) -> None:
        """Persist the index. ``force=False`` skips a save made too recently."""
        with self._lock:
            self._unsaved = True
            now = time.monotonic()
            if not force and now - self._last_save < _SAVE_MIN_INTERVAL_S:
                return
            # Unique temp name: two cmon processes sharing one cache directory
            # must not write the same scratch file at the same time.
            tmp = self.cache_path.with_name(
                f"{self.cache_path.name}.{os.getpid()}.tmp"
            )
            # Serialise a snapshot, not the live dict: entries are whole-value
            # replacements, so a shallow copy is cheap and means a write can
            # never fail halfway with "dictionary changed size during iteration".
            snapshot = dict(self._cache)
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.cache_dir, _CACHE_DIR_MODE)
                except OSError:
                    pass
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             _CACHE_FILE_MODE)
                with os.fdopen(fd, "w") as fh:
                    json.dump({"version": CACHE_VERSION, "entries": snapshot}, fh)
                os.replace(tmp, self.cache_path)
                self._last_save = now
                self._unsaved = False
                self._prune_old_versions()
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def session_paths(self) -> List[Path]:
        if not self.projects_dir.is_dir():
            return []
        return sorted(self.projects_dir.glob("*/*.jsonl"))

    def load(
        self,
        *,
        progress: Optional[Callable[[int, int, str], None]] = None,
        paths: Optional[Iterable[Path]] = None,
        force: bool = False,
    ) -> List[Session]:
        """Return every parsed session, using the cache where still valid."""
        all_paths = list(paths) if paths is not None else self.session_paths()
        total = len(all_paths)
        sessions: List[Session] = []
        dirty = False

        # Held across the whole parse so a reindex on one thread and the
        # background refresh on another cannot interleave into the index.
        with self._lock:
            for i, path in enumerate(all_paths):
                key = str(path)
                fp = _fingerprint(path)
                entry = self._cache.get(key)
                if not force and entry and entry.get("fp") == fp:
                    try:
                        sessions.append(session_from_dict(entry["data"]))
                        if progress:
                            progress(i + 1, total, path.stem)
                        continue
                    except (KeyError, TypeError, ValueError):
                        pass  # Corrupt entry — fall through and re-parse.

                sess = parse_session_file(path)
                sessions.append(sess)
                self._cache[key] = {"fp": fp, "data": session_to_dict(sess)}
                dirty = True
                if progress:
                    progress(i + 1, total, path.stem)

            # Drop cache entries for transcripts that no longer exist.
            if paths is None:
                live_keys = {str(p) for p in all_paths}
                stale = [k for k in self._cache if k not in live_keys]
                for k in stale:
                    del self._cache[k]
                    dirty = True

            if dirty:
                self.save_cache()
        return sessions

    def refresh(self, sessions: List[Session]) -> tuple:
        """Re-parse only the transcripts that changed since ``sessions`` was built.

        Returns ``(sessions, changed_ids)``. This is what the live dashboard
        polls: it costs one ``stat`` per transcript plus a full parse of only
        the handful of files actually being written to.
        """
        by_path = {s.path: s for s in sessions}
        changed: List[str] = []
        result: List[Session] = []
        dirty = False

        with self._lock:
            for path in self.session_paths():
                key = str(path)
                fp = _fingerprint(path)
                entry = self._cache.get(key)
                existing = by_path.get(key)

                if existing is not None and entry and entry.get("fp") == fp:
                    result.append(existing)
                    continue

                sess = parse_session_file(path)
                self._cache[key] = {"fp": fp, "data": session_to_dict(sess)}
                dirty = True
                changed.append(sess.session_id)
                result.append(sess)

            if dirty:
                # Throttled: a live session makes this path run every couple of
                # seconds, and the index is far too big to rewrite that often.
                self.save_cache(force=False)
        return result, changed
