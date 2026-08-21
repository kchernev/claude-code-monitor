"""Text I/O must not depend on the platform's default encoding.

On Windows the default text encoding is the ANSI code page (cp1252 on most
systems), so ``open()`` and ``Path.read_text()`` without an explicit
``encoding`` fail on any character outside that table. Every request to the
web UI returned a 500, because ``index.html`` contains box-drawing characters.

Linux cannot be put into cp1252, but it can be put into a C/ASCII locale,
which fails in exactly the same way and on the same bytes. The tests below run
the real code paths in a child interpreter under that locale, and a static
guard catches the next occurrence anywhere in the package.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import ROOT, CorpusDir

PACKAGE = ROOT / "claude_monitor"

# Enough to blow up cp1252 and ASCII alike: box drawing, an em dash, accents.
SPICY = "café — naïve ═╡ ✓"


# HOME is redirected for isolation, which also moves the user site-packages
# the child would otherwise import flask and psutil from — so carry this
# interpreter's real package directories over explicitly.
SITE_DIRS = [p for p in sys.path
             if p and ("site-packages" in p or "dist-packages" in p)]


def run_ascii(code: str, home: Path) -> subprocess.CompletedProcess:
    """Run ``code`` in a child interpreter whose default encoding is ASCII."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(ROOT)] + SITE_DIRS),
        "HOME": str(home),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",              # off with Python 3.15's UTF-8 default
        "PYTHONCOERCECLOCALE": "0",     # and off with PEP 538 coercion
        "PYTHONIOENCODING": "ascii",
    }
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.fixture(scope="module")
def ascii_locale_available(tmp_path_factory):
    """Skip when the interpreter refuses to leave UTF-8 mode."""
    home = tmp_path_factory.mktemp("probe")
    p = run_ascii("import locale; print(locale.getpreferredencoding(False))", home)
    encoding = (p.stdout or "").strip().lower()
    if encoding.replace("-", "") in ("utf8", ""):
        pytest.skip(f"cannot force a non-UTF-8 default encoding (got {encoding!r})")
    return encoding


def payload(proc: subprocess.CompletedProcess) -> dict:
    """The child's JSON verdict, with its traceback surfaced on failure."""
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


def test_the_shipped_template_is_not_representable_in_the_windows_code_page():
    # If this ever stops being true the regression below stops proving
    # anything, so assert the precondition rather than assuming it.
    raw = (PACKAGE / "web" / "templates" / "index.html").read_bytes()
    assert any(b > 127 for b in raw)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")


def test_the_web_ui_serves_its_page_under_a_non_utf8_default_encoding(
        tmp_path, ascii_locale_available):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    result = payload(run_ascii(f"""
        import json
        from pathlib import Path
        from claude_monitor.web.server import create_app
        app = create_app(claude_dir=Path({str(home / ".claude")!r}),
                         allow_network=False)
        r = app.test_client().get("/")
        print(json.dumps({{"status": r.status_code,
                           "bytes": len(r.get_data())}}))
    """, home))

    assert result["status"] == 200
    assert result["bytes"] > 0


# ---------------------------------------------------------------------------
# The rest of the text I/O the parser does
# ---------------------------------------------------------------------------


def test_an_agent_sidecar_with_non_ascii_text_is_read_not_crashed_on(
        tmp_path, ascii_locale_available):
    # These sidecars are written by Claude Code, in UTF-8, and an agent
    # description is free text. The handler around this read caught
    # JSONDecodeError but not UnicodeDecodeError, so it took down the parse.
    home = tmp_path / "home"
    home.mkdir()
    sub = tmp_path / "subagents"
    sub.mkdir()
    (sub / "agent-a.jsonl").write_text("", encoding="utf-8")
    (sub / "agent-a.meta.json").write_text(
        json.dumps({"description": SPICY, "agentType": "Explore"},
                   ensure_ascii=False), encoding="utf-8")

    result = payload(run_ascii(f"""
        import json
        from pathlib import Path
        from claude_monitor.parser import _read_agent_meta
        meta = _read_agent_meta(Path({str(sub / "agent-a.jsonl")!r}))
        print(json.dumps({{"description": meta.get("description", ""),
                           "type": meta.get("agentType", "")}}))
    """, home))

    assert result["description"] == SPICY
    assert result["type"] == "Explore"


def test_a_non_ascii_transcript_parses_and_round_trips_through_the_cache(
        tmp_path, ascii_locale_available):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "-home-dev-proj"
    projects.mkdir(parents=True)
    records = [
        {"type": "ai-title", "aiTitle": SPICY},
        {"type": "user", "uuid": "u1", "timestamp": "2026-08-20T12:00:00Z",
         "cwd": "/home/dev/proj",
         "message": {"role": "user", "content": SPICY}},
        {"type": "assistant", "uuid": "u2", "requestId": "r1",
         "timestamp": "2026-08-20T12:00:01Z",
         "message": {"role": "assistant", "id": "m1",
                     "model": "claude-opus-5",
                     "usage": {"output_tokens": 10}}},
    ]
    with open(projects / "s1.jsonl", "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    result = payload(run_ascii(f"""
        import json
        from pathlib import Path
        from claude_monitor.parser import Corpus
        claude = Path({str(home / ".claude")!r})
        cache = Path({str(tmp_path / "cache")!r})

        first = Corpus(claude_dir=claude, cache_dir=cache).load()
        # A second, cold instance reads the index back off disk.
        second = Corpus(claude_dir=claude, cache_dir=cache).load()
        print(json.dumps({{
            "parsed_title": first[0].title,
            "cached_title": second[0].title,
            "prompt": second[0].prompts[0]["text"],
            "output": second[0].usage.output_tokens,
        }}))
    """, home))

    assert result["parsed_title"] == SPICY
    assert result["cached_title"] == SPICY
    assert result["prompt"] == SPICY
    assert result["output"] == 10


def test_a_workflow_script_with_non_ascii_source_is_served_intact(
        tmp_path, ascii_locale_available):
    # A workflow script is arbitrary JavaScript the user wrote; decoding it
    # with the platform default silently mojibakes the source shown in the
    # debugger rather than raising, which is the harder failure to notice.
    home = tmp_path / "home"
    claude = home / ".claude"
    corpus = CorpusDir(claude)
    sess = corpus.session(project="proj", session_id="s1")
    sess.agent("w1", workflow="wf_abc123").assistant(
        output=5, stop_reason="end_turn")
    sess.workflow_script("wf_abc123", "review")
    sess.build()
    script = (claude / "projects" / "-home-dev-proj" / "s1" / "workflows"
              / "scripts" / "review-wf_abc123.js")
    script.write_text(
        "export const meta = {{ name: 'review', description: '{0}' }}\n"
        "// {0}\n".format(SPICY), encoding="utf-8")

    result = payload(run_ascii(f"""
        import json
        from pathlib import Path
        from claude_monitor.web.server import create_app
        app = create_app(claude_dir=Path({str(claude)!r}), allow_network=False)
        store = app.config["STORE"]
        store._publish(store.corpus.load())
        r = app.test_client().get("/api/workflows/s1/wf_abc123")
        body = r.get_json() or {{}}
        print(json.dumps({{"status": r.status_code,
                           "script": body.get("script", ""),
                           "description": body.get("description", "")}}))
    """, home))

    assert result["status"] == 200
    assert SPICY in result["script"]
    assert result["description"] == SPICY


def test_claude_codes_own_state_file_is_read_as_utf8(
        tmp_path, ascii_locale_available):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"organizationRateLimitTier": "claude_max_20x",
                         "organizationName": SPICY},
    }, ensure_ascii=False), encoding="utf-8")

    result = payload(run_ascii("""
        import json
        from claude_monitor.web.server import plan_payload
        p = plan_payload(allow_network=False)
        print(json.dumps({"available": p.get("available"),
                          "plan": p.get("plan")}))
    """, home))

    assert result["available"] is True
    assert result["plan"] == "Max 20×"


# ---------------------------------------------------------------------------
# Static guard against the next occurrence
# ---------------------------------------------------------------------------


def _mode_of(call: ast.Call, position: int) -> str:
    """The literal file mode a call requests, or '' if it is not a literal."""
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    if len(call.args) > position:
        arg = call.args[position]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return ""


def _has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" or kw.arg is None for kw in call.keywords)


# Modules whose ``open`` is not a text-file open: ``os.open`` returns a raw
# descriptor and takes no encoding, ``webbrowser.open`` opens a URL, and the
# archive modules deal in bytes.
_NOT_A_TEXT_FILE_OPEN = {
    "webbrowser", "os", "io", "socket", "shelve", "tarfile", "zipfile",
    "gzip", "bz2", "lzma", "codecs", "sqlite3", "wave", "dbm", "urllib",
}


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        if (f.attr == "open" and isinstance(f.value, ast.Name)
                and f.value.id in _NOT_A_TEXT_FILE_OPEN):
            return ""
        return f.attr
    return ""


def find_locale_dependent_text_io(path: Path):
    """Yield ``(line, source)`` for text I/O with no explicit encoding."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        if name in ("open", "fdopen"):
            # Position 1 is the mode for open()/Path.open()/os.fdopen().
            if "b" in _mode_of(node, 1) or _has_encoding(node):
                continue
            findings.append((node.lineno, name))
        elif name in ("read_text", "write_text"):
            if not _has_encoding(node):
                findings.append((node.lineno, name))
        elif name == "run":
            kwargs = {kw.arg for kw in node.keywords}
            if not {"text", "universal_newlines"} & kwargs:
                continue
            if "encoding" not in kwargs:
                findings.append((node.lineno, "subprocess.run(text=True)"))
    return findings


def test_no_text_io_in_the_package_relies_on_the_platform_encoding():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for line, what in find_locale_dependent_text_io(path):
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {what}")
    assert offenders == [], (
        "these calls decode or encode with the platform default, which is the "
        "ANSI code page on Windows — pass encoding=\"utf-8\":\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_actually_catches_the_pattern_it_screens_for(tmp_path):
    # A guard that never fires is worse than no guard, so prove it fires.
    sample = tmp_path / "sample.py"
    sample.write_text(textwrap.dedent("""
        from pathlib import Path
        import subprocess

        def bad():
            open("a.txt").read()
            Path("b.txt").read_text()
            Path("c.txt").write_text("x")
            subprocess.run(["git", "log"], text=True)

        def good():
            open("a.txt", "rb").read()
            open("a.txt", encoding="utf-8").read()
            Path("b.txt").read_text(encoding="utf-8")
            Path("c.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "log"], text=True, encoding="utf-8")
            subprocess.run(["git", "log"], capture_output=True)
    """), encoding="utf-8")

    found = {what for _line, what in find_locale_dependent_text_io(sample)}
    assert found == {"open", "read_text", "write_text",
                     "subprocess.run(text=True)"}
    assert len(find_locale_dependent_text_io(sample)) == 4


def test_the_guard_ignores_calls_that_only_share_the_name_open(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(textwrap.dedent("""
        import os, webbrowser

        def fine():
            webbrowser.open("http://localhost:8787")
            fd = os.open("f", os.O_WRONLY)
            os.fdopen(fd, "wb").close()
    """), encoding="utf-8")
    assert find_locale_dependent_text_io(sample) == []


def test_the_guard_still_catches_a_text_mode_fdopen(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(textwrap.dedent("""
        import os

        def bad(fd):
            os.fdopen(fd, "w").write("x")
    """), encoding="utf-8")
    assert [what for _l, what in find_locale_dependent_text_io(sample)] == [
        "fdopen"]
