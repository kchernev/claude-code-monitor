"""Live git activity for the repositories Claude Code has sessions in.

Adapted from GitMonitor (~/workspace/gitmonitor), with one structural change:
repos are not configured by hand — they are discovered from the session corpus
by resolving each session's ``cwd`` to its git toplevel. Everything here is
read-only ``git`` run on a background thread.

Sampling cadence is tiered by how recently Claude worked in a repo: a repo
with a live session samples every tick, one touched in the last two days every
~30s, anything older every ~5min — so a long history of projects doesn't turn
into a constant fork storm.

The WIP series lives in memory from server start (uncommitted state cannot be
reconstructed after the fact); the committed series is rebuilt from ``git log``
on demand, so the chart is meaningful immediately.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

SAMPLE_INTERVAL = 5.0          # seconds between ticks
HISTORY_KEEP = 24 * 3600       # in-memory WIP samples kept this long
UNTRACKED_FILE_CAP = 2_000_000 # skip line-counting untracked files bigger
CHART_BUCKETS = 240            # max points served per history request
_RECENT_EVERY = 6              # ticks between samples, repo active in last 48h
_IDLE_EVERY = 60               # ticks between samples, older repos
_COMMITS_TTL = 30.0            # seconds to cache a repo's commit log


# ---------------------------------------------------------------- git helpers


def run_git(repo: str, *args: str, timeout: float = 10) -> str:
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
        return p.stdout if p.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def parse_numstat(text: str) -> Tuple[int, int, Dict[str, Tuple[int, int]]]:
    """Return (added, deleted, files{path: (a, d)}). Binary files count as 0."""
    added = deleted = 0
    files: Dict[str, Tuple[int, int]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a = int(parts[0]) if parts[0].isdigit() else 0
        d = int(parts[1]) if parts[1].isdigit() else 0
        added += a
        deleted += d
        files[parts[2]] = (a, d)
    return added, deleted, files


_line_cache: Dict[str, Tuple[float, int, int]] = {}


def count_lines(path: str) -> int:
    """Line count for an untracked file, cached on (mtime, size)."""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    if st.st_size > UNTRACKED_FILE_CAP:
        return 0
    cached = _line_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
            if b"\0" in head:            # binary
                _line_cache[path] = (st.st_mtime, st.st_size, 0)
                return 0
            n = head.count(b"\n")
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
        _line_cache[path] = (st.st_mtime, st.st_size, n)
        return n
    except OSError:
        return 0


def sample_repo(path: str) -> Optional[dict]:
    """One read-only snapshot of a repo's working state."""
    if not os.path.isdir(path):
        return None

    branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not branch:
        return None
    ua, ud, ufiles = parse_numstat(run_git(path, "diff", "--numstat"))
    sa, sd, sfiles = parse_numstat(run_git(path, "diff", "--cached", "--numstat"))

    untracked_names = [
        l for l in run_git(path, "ls-files", "--others",
                           "--exclude-standard").splitlines() if l
    ]
    untracked_lines = sum(
        count_lines(os.path.join(path, f)) for f in untracked_names
    )

    ca, cd, _ = parse_numstat(
        run_git(path, "log", "--since=midnight", "--numstat", "--format=")
    )
    commits_today = len([
        l for l in run_git(path, "log", "--since=midnight",
                           "--format=%h").splitlines() if l
    ])

    commits = []
    for line in run_git(path, "log", "-5",
                        "--format=%h%x09%ct%x09%an%x09%s").splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "t": int(parts[1]),
                            "author": parts[2], "subject": parts[3]})

    ahead = behind = None
    lr = run_git(path, "rev-list", "--left-right", "--count",
                 "@{upstream}...HEAD").split()
    if len(lr) == 2:
        behind, ahead = int(lr[0]), int(lr[1])

    # Merge staged + unstaged per file for the activity list.
    changed: Dict[str, dict] = {}
    for fname, (a, d) in sfiles.items():
        changed[fname] = {"file": fname, "add": a, "del": d, "staged": True}
    for fname, (a, d) in ufiles.items():
        e = changed.setdefault(
            fname, {"file": fname, "add": 0, "del": 0, "staged": False}
        )
        e["add"] += a
        e["del"] += d
    changed_list = sorted(
        changed.values(), key=lambda e: -(e["add"] + e["del"])
    )[:8]

    wip = sa + ua + untracked_lines
    return {
        "branch": branch,
        "staged_add": sa, "staged_del": sd,
        "unstaged_add": ua, "unstaged_del": ud,
        "untracked_files": len(untracked_names),
        "untracked_lines": untracked_lines,
        "committed_add": ca, "committed_del": cd,
        "commits_today": commits_today,
        "wip": wip,
        "ahead": ahead, "behind": behind,
        "changed": changed_list,
        "commits": commits,
        "sampled_at": time.time(),
    }


def _slug(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- monitor


class GitMonitor:
    """Discovers session repos and keeps live git state for them."""

    def __init__(self, store, interval: float = SAMPLE_INTERVAL):
        self.store = store            # DataStore: .sessions() never blocks
        self.interval = interval
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._repos: Dict[str, dict] = {}     # rid -> {path, name, session agg}
        self._latest: Dict[str, dict] = {}    # rid -> last sample_repo() dict
        self._samples: Dict[str, List[Tuple[float, int]]] = {}  # rid -> (t, wip)
        self._toplevel: Dict[str, Tuple[Optional[str], float]] = {}
        self._commit_log: Dict[str, Tuple[float, List[dict]]] = {}
        self._stats_log: Dict[str, Tuple[float, List[dict]]] = {}
        self._tick = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cmon-git", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- repo discovery ---------------------------------------------------

    def _resolve_toplevel(self, cwd: str) -> Optional[str]:
        """cwd -> git toplevel, cached; misses re-checked every 10 minutes."""
        hit = self._toplevel.get(cwd)
        now = time.time()
        if hit and (hit[0] is not None or now - hit[1] < 600):
            return hit[0]
        top = None
        if os.path.isdir(cwd):
            top = run_git(cwd, "rev-parse", "--show-toplevel").strip() or None
        self._toplevel[cwd] = (top, now)
        return top

    def _discover(self) -> None:
        """Rebuild the repo list from the current session snapshot."""
        repos: Dict[str, dict] = {}
        for s in self.store.sessions():
            if not s.cwd:
                continue
            top = self._resolve_toplevel(s.cwd)
            if not top:
                continue
            rid = _slug(top)
            r = repos.get(rid)
            if r is None:
                r = repos[rid] = {
                    "id": rid, "path": top,
                    "name": os.path.basename(top) or top,
                    "sessions": 0, "live": 0, "cost": 0.0,
                    "last_activity": None, "projects": {},
                }
            r["sessions"] += 1
            r["live"] += 1 if s.is_live else 0
            r["cost"] += s.total_cost
            if s.ended and (r["last_activity"] is None
                            or s.ended > r["last_activity"]):
                r["last_activity"] = s.ended
            r["projects"][s.project] = r["projects"].get(s.project, 0) + 1
        with self._lock:
            self._repos = repos
            for gone in set(self._samples) - set(repos):
                self._samples.pop(gone, None)
                self._latest.pop(gone, None)

    # -- sampling ---------------------------------------------------------

    def _due(self, r: dict) -> bool:
        if r["live"]:
            return True
        last = r["last_activity"]
        recent = last is not None and (
            datetime.now(timezone.utc) - last < timedelta(hours=48)
        )
        every = _RECENT_EVERY if recent else _IDLE_EVERY
        # Spread repos across ticks so one tick doesn't sample everything.
        phase = int(r["id"], 16) % every
        return self._tick % every == phase

    def _loop(self) -> None:
        # Work first, wait after — the page is served within moments of
        # startup and must not show an empty repo list for one interval.
        while not self._stop.is_set():
            self._tick += 1
            try:
                self._discover()
                with self._lock:
                    due = [dict(r) for r in self._repos.values() if self._due(r)]
                for r in due:
                    stats = sample_repo(r["path"])
                    now = time.time()
                    with self._lock:
                        if stats is None:
                            self._latest.pop(r["id"], None)
                            continue
                        self._latest[r["id"]] = stats
                        buf = self._samples.setdefault(r["id"], [])
                        buf.append((round(now, 1), stats["wip"]))
                        cutoff = now - HISTORY_KEEP
                        while buf and buf[0][0] < cutoff:
                            buf.pop(0)
            except Exception:
                pass  # a repo mid-gc or a vanished path; next tick retries
            if self._stop.wait(self.interval):
                break

    # -- commit log (for the committed series and markers) ----------------

    def _commits(self, rid: str, path: str) -> List[dict]:
        now = time.time()
        hit = self._commit_log.get(rid)
        if hit and now - hit[0] < _COMMITS_TTL:
            return hit[1]
        out: List[dict] = []
        cur: Optional[dict] = None
        text = run_git(path, "log", "--since=25.hours", "-n", "2000",
                       "--numstat", "--format=%x01%ct%x09%h%x09%s", timeout=20)
        for line in text.splitlines():
            if line.startswith("\x01"):
                parts = line[1:].split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit():
                    cur = {"t": int(parts[0]), "hash": parts[1],
                           "subject": parts[2], "add": 0}
                    out.append(cur)
                else:
                    cur = None
            elif cur is not None and "\t" in line:
                parts = line.split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit():
                    cur["add"] += int(parts[0])
        out.reverse()  # oldest first
        self._commit_log[rid] = (now, out)
        return out

    def _commit_stats(self, rid: str, path: str) -> List[dict]:
        """Per-commit numstat for the last 30 days, briefly cached.

        Richer than :meth:`_commits` (author + per-file churn + deletions),
        and correspondingly heavier, so it is fetched lazily on the stats
        endpoint rather than by the sampler.
        """
        now = time.time()
        hit = self._stats_log.get(rid)
        if hit and now - hit[0] < _COMMITS_TTL:
            return hit[1]
        commits: List[dict] = []
        cur: Optional[dict] = None
        text = run_git(path, "log", "--since=30.days", "-n", "3000",
                       "--numstat", "--format=%x01%ct%x09%an", timeout=30)
        for line in text.splitlines():
            if line.startswith("\x01"):
                parts = line[1:].split("\t", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    cur = {"t": int(parts[0]), "author": parts[1],
                           "add": 0, "del": 0, "files": []}
                    commits.append(cur)
                else:
                    cur = None
            elif cur is not None and "\t" in line:
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    a = int(parts[0]) if parts[0].isdigit() else 0
                    d = int(parts[1]) if parts[1].isdigit() else 0
                    cur["add"] += a
                    cur["del"] += d
                    cur["files"].append((parts[2], a, d))
        self._stats_log[rid] = (now, commits)
        return commits

    def stats(self, repo_sel: str, range_s: int,
              days: Optional[int] = None) -> dict:
        """Commit analytics across repos for the window (GitMonitor's Stats tab)."""
        now = time.time()
        start = now - range_s
        repos = self._shown(days)
        if repo_sel != "all":
            repos = [r for r in repos if r["id"] == repo_sel]
        multi = len(repos) > 1

        gathered: List[Tuple[dict, str]] = []
        for r in repos:
            for c in self._commit_stats(r["id"], r["path"]):
                if c["t"] >= start:
                    gathered.append((c, r["name"]))

        def day_start(t: float) -> float:
            lt = time.localtime(t)
            return time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

        per_day = range_s > 2 * 86400
        buckets: Dict[float, dict] = {}
        if per_day:
            cur_t = day_start(start)
            while cur_t <= now:
                buckets[cur_t] = {"t": cur_t, "commits": 0, "add": 0, "del": 0}
                lt = time.localtime(cur_t)
                cur_t = time.mktime(
                    (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
        else:
            cur_t = start - start % 3600
            while cur_t <= now:
                buckets[cur_t] = {"t": cur_t, "commits": 0, "add": 0, "del": 0}
                cur_t += 3600

        authors: Dict[str, dict] = {}
        files: Dict[str, dict] = {}
        hours = [0] * 24
        tot_add = tot_del = 0
        days_active = set()
        for c, repo_name in gathered:
            key = day_start(c["t"]) if per_day else c["t"] - c["t"] % 3600
            b = buckets.get(key)
            if b:
                b["commits"] += 1
                b["add"] += c["add"]
                b["del"] += c["del"]
            a = authors.setdefault(c["author"], {
                "name": c["author"], "commits": 0, "add": 0, "del": 0})
            a["commits"] += 1
            a["add"] += c["add"]
            a["del"] += c["del"]
            for fname, fa, fd in c["files"]:
                label = f"{repo_name}/{fname}" if multi else fname
                f = files.setdefault(label, {
                    "file": label, "commits": 0, "add": 0, "del": 0})
                f["commits"] += 1
                f["add"] += fa
                f["del"] += fd
            hours[time.localtime(c["t"]).tm_hour] += 1
            tot_add += c["add"]
            tot_del += c["del"]
            days_active.add(time.strftime("%Y-%m-%d", time.localtime(c["t"])))

        return {
            "per_day": per_day,
            "buckets": [buckets[k] for k in sorted(buckets)],
            "authors": sorted(authors.values(), key=lambda a: -a["add"])[:8],
            "files": sorted(files.values(),
                            key=lambda f: -(f["add"] + f["del"]))[:10],
            "hours": hours,
            "totals": {"commits": len(gathered), "add": tot_add,
                       "del": tot_del, "files": len(files),
                       "days": len(days_active)},
        }

    # -- payloads ---------------------------------------------------------

    def _all_repos(self) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self._repos.values()]

    def _shown(self, days: Optional[int]) -> List[dict]:
        """Repos with session activity inside the window; live always counts.

        ``days`` falsy means everything — the "All" pill.
        """
        repos = self._all_repos()
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            repos = [
                r for r in repos
                if r["live"] or (r["last_activity"]
                                 and r["last_activity"] >= cutoff)
            ]
        return repos

    def snapshot(self, days: Optional[int] = None) -> dict:
        now = time.time()
        repos = self._shown(days)
        out = []
        tot = {"wip": 0, "staged": 0, "unstaged": 0, "untracked": 0,
               "committed_today": 0, "commits_today": 0, "dirty": 0}
        with self._lock:
            latest = {rid: dict(v) for rid, v in self._latest.items()}
            sparks = {
                rid: [[t, w] for t, w in pts if t >= now - 1800]
                for rid, pts in self._samples.items()
            }
        for r in repos:
            stats = latest.get(r["id"])
            spark = sparks.get(r["id"], [])
            # Downsample the 30-min spark to ≤40 points.
            if len(spark) > 40:
                step = len(spark) / 40.0
                spark = [spark[int(i * step)] for i in range(40)]
            project = max(r["projects"], key=r["projects"].get) \
                if r["projects"] else r["name"]
            out.append({
                "id": r["id"], "path": r["path"], "name": r["name"],
                "project": project,
                "sessions": r["sessions"], "live": r["live"],
                "cost": r["cost"],
                "last_activity": r["last_activity"].isoformat()
                if r["last_activity"] else None,
                "stats": stats, "spark": spark,
            })
            if stats:
                tot["wip"] += stats["wip"]
                tot["staged"] += stats["staged_add"]
                tot["unstaged"] += stats["unstaged_add"]
                tot["untracked"] += stats["untracked_lines"]
                tot["committed_today"] += stats["committed_add"]
                tot["commits_today"] += stats["commits_today"]
                tot["dirty"] += 1 if stats["wip"] > 0 else 0
        # Newest activity first within equal wip/live — two stable passes,
        # since an ISO string can't be negated inside one key tuple.
        out.sort(key=lambda r: r["last_activity"] or "", reverse=True)
        out.sort(key=lambda r: (
            -(r["stats"]["wip"] if r["stats"] else 0), -r["live"],
        ))
        return {
            "repos": out, "totals": tot, "now": now,
            "interval": self.interval,
            "sampling_since": self.started_at,
        }

    def history(self, repo_sel: str, range_s: int,
                days: Optional[int] = None) -> dict:
        """WIP + committed series and commit markers over the trailing window.

        WIP comes from the in-memory samples (buckets with no sample within the
        forward-fill TTL are null, so gaps render as gaps). Committed is the
        cumulative lines added by commits inside the window, rebuilt from
        ``git log`` — exact regardless of when the server started.
        """
        now = time.time()
        start = now - range_s
        repos = self._shown(days)
        if repo_sel != "all":
            repos = [r for r in repos if r["id"] == repo_sel]
        rids = [r["id"] for r in repos]

        width = max(range_s / CHART_BUCKETS, self.interval)
        nb = int(range_s // width) + 1
        with self._lock:
            series = [list(self._samples.get(rid, [])) for rid in rids]

        # WIP: bucket, forward-fill with a TTL so long gaps stay honest.
        grids = []
        for pts in series:
            arr: List[Optional[Tuple[float, int]]] = [None] * nb
            last_before = None
            for pt in pts:
                if pt[0] < start:
                    last_before = pt
                    continue
                b = min(int((pt[0] - start) // width), nb - 1)
                arr[b] = pt
            grids.append((arr, last_before))
        ttl = max(600.0, 3 * width, _IDLE_EVERY * self.interval + 30)
        carry: List[Optional[Tuple[float, int]]] = [lb for _a, lb in grids]

        # Committed: cumulative additions from commit events in the window.
        events: List[Tuple[float, int]] = []
        markers: List[dict] = []
        for r in repos:
            for c in self._commits(r["id"], r["path"]):
                if c["t"] >= start:
                    events.append((c["t"], c["add"]))
                    markers.append({"t": c["t"], "hash": c["hash"],
                                    "subject": c["subject"], "repo": r["name"],
                                    "add": c["add"]})
        events.sort()
        markers.sort(key=lambda m: m["t"])

        points = []
        ei = 0
        committed = 0
        for b in range(nb):
            t_bucket = start + (b + 1) * width
            while ei < len(events) and events[ei][0] <= t_bucket:
                committed += events[ei][1]
                ei += 1
            wip = 0
            have = False
            for i, (arr, _lb) in enumerate(grids):
                if arr[b] is not None:
                    carry[i] = arr[b]
                c = carry[i]
                if c is not None and t_bucket - c[0] <= ttl:
                    wip += c[1]
                    have = True
            points.append({
                "t": round(t_bucket, 1),
                "wip": wip if have else None,
                "committed": committed,
            })
        return {"points": points, "start": start, "end": now,
                "commits": markers[-150:]}
