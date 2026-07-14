"""Read-only Git monitor for the target repository.

Hard safety rule: this module only ever runs *read-only* git commands. It never
runs reset, clean, checkout -f, stash, commit, push or any history rewrite. The
builder observes repository truth; Claude is the only actor that changes the
target repository, and it commits its own work.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from autonomous_builder.models import GitState

# Commands this module is permitted to run. Anything not read-only is a bug.
_ALLOWED_FIRST_ARGS = {
    "rev-parse", "status", "branch", "symbolic-ref", "remote", "rev-list",
    "log", "config", "diff", "show", "cat-file", "ls-files",
}


class GitError(Exception):
    pass


class GitMonitor:
    """Observe (never mutate) a target git repository."""

    def __init__(self, root: str | Path, *, timeout: float = 30.0):
        self.root = Path(root).expanduser()
        self.timeout = timeout

    # -- low-level ----------------------------------------------------------
    def _run(self, *args: str) -> tuple[int, str, str]:
        if not args or args[0] not in _ALLOWED_FIRST_ARGS:
            raise GitError(f"refusing to run non-read-only git command: git {' '.join(args)}")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:  # git not installed
            raise GitError(f"git executable not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing
            raise GitError(f"git command timed out: git {' '.join(args)}") from exc
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def _ok(self, *args: str) -> Optional[str]:
        code, out, _ = self._run(*args)
        return out if code == 0 else None

    # -- public read-only queries ------------------------------------------
    def is_git_repo(self) -> bool:
        if not self.root.exists():
            return False
        code, out, _ = self._run("rev-parse", "--is-inside-work-tree")
        return code == 0 and out == "true"

    def toplevel(self) -> Optional[str]:
        return self._ok("rev-parse", "--show-toplevel")

    def current_branch(self) -> Optional[str]:
        # returns "HEAD" when detached
        return self._ok("rev-parse", "--abbrev-ref", "HEAD")

    def head(self) -> Optional[str]:
        return self._ok("rev-parse", "HEAD")

    def origin_url(self) -> Optional[str]:
        return self._ok("remote", "get-url", "origin")

    def is_dirty(self) -> bool:
        out = self._ok("status", "--porcelain")
        return bool(out)

    def porcelain(self) -> list[str]:
        out = self._ok("status", "--porcelain")
        return [l for l in (out or "").splitlines() if l.strip()]

    def ahead_behind(self, upstream: str = "@{upstream}") -> tuple[int, int]:
        out = self._ok("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if not out:
            return (0, 0)
        try:
            ahead_s, behind_s = out.split()
            return int(ahead_s), int(behind_s)
        except ValueError:
            return (0, 0)

    def commit_exists(self, sha: str) -> bool:
        if not sha:
            return False
        code, out, _ = self._run("cat-file", "-t", sha)
        return code == 0 and out == "commit"

    def is_ancestor(self, maybe_ancestor: str, descendant: str = "HEAD") -> bool:
        """True if *maybe_ancestor* is contained in the history of *descendant*.

        Implemented with a read-only ``rev-list`` containment check (merge-base
        is deliberately outside the read-only allow-list).
        """
        if not maybe_ancestor:
            return False
        out = self._ok("rev-list", descendant)
        if not out:
            return False
        shas = out.split()
        return any(s == maybe_ancestor or s.startswith(maybe_ancestor) for s in shas)

    def last_commit_message(self) -> Optional[str]:
        return self._ok("log", "-1", "--pretty=%B")

    def last_commit_files(self) -> list[str]:
        out = self._ok("show", "--name-only", "--pretty=format:", "HEAD")
        return [l for l in (out or "").splitlines() if l.strip()]

    # -- snapshot -----------------------------------------------------------
    def snapshot(self) -> GitState:
        """Capture a full read-only GitState snapshot; never raises."""
        state = GitState(root=str(self.root))
        try:
            state.exists = self.root.exists()
            if not state.exists:
                state.error = "target root does not exist"
                return state
            state.is_repo = self.is_git_repo()
            if not state.is_repo:
                state.error = "not a git repository"
                return state
            state.branch = self.current_branch()
            state.head = self.head()
            state.origin_url = self.origin_url()
            porcelain = self.porcelain()
            state.dirty = bool(porcelain)
            changed, untracked = [], []
            for line in porcelain:
                # porcelain format: XY <path>
                status = line[:2]
                path = line[3:].strip()
                if status.strip() == "??":
                    untracked.append(path)
                else:
                    changed.append(path)
            state.changed_files = changed
            state.untracked_files = untracked
            ahead, behind = self.ahead_behind()
            state.ahead, state.behind = ahead, behind
        except GitError as exc:  # pragma: no cover - defensive
            state.error = str(exc)
        return state
