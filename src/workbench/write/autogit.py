"""Auto-git for the case workdir: every change is committed in the background, attributed.

Once the working tree has been quiet for IDLE seconds, pending changes are committed in up to
three commits: the user's saves (``user: …``), everything else (``agent: …``) and the notes
file (``notes: …``). Who changed a file comes from the Write workspace: a save from the editor
is the user's; anything else that changed on disk is the agent's. Agents that commit their own
work leave nothing behind for this to do.

It stands back while git is busy (a merge, rebase, cherry-pick or an index.lock) and while the
index holds changes someone else staged, so an agent mid-commit is never overtaken.

``tag(...)`` marks a version: an annotated ``state/<date>-<slug>`` tag on HEAD, after committing
what is pending. Older ``submitted/`` tags are still listed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

IDLE = 5.0
PERIOD = 1.5
NOTES = {".treenotes.json", "notes/.treenotes.json"}


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


DANISH = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "Æ": "Ae", "Ø": "Oe", "Å": "Aa"})


def slugify(s: str) -> str:
    s = s.translate(DANISH)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s.strip().lower()).strip("-.")
    return s[:60] or "version"


def summary(paths: list[str]) -> str:
    if len(paths) == 1:
        return paths[0]
    shown = ", ".join(paths[:3])
    return f"{len(paths)} files ({shown}{', …' if len(paths) > 3 else ''})"


class AutoGit:
    def __init__(self, ws, idle: float = IDLE, period: float = PERIOD):
        self.ws = ws
        self.root: Path = ws.root
        self.idle, self.period = idle, period
        self.lock = threading.Lock()
        self.sig = ""
        self.since = 0.0
        self.error = ""
        self.last: dict | None = None
        self.enabled = self.is_repo()
        self.stopped = threading.Event()

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="autogit", daemon=True).start()

    def stop(self) -> None:
        self.stopped.set()

    # ---------- the loop ----------
    def busy(self) -> bool:
        g = self.root / ".git"
        return any((g / n).exists() for n in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
                                               "REVERT_HEAD", "index.lock"))

    def status(self) -> list[tuple[str, str]]:
        """(XY, path) for every change, renames as their new path."""
        raw = git(self.root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        out, parts, i = [], raw.split("\0"), 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            if xy[0] in "RC":
                i += 1  # the old name follows
            out.append((xy, path))
        return out

    def _loop(self) -> None:
        while not self.stopped.wait(self.period):
            try:
                self.tick()
            except Exception as e:  # never let the thread die; say what broke
                self.error = str(e)
                print(f"workbench autogit: {e}", file=sys.stderr, flush=True)

    def tick(self, now: float | None = None) -> dict | None:
        now = time.monotonic() if now is None else now
        if not self.enabled:
            self.enabled = self.is_repo()
            return None
        if self.busy():
            return None
        changes = self.status()
        if not changes:
            self.sig = ""
            return None
        sig = repr(changes) + repr(self.mtimes([p for _, p in changes]))
        if sig != self.sig:
            self.sig, self.since = sig, now
            return None
        if now - self.since < self.idle:
            return None
        if any(xy[0] not in " ?" for xy, _ in changes):
            return None  # someone staged changes: they are committing; leave it to them
        return self.commit_pending([p for _, p in changes])

    def mtimes(self, paths: list[str]) -> list[float]:
        out = []
        for p in paths:
            try:
                out.append((self.root / p).stat().st_mtime)
            except OSError:
                out.append(0.0)
        return out

    def who(self, path: str) -> str:
        if path in NOTES:
            return "notes"
        ch = (getattr(self.ws, "changes", None) or {}).get(path)
        if ch and ch.get("by") == "you":
            return "user"
        by = getattr(self.ws, "_by", None)  # a save from the editor in the last few seconds, seen or not
        return "user" if by and by(path) == "you" else "agent"

    def commit_pending(self, paths: list[str] | None = None) -> dict | None:
        with self.lock:
            if paths is None:
                paths = [p for _, p in self.status()]
            if not paths:
                return None
            groups: dict[str, list[str]] = {"user": [], "agent": [], "notes": []}
            for p in paths:
                groups[self.who(p)].append(p)
            done = None
            for who in ("user", "agent", "notes"):
                if not groups[who]:
                    continue
                git(self.root, "add", "-A", "--", *groups[who])
                msg = "notes: annotations and feedback" if who == "notes" else f"{who}: {summary(groups[who])}"
                git(self.root, "commit", "-q", "--no-verify", "-m", msg, "--", *groups[who])
                done = {"who": who, "msg": msg, "paths": groups[who]}
            self.sig, self.error = "", ""
            self.last = done
            return done

    # ---------- what the page shows ----------
    def state(self) -> dict:
        if not self.is_repo():
            return {"repo": False}
        head = git(self.root, "log", "-1", "--format=%h%x00%s%x00%ct", check=False).strip().split("\0")
        count = git(self.root, "rev-list", "--count", "HEAD", check=False).strip()
        branch = git(self.root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        pending = len(self.status()) if not self.busy() else -1
        return {
            "repo": True,
            "branch": branch,
            "commits": int(count) if count.isdigit() else 0,
            "last": {"hash": head[0], "msg": head[1], "at": int(head[2])} if len(head) == 3 else None,
            "pending": pending,
            "busy": self.busy(),
            "tags": self.tags(),
            "error": self.error,
        }

    def log(self, n: int = 30) -> list[dict]:
        raw = git(self.root, "log", f"-{n}", "--format=%h%x00%s%x00%ct%x00%D", check=False)
        out = []
        for line in raw.splitlines():
            h, s, t, refs = [*line.split("\0"), "", "", "", ""][:4]
            tags = [r.removeprefix("tag: ") for r in refs.split(", ") if r.startswith(("tag: state/", "tag: submitted/"))]
            who = s.split(":", 1)[0] if ":" in s else ""
            out.append({"hash": h, "msg": s, "at": int(t or 0), "who": who, "tags": tags})
        return out

    def tags(self) -> list[dict]:
        raw = git(self.root, "tag", "-l", "state/*", "submitted/*", "--sort=-creatordate",
                  "--format=%(refname:short)%00%(if)%(*objectname)%(then)%(*objectname:short)%(else)%(objectname:short)%(end)%00%(creatordate:unix)%00%(contents:subject)",
                  check=False)
        out = []
        for line in raw.splitlines():
            name, h, t, subj = [*line.split("\0"), "", "", "", ""][:4]
            out.append({"name": name, "hash": h, "at": int(t or 0), "msg": subj})
        return out

    def tag(self, label: str, message: str = "") -> dict:
        """Tag HEAD (committing what is pending first)."""
        if not self.is_repo():
            raise RuntimeError("the workdir is not a git repository")
        if self.busy():
            raise RuntimeError("git is busy (a merge or rebase is in progress)")
        self.commit_pending()
        name = f"state/{date.today().isoformat()}-{slugify(label)}"
        if git(self.root, "tag", "-l", name, check=False).strip():
            raise RuntimeError(f"tag {name} already exists")
        git(self.root, "tag", "-a", name, "-m", message or label)
        return {"name": name, "hash": git(self.root, "rev-parse", "--short", "HEAD").strip()}
