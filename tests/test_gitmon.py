"""Git monitoring: numstat parsing, line counting, and the subprocess guards."""

from __future__ import annotations

import subprocess

import pytest

from claude_monitor import gitmon
from claude_monitor.gitmon import (
    GitMonitor, UNTRACKED_FILE_CAP, count_lines, parse_numstat, run_git,
    sample_repo,
)


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repository with one commit."""
    d = tmp_path / "repo"
    d.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }

    def git(*args):
        return subprocess.run(["git", "-C", str(d), *args], env=env,
                              capture_output=True, text=True, check=True)

    git("init", "-q", "-b", "main")
    (d / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "café — naïve commit subject")
    return d


# ---------------------------------------------------------------------------
# numstat
# ---------------------------------------------------------------------------


def test_numstat_totals_additions_and_deletions_per_file():
    added, deleted, files = parse_numstat("10\t2\tsrc/a.py\n0\t5\tsrc/b.py\n")
    assert (added, deleted) == (10, 7)
    assert files == {"src/a.py": (10, 2), "src/b.py": (0, 5)}


def test_a_binary_file_counts_as_no_churn():
    # git writes "-" rather than a number for binary blobs.
    added, deleted, files = parse_numstat("-\t-\tdocs/logo.png\n")
    assert (added, deleted) == (0, 0)
    assert files == {"docs/logo.png": (0, 0)}


def test_a_path_containing_a_tab_survives_intact():
    _a, _d, files = parse_numstat("1\t1\tweird\tname.py\n")
    assert "weird\tname.py" in files


def test_short_and_empty_lines_are_skipped():
    added, deleted, files = parse_numstat("\ngarbage\n1\t1\tok.py\n2\t2\n")
    assert (added, deleted) == (1, 1)
    assert list(files) == ["ok.py"]


def test_empty_numstat_output_is_all_zeroes():
    assert parse_numstat("") == (0, 0, {})


# ---------------------------------------------------------------------------
# Untracked line counting
# ---------------------------------------------------------------------------


def test_lines_are_counted_for_a_text_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert count_lines(str(p)) == 3


def test_a_binary_file_counts_as_zero_lines(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"\x89PNG\x00\x1a\n\n\n")
    assert count_lines(str(p)) == 0


def test_a_file_larger_than_the_cap_is_not_read(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_bytes(b"x\n" * (UNTRACKED_FILE_CAP // 2 + 10))
    assert count_lines(str(p)) == 0


def test_a_missing_file_counts_as_zero(tmp_path):
    assert count_lines(str(tmp_path / "nope.txt")) == 0


def test_the_count_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    assert count_lines(str(p)) == 2

    reads = []
    real_open = open

    def counting_open(*a, **kw):
        reads.append(a[0])
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    assert count_lines(str(p)) == 2
    assert reads == []                      # served from the (mtime, size) cache


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


def test_git_text_is_decoded_as_utf8_not_the_platform_locale(monkeypatch):
    # Windows defaults to the ANSI code page, which mangles any non-ASCII
    # branch name, path or commit subject git hands back.
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gitmon.subprocess, "run", fake_run)
    run_git("/tmp", "status")
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_a_non_ascii_commit_subject_round_trips(repo):
    out = run_git(str(repo), "log", "-1", "--format=%s")
    assert out.strip() == "café — naïve commit subject"


def test_a_failing_git_command_yields_empty_output(repo):
    assert run_git(str(repo), "rev-parse", "--verify", "no-such-ref") == ""


def test_git_in_a_directory_that_is_not_a_repo_yields_empty_output(tmp_path):
    assert run_git(str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD") == ""


def test_a_missing_git_binary_is_survivable(monkeypatch):
    def boom(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(gitmon.subprocess, "run", boom)
    assert run_git("/tmp", "status") == ""


def test_a_hung_git_command_is_survivable(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(gitmon.subprocess, "run", boom)
    assert run_git("/tmp", "status") == ""


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def test_sampling_a_clean_repo_reports_its_branch_and_no_work_in_progress(repo):
    snap = sample_repo(str(repo))
    assert snap["branch"] == "main"
    assert snap["wip"] == 0
    assert snap["untracked_files"] == 0
    assert snap["commits"][0]["subject"] == "café — naïve commit subject"


def test_uncommitted_and_untracked_work_shows_up_as_wip(repo):
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\ny\n", encoding="utf-8")
    snap = sample_repo(str(repo))

    # One added line in a tracked file, plus two lines of a new untracked one.
    assert snap["unstaged_add"] == 1
    assert (snap["untracked_files"], snap["untracked_lines"]) == (1, 2)
    assert snap["wip"] == 3
    assert [c["file"] for c in snap["changed"]] == ["a.txt"]


def test_staged_and_unstaged_edits_to_one_file_are_merged(repo):
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True,
                   capture_output=True)
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\n",
                                encoding="utf-8")
    snap = sample_repo(str(repo))

    assert snap["staged_add"] == 1 and snap["unstaged_add"] == 1
    assert snap["wip"] == 2
    assert len(snap["changed"]) == 1          # one file, not two entries
    assert snap["changed"][0]["add"] == 2


def test_sampling_something_that_is_not_a_directory_returns_nothing(tmp_path):
    assert sample_repo(str(tmp_path / "nope")) is None


def test_sampling_a_plain_directory_returns_nothing(tmp_path):
    assert sample_repo(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Input validation on the subprocess boundary
# ---------------------------------------------------------------------------


class _NoSessions:
    def sessions(self, **_kw):
        return []


@pytest.mark.parametrize("sha", [
    "--output=/tmp/pwned",      # a git option, not a revision
    "HEAD",                     # a ref expression
    "main..HEAD",
    "deadbee; rm -rf /",
    "",
    "abc",                      # too short to be a real abbreviation
    "g" * 40,                   # not hex
])
def test_only_a_bare_hex_sha_is_accepted_for_a_commit_lookup(sha, monkeypatch):
    # A crafted URL must not be able to smuggle options or rev expressions
    # into the git subprocess.
    called = []
    monkeypatch.setattr(gitmon, "run_git",
                        lambda *a, **kw: called.append(a) or "")
    gm = GitMonitor(_NoSessions())
    assert gm.commit_detail("any-repo-id", sha) is None
    assert called == []          # rejected before git is ever invoked


def test_a_well_formed_sha_gets_past_validation_to_the_repo_lookup(monkeypatch):
    gm = GitMonitor(_NoSessions())
    # No such repo, so it still returns None — but only after the sha passed.
    assert gm.commit_detail("unknown-repo", "a" * 40) is None


def test_repo_ids_are_stable_and_path_derived():
    assert gitmon._slug("/home/dev/proj") == gitmon._slug("/home/dev/proj")
    assert gitmon._slug("/home/dev/proj") != gitmon._slug("/home/dev/other")
