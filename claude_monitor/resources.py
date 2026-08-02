"""Attribute OS-level resource usage to Claude Code sessions.

Claude Code appends to its transcript and closes the handle, so a process holds
no open file descriptor we could match on. Instead we match on the working
directory, which every transcript records, and disambiguate by recency.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency in practice
    psutil = None

from .models import Session


class ProcessInfo:
    """A live Claude Code process."""

    __slots__ = ("pid", "cwd", "created", "cpu_percent", "rss", "threads", "cmdline")

    def __init__(self, pid: int, cwd: str, created: datetime, cmdline: str):
        self.pid = pid
        self.cwd = cwd
        self.created = created
        self.cmdline = cmdline
        self.cpu_percent = 0.0
        self.rss = 0
        self.threads = 0


class ResourceMonitor:
    """Discovers Claude processes and keeps CPU sampling state between refreshes.

    ``psutil.Process.cpu_percent()`` reports usage since the previous call on
    the same object, so the Process handles are cached across refreshes; the
    first sample for any process necessarily reads 0.
    """

    def __init__(self) -> None:
        self._procs: Dict[int, "psutil.Process"] = {}

    @property
    def available(self) -> bool:
        return psutil is not None

    def _is_claude(self, proc) -> bool:
        try:
            name = (proc.name() or "").lower()
            if name == "claude":
                return True
            cmdline = proc.cmdline()
        except Exception:
            return False
        if not cmdline:
            return False
        exe = cmdline[0].lower()
        # Match the CLI itself, not our own monitor or unrelated node scripts.
        if exe.endswith("/claude") or exe == "claude":
            return True
        return False

    def scan(self) -> List[ProcessInfo]:
        """Return every currently running Claude Code process."""
        if psutil is None:
            return []

        found: List[ProcessInfo] = []
        seen: set = set()

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if not self._is_claude(proc):
                    continue
                pid = proc.pid
                seen.add(pid)
                handle = self._procs.get(pid)
                if handle is None:
                    handle = proc
                    self._procs[pid] = handle
                    handle.cpu_percent(None)  # prime the sampler

                try:
                    cwd = handle.cwd() or ""
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    cwd = ""

                info = ProcessInfo(
                    pid=pid,
                    cwd=cwd,
                    created=datetime.fromtimestamp(handle.create_time(), tz=timezone.utc),
                    cmdline=" ".join(handle.cmdline()[:4]),
                )
                info.cpu_percent = handle.cpu_percent(None)
                mem = handle.memory_info()
                info.rss = mem.rss
                info.threads = handle.num_threads()
                found.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        # Forget handles for processes that have exited.
        for pid in list(self._procs):
            if pid not in seen:
                del self._procs[pid]

        return found

    def attach(self, sessions: List[Session]) -> List[ProcessInfo]:
        """Bind live processes to their sessions, mutating the sessions in place.

        A process is matched to the most recently active session whose recorded
        ``cwd`` equals the process's working directory and whose activity
        extends past the process start time. Each session is claimed at most
        once, so N concurrent sessions in one directory map to N processes.
        """
        procs = self.scan()
        if not procs:
            return []

        by_cwd: Dict[str, List[Session]] = {}
        for s in sessions:
            if s.cwd:
                by_cwd.setdefault(s.cwd.rstrip("/"), []).append(s)
        for group in by_cwd.values():
            group.sort(key=lambda s: s.ended or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        claimed: set = set()
        # Newest process first, so it gets the newest matching session.
        for info in sorted(procs, key=lambda p: p.created, reverse=True):
            candidates = by_cwd.get(info.cwd.rstrip("/"), [])
            chosen: Optional[Session] = None
            for sess in candidates:
                if sess.session_id in claimed:
                    continue
                # The session must show activity at or after the process began.
                if sess.ended and sess.ended >= info.created:
                    chosen = sess
                    break
            if chosen is None:
                # Fall back to the newest unclaimed session in that directory.
                for sess in candidates:
                    if sess.session_id not in claimed:
                        chosen = sess
                        break
            if chosen is None:
                continue

            claimed.add(chosen.session_id)
            chosen.pid = info.pid
            chosen.cpu_percent = info.cpu_percent
            chosen.rss_bytes = info.rss
            chosen.num_threads = info.threads
            chosen.proc_started = info.created

        return procs


def system_snapshot() -> dict:
    """Machine-wide CPU and memory, for context alongside per-session numbers."""
    if psutil is None:
        return {}
    try:
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(None),
            "cpu_count": psutil.cpu_count(),
            "mem_total": vm.total,
            "mem_used": vm.used,
            "mem_percent": vm.percent,
            "load": tuple(round(x, 2) for x in psutil.getloadavg()),
        }
    except Exception:
        return {}


def fmt_bytes(n: float) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"
