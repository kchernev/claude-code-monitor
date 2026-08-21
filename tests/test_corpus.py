"""The incremental cache layer: what gets re-parsed, and what gets reused."""

from __future__ import annotations

import json
import os
import stat

import pytest

from claude_monitor import parser as parser_mod
from claude_monitor.parser import CACHE_VERSION, Corpus, _fingerprint


@pytest.fixture
def count_parses(monkeypatch):
    """Count how many transcripts actually get re-read."""
    calls = []
    real = parser_mod.parse_session_file

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(parser_mod, "parse_session_file", counting)
    return calls


def append_record(path, rec=None):
    """Grow a transcript the way a live session would."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec or {"type": "user", "uuid": "extra",
                                    "timestamp": "2026-08-20T13:00:00Z",
                                    "message": {"role": "user",
                                                "content": "more"}}) + "\n")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_every_transcript_under_projects_is_found(corpus_dir, corpus):
    a = corpus_dir.session(project="alpha").assistant(output=1).build()
    b = corpus_dir.session(project="beta").assistant(output=1).build()
    assert set(corpus.session_paths()) == {a, b}


def test_subagent_transcripts_are_not_mistaken_for_sessions(corpus_dir, corpus):
    s = corpus_dir.session()
    s.agent("a1").assistant(output=1)
    main = s.build()
    # Subagents live a directory deeper than the "*/*.jsonl" session glob.
    assert corpus.session_paths() == [main]


def test_a_missing_projects_directory_is_not_an_error(tmp_path):
    c = Corpus(claude_dir=tmp_path / "nothing", cache_dir=tmp_path / "cache")
    assert c.session_paths() == []
    assert c.load() == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_an_unchanged_transcript_is_parsed_once_and_then_served_from_cache(
        corpus_dir, corpus, count_parses):
    corpus_dir.session().assistant(output=100).build()

    first = corpus.load()
    assert len(count_parses) == 1

    second = corpus.load()
    assert len(count_parses) == 1                 # no re-parse
    assert second[0].usage.output_tokens == first[0].usage.output_tokens


def test_a_grown_transcript_is_re_parsed(corpus_dir, corpus, count_parses):
    path = corpus_dir.session().assistant(output=100).build()
    corpus.load()
    count_parses.clear()

    append_record(path)
    sessions = corpus.load()
    assert count_parses == [str(path)]
    assert sessions[0].user_turns == 1


def test_a_new_subagent_transcript_invalidates_its_session(
        corpus_dir, corpus, count_parses):
    # The fingerprint has to cover the subagent directory, or a fan-out that
    # runs while the main thread is quiet would never show up.
    s = corpus_dir.session()
    s.assistant(output=10)
    path = s.build()
    corpus.load()
    count_parses.clear()

    sub = path.parent / path.stem / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-late.jsonl").write_text(
        json.dumps({"type": "assistant", "uuid": "x", "requestId": "r",
                    "timestamp": "2026-08-20T13:00:00Z",
                    "message": {"role": "assistant", "id": "m",
                                "model": "claude-opus-5",
                                "usage": {"output_tokens": 7}}}) + "\n",
        encoding="utf-8")

    sessions = corpus.load()
    assert count_parses == [str(path)]
    assert [a.agent_id for a in sessions[0].agents] == ["late"]


def test_force_re_parses_even_a_valid_cache_entry(
        corpus_dir, corpus, count_parses):
    corpus_dir.session().assistant(output=1).build()
    corpus.load()
    count_parses.clear()
    corpus.load(force=True)
    assert len(count_parses) == 1


def test_the_fingerprint_changes_with_size(corpus_dir):
    path = corpus_dir.session().assistant(output=1).build()
    before = _fingerprint(path)
    append_record(path)
    assert _fingerprint(path) != before


def test_the_fingerprint_of_a_missing_file_is_stable_and_harmless(tmp_path):
    assert _fingerprint(tmp_path / "gone.jsonl") == "0:0"


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------


def test_the_cache_survives_a_restart(corpus_dir, tmp_path, count_parses):
    cache = tmp_path / "cache"
    corpus_dir.session().assistant(output=42).build()

    Corpus(claude_dir=corpus_dir.root, cache_dir=cache).load()
    assert len(count_parses) == 1

    fresh = Corpus(claude_dir=corpus_dir.root, cache_dir=cache)
    sessions = fresh.load()
    assert len(count_parses) == 1                 # served from the saved index
    assert sessions[0].usage.output_tokens == 42


def test_an_index_from_an_older_format_is_ignored(corpus_dir, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"index-v{CACHE_VERSION}.json").write_text(
        json.dumps({"version": CACHE_VERSION - 1, "entries": {"x": "junk"}}),
        encoding="utf-8")
    c = Corpus(claude_dir=corpus_dir.root, cache_dir=cache)
    assert c._cache == {}


def test_a_corrupt_index_is_discarded_rather_than_fatal(corpus_dir, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"index-v{CACHE_VERSION}.json").write_text(
        "{not json", encoding="utf-8")
    c = Corpus(claude_dir=corpus_dir.root, cache_dir=cache)
    assert c._cache == {}
    assert c.load() == []


def test_a_corrupt_entry_falls_through_to_a_fresh_parse(
        corpus_dir, corpus, count_parses):
    path = corpus_dir.session().assistant(output=5).build()
    corpus.load()
    count_parses.clear()

    # Keep the fingerprint valid but wreck the payload.
    corpus._cache[str(path)]["data"] = {"session_id": "x"}
    sessions = corpus.load()

    assert count_parses == [str(path)]
    assert sessions[0].usage.output_tokens == 5


def test_entries_for_deleted_transcripts_are_dropped(corpus_dir, corpus):
    keep = corpus_dir.session(project="keep").assistant(output=1).build()
    gone = corpus_dir.session(project="gone").assistant(output=1).build()
    corpus.load()
    assert len(corpus._cache) == 2

    gone.unlink()
    corpus.load()
    assert list(corpus._cache) == [str(keep)]


def test_a_scoped_load_does_not_prune_the_rest_of_the_index(corpus_dir, corpus):
    a = corpus_dir.session(project="alpha").assistant(output=1).build()
    corpus_dir.session(project="beta").assistant(output=1).build()
    corpus.load()

    corpus.load(paths=[a])
    assert len(corpus._cache) == 2


def test_indexes_from_superseded_versions_are_cleaned_up(corpus_dir, corpus):
    corpus.cache_dir.mkdir(parents=True, exist_ok=True)
    stale = corpus.cache_dir / "index-v1.json"
    stale.write_text("{}", encoding="utf-8")

    corpus_dir.session().assistant(output=1).build()
    corpus.load()

    assert not stale.exists()
    assert corpus.cache_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_index_is_private_because_it_holds_prompt_text(corpus_dir, corpus):
    corpus_dir.session().user("something private").build()
    corpus.load()

    assert stat.S_IMODE(corpus.cache_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(corpus.cache_dir.stat().st_mode) == 0o700


def test_saving_leaves_no_temp_file_behind(corpus_dir, corpus):
    corpus_dir.session().assistant(output=1).build()
    corpus.load()
    assert list(corpus.cache_dir.glob("*.tmp")) == []


def test_a_throttled_save_is_skipped_rather_than_rewriting_the_index(
        corpus_dir, corpus):
    # A live session drives this path every couple of seconds and the index is
    # measured in megabytes.
    corpus_dir.session().assistant(output=1).build()
    corpus.load()
    assert corpus._unsaved is False

    corpus._cache["synthetic"] = {"fp": "x", "data": {}}
    corpus.save_cache(force=False)
    assert corpus._unsaved is True               # deferred, not written

    corpus.save_cache(force=True)
    assert corpus._unsaved is False


def test_an_unwritable_cache_directory_does_not_break_loading(
        corpus_dir, tmp_path):
    # The cache is only an optimisation; losing it must never lose the data.
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    corpus_dir.session().assistant(output=9).build()

    c = Corpus(claude_dir=corpus_dir.root, cache_dir=blocker / "cache")
    sessions = c.load()
    assert sessions[0].usage.output_tokens == 9


# ---------------------------------------------------------------------------
# Incremental refresh (what the live dashboard polls)
# ---------------------------------------------------------------------------


def test_refresh_reports_nothing_when_nothing_moved(
        corpus_dir, corpus, count_parses):
    corpus_dir.session().assistant(output=1).build()
    sessions = corpus.load()
    count_parses.clear()

    result, changed = corpus.refresh(sessions)
    assert changed == []
    assert count_parses == []
    # The untouched session object is handed straight back, not rebuilt.
    assert result[0] is sessions[0]


def test_refresh_re_parses_only_the_transcript_that_moved(
        corpus_dir, corpus, count_parses):
    quiet = corpus_dir.session(project="quiet").assistant(output=1).build()
    busy = corpus_dir.session(project="busy").assistant(output=1).build()
    sessions = corpus.load()
    count_parses.clear()

    append_record(busy)
    result, changed = corpus.refresh(sessions)

    assert count_parses == [str(busy)]
    by_path = {s.path: s for s in result}
    assert changed == [by_path[str(busy)].session_id]
    assert by_path[str(quiet)] is next(s for s in sessions if s.path == str(quiet))


def test_refresh_picks_up_a_transcript_that_did_not_exist_before(
        corpus_dir, corpus):
    corpus_dir.session(project="first").assistant(output=1).build()
    sessions = corpus.load()

    corpus_dir.session(project="second").assistant(output=1).build()
    result, changed = corpus.refresh(sessions)

    assert len(result) == 2
    assert len(changed) == 1
