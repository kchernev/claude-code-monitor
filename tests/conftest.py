"""Shared fixtures and a small builder DSL for synthetic transcripts.

The parser's whole job is to read the newline-delimited JSON Claude Code
writes under ``~/.claude/projects/``, so almost every test needs a transcript
that looks real. Hand-rolling those dicts inline makes tests unreadable and
hides which field is the one under test, so they are built here instead:

    sess = corpus_dir.session("proj")
    sess.assistant(output=100, tools=[tool_use("Bash", {"command": "ls"})])
    sess.user_result("tu1")
    sess.build()

Everything is deterministic — timestamps come from a fixed base clock, never
from ``now()`` — so a failure reproduces exactly.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The project ships as a plain directory with a ./cmon launcher rather than an
# installed package, so tests put the repo root on the path the same way.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Anchored an hour in the past and resolved once per run, so a transcript
# built by these helpers is always "recent" no matter what the calendar says.
# A hard-coded date would drift: every ``?days=N`` window is measured from
# now, so the suite would quietly start excluding its own fixtures. Records
# are placed by second-offsets from here, which keeps each run reproducible.
BASE_TS = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


def ts_at(offset_s: float) -> str:
    """Transcript-style ISO timestamp ``offset_s`` after the base instant."""
    return (
        (BASE_TS + timedelta(seconds=offset_s))
        .isoformat()
        .replace("+00:00", "Z")
    )


class Clock:
    """Hands out increasing timestamps, or an explicit offset on request."""

    def __init__(self, step: float = 1.0):
        self.step = step
        self.cursor = 0.0
        self.earliest = None

    def stamp(self, at=None) -> str:
        if at is None:
            at = self.cursor
        self.cursor = at + self.step
        if self.earliest is None or at < self.earliest:
            self.earliest = at
        return ts_at(at)


def tool_use(name: str, inp=None, tool_id: str = None) -> dict:
    """A ``tool_use`` content block as it appears in an assistant record."""
    return {
        "type": "tool_use",
        "id": tool_id or f"tu_{name.lower()}",
        "name": name,
        "input": inp or {},
    }


def usage_block(input=0, output=0, cache_read=0, write_5m=0, write_1h=0,
                *, flat_write=None, fast=False, web_search=0) -> dict:
    """A ``message.usage`` object.

    ``flat_write`` emits the older ``cache_creation_input_tokens`` shape with
    no 5m/1h split, which the parser has to keep understanding.
    """
    u = {
        "input_tokens": input,
        "output_tokens": output,
        "cache_read_input_tokens": cache_read,
    }
    if flat_write is not None:
        u["cache_creation_input_tokens"] = flat_write
    else:
        u["cache_creation"] = {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        }
    if fast:
        u["speed"] = "fast"
    if web_search:
        u["server_tool_use"] = {"web_search_requests": web_search}
    return u


class _Transcript:
    """Base emitter for the record types shared by sessions and agents."""

    def __init__(self, path: Path, clock: Clock, ids: itertools.count):
        self.path = path
        self.clock = clock
        self._ids = ids
        self.records: list = []

    # -- raw ---------------------------------------------------------------

    def raw(self, rec: dict) -> "_Transcript":
        self.records.append(rec)
        return self

    def _uuid(self) -> str:
        return f"uuid-{next(self._ids)}"

    # -- records -----------------------------------------------------------

    def assistant(self, *, at=None, model="claude-opus-5", stop_reason=None,
                  tools=(), text=None, msg_id=None, request_id=None,
                  uuid=None, usage=None, **usage_kw) -> "_Transcript":
        content = [dict(b) for b in tools]
        if text is not None:
            content.append({"type": "text", "text": text})
        n = next(self._ids)
        rec = {
            "type": "assistant",
            "timestamp": self.clock.stamp(at),
            "uuid": uuid or f"uuid-{n}",
            "requestId": request_id if request_id is not None else f"req_{n}",
            "message": {
                "role": "assistant",
                "id": msg_id if msg_id is not None else f"msg_{n}",
                "model": model,
                "usage": usage if usage is not None else usage_block(**usage_kw),
                "stop_reason": stop_reason,
                "content": content,
            },
        }
        return self.raw(rec)

    def user(self, text="do the thing", *, at=None, uuid=None,
             origin_kind=None, prompt_source=None) -> "_Transcript":
        msg = {"role": "user", "content": text}
        rec = {
            "type": "user",
            "timestamp": self.clock.stamp(at),
            "uuid": uuid or self._uuid(),
            "message": msg,
        }
        if origin_kind:
            rec["origin"] = {"kind": origin_kind}
        if prompt_source:
            rec["promptSource"] = prompt_source
        return self.raw(rec)

    def user_result(self, tool_use_id="tu_bash", content="ok", *, at=None,
                    is_error=False, uuid=None, tool_use_result=None,
                    message_content=None) -> "_Transcript":
        """A user record carrying a ``tool_result`` — not a human turn."""
        blocks = message_content
        if blocks is None:
            block = {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": content}
            if is_error:
                block["is_error"] = True
            blocks = [block]
        rec = {
            "type": "user",
            "timestamp": self.clock.stamp(at),
            "uuid": uuid or self._uuid(),
            "message": {"role": "user", "content": blocks},
        }
        if tool_use_result is not None:
            rec["toolUseResult"] = tool_use_result
        return self.raw(rec)

    def interrupt(self, *, at=None) -> "_Transcript":
        return self.user("[Request interrupted by user]", at=at)

    def write(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec) + "\n")
        return self.path


class AgentTranscript(_Transcript):
    """One ``subagents/**/agent-<id>.jsonl`` file plus its meta sidecar."""

    def __init__(self, path: Path, clock, ids, meta=None):
        super().__init__(path, clock, ids)
        self.meta = meta

    def write(self) -> Path:
        super().write()
        if self.meta is not None:
            sidecar = self.path.parent / (self.path.stem + ".meta.json")
            sidecar.write_text(json.dumps(self.meta), encoding="utf-8")
        return self.path


class SessionTranscript(_Transcript):
    """A main-thread transcript and every subagent hanging off it."""

    def __init__(self, projects_dir: Path, project: str, session_id: str,
                 cwd: str):
        clock = Clock()
        ids = itertools.count(1)
        proj_dir = projects_dir / project
        super().__init__(proj_dir / f"{session_id}.jsonl", clock, ids)
        self.session_id = session_id
        self.base = proj_dir / session_id
        self.cwd = cwd
        self._agents: list = []
        self._journals: dict = {}

    # -- session-only records ---------------------------------------------

    def meta(self, *, cwd=None, branch="main", version="2.0.0",
             at=None) -> "SessionTranscript":
        """A record carrying the session-level fields the parser harvests."""
        return self.raw({
            "type": "system",
            "subtype": "init",
            "timestamp": self.clock.stamp(at),
            "uuid": self._uuid(),
            "cwd": cwd or self.cwd,
            "gitBranch": branch,
            "version": version,
        })

    def title(self, text) -> "SessionTranscript":
        return self.raw({"type": "ai-title", "aiTitle": text})

    def last_prompt(self, text) -> "SessionTranscript":
        return self.raw({"type": "last-prompt", "lastPrompt": text})

    def permission_mode(self, mode) -> "SessionTranscript":
        return self.raw({"type": "permission-mode", "permissionMode": mode})

    def file_edit(self, path) -> "SessionTranscript":
        return self.raw({"type": "file-history-delta", "trackingPath": path})

    def turn_duration(self, ms, *, uuid=None) -> "SessionTranscript":
        return self.raw({
            "type": "system", "subtype": "turn_duration",
            "uuid": uuid or self._uuid(), "durationMs": ms,
        })

    def spawn_agent(self, agent_id, *, description="", subagent_type="",
                    prompt="", model="", tool_id=None,
                    at=None) -> "SessionTranscript":
        """The main-thread Agent tool call and its result, as a pair.

        This is the link the parser follows from the main transcript to the
        subagent file on disk.
        """
        tid = tool_id or f"tu_agent_{agent_id}"
        self.assistant(
            at=at,
            tools=[tool_use("Agent", {"description": description,
                                      "subagent_type": subagent_type,
                                      "prompt": prompt}, tid)],
        )
        return self.user_result(tid, tool_use_result={
            "agentId": agent_id,
            "description": description,
            "prompt": prompt,
            "resolvedModel": model,
        })

    # -- subagent transcripts ---------------------------------------------

    def agent(self, agent_id, *, meta=None, workflow=None) -> AgentTranscript:
        sub = self.base / "subagents"
        if workflow:
            sub = sub / "workflows" / workflow
        t = AgentTranscript(sub / f"agent-{agent_id}.jsonl", self.clock,
                            self._ids, meta)
        self._agents.append(t)
        return t

    def journal(self, workflow, agent_id, result) -> "SessionTranscript":
        """A workflow journal entry recording one agent's return value."""
        self._journals.setdefault(workflow, []).append(
            {"type": "result", "agentId": agent_id, "result": result}
        )
        return self

    def workflow_script(self, workflow, name) -> "SessionTranscript":
        d = self.base / "workflows" / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}-{workflow}.js").write_text(
            "export const meta = {}\n", encoding="utf-8"
        )
        return self

    def build(self) -> Path:
        """Write the session, its subagents and any workflow journals."""
        if not any(r.get("cwd") for r in self.records):
            # Stamped at the transcript's own earliest moment: an implicit
            # record must never stretch the session's span, or a deliberately
            # ancient fixture would look like it was active just now.
            first = self.clock.earliest if self.clock.earliest is not None else 0
            self.records.insert(0, {
                "type": "system", "subtype": "init",
                "timestamp": ts_at(first), "uuid": "uuid-cwd",
                "cwd": self.cwd, "gitBranch": "main", "version": "2.0.0",
            })
        path = super().write()
        for a in self._agents:
            a.write()
        for wf, entries in self._journals.items():
            jdir = self.base / "subagents" / "workflows" / wf
            jdir.mkdir(parents=True, exist_ok=True)
            with open(jdir / "journal.jsonl", "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e) + "\n")
        return path


class CorpusDir:
    """A throwaway ``~/.claude`` holding synthetic transcripts."""

    def __init__(self, root: Path):
        self.root = root
        self.projects_dir = root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._n = itertools.count(1)

    def session(self, project="proj", session_id=None,
                cwd=None) -> SessionTranscript:
        n = next(self._n)
        sid = session_id or f"sess{n:04d}-0000-0000-0000-00000000000{n}"
        cwd = cwd or f"/home/dev/{project}"
        encoded = "-" + cwd.strip("/").replace("/", "-")
        return SessionTranscript(self.projects_dir, encoded, sid, cwd)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """Never let a test read or write the developer's real Claude data.

    ``Corpus`` falls back to ``Path.home()`` for both the transcript root and
    the on-disk cache, and ``DataStore`` offers no way to override the cache
    path at all — so a test that forgets an argument would silently scribble
    on the real index. Redirecting home makes that impossible rather than
    merely unlikely.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return fake_home


@pytest.fixture
def corpus_dir(tmp_path) -> CorpusDir:
    return CorpusDir(tmp_path / "claude")


@pytest.fixture
def corpus(corpus_dir, tmp_path):
    from claude_monitor.parser import Corpus

    return Corpus(claude_dir=corpus_dir.root, cache_dir=tmp_path / "cache")
