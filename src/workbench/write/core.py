# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
"""
Write tab: edit and annotate a case workdir in the browser.

    jura write print PATH  # print the annotated tree (for agents, READMEs)
    jura write fb ls       # open feedback, for the agent to act on
    jura write edits       # the user's saved and unsaved edits
    jura write context     # the user's current selection

What it does
  * Shows PATH as a tree (line/word counts, symlink targets, empty folders);
    names are coloured by document size: word count ranked among all files (folders among folders).
    The window can instead colour by recency, open feedback or annotation length, and can list
    recent files. `jura write print` on a terminal stays on document size. The header box filters
    that tree; Enter searches inside the files (the same files the tree shows).
  * Edit any text file; Ctrl/Cmd+S saves. Symlinked files edit their target.
  * Plot files (png, jpg, gif, webp, bmp, tiff, avif, svg, pdf) open in a viewer.
    SVG can still be edited as source.
  * Annotate any file or folder (including the root): your standing comments for the agent,
    which it reads but never writes. Leave feedback
    for an agent on a path, on lines of a file, or on several paths; it is open or
    done and the agent replies via `jura write fb`. Everything lives in
    PATH/.juranotes.json, or PATH/notes/.juranotes.json when a notes folder is
    present: a flat {"relative/path": "annotation"} map, or
    {"notes": {...}, "feedback": [...]} once there is feedback.
  * Watches the disk: when an agent (or anything else) changes the tree, the
    view refreshes. Line ages follow the content (a later edit does not restart
    the fade of lines it left alone) and are kept for an hour, including across
    a restart. An open file reloads silently if you have no unsaved edits;
    otherwise you get a conflict banner with compare / load disk / overwrite.
  * Saves are atomic and check that the file hasn't changed since you opened it.
  * Create, rename/move and delete files and folders; notes follow renames.
    Notes whose path disappeared are listed so you can reattach or drop them.

Built on treeparse (Python 3.12+): `jura write --stub` / `-j` describe it. Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import difflib
import fnmatch
from bisect import bisect_left, bisect_right
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import ClassVar, List, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from treeparse import argument, cli, command, group, option

from workbench.paths import state_dir

from . import __version__
from .term import Sessions, ws_accept

DEFAULT_IGNORE = [
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    ".DS_Store",
    "*.pyc",
    ".treedit-*",
    ".ruff_cache",
    "target",
]
NOTES_NAME = ".juranotes.json"
OLD_NOTES_NAME = ".treenotes.json"  # the name up to 0.1.0a2, renamed in place on first use
DEFAULT_SHOW = ["notes"]  # a private notes/ repo is usually git-ignored by the project, but belongs in the tree


def notes_file(root: Path) -> Path:
    """Where ROOT keeps annotations and feedback.

    A notes/ folder is usually its own git repo, ignored by the product. The file lives there when
    that folder is present, so it is not at the product root. A file already at the root moves across
    when notes/ has none yet. When both files exist, the root one stays in use.
    """
    folder = root / "notes"
    for d in (root, folder):
        old, new = d / OLD_NOTES_NAME, d / NOTES_NAME
        if old.is_file() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                pass
    home = folder / NOTES_NAME
    legacy = root / NOTES_NAME
    if not folder.is_dir():
        return legacy
    if legacy.is_file() and not home.exists():
        try:
            legacy.rename(home)
        except OSError:
            return legacy
        return home
    if legacy.is_file():
        return legacy
    return home


MAX_TEXT = 2 * 1024 * 1024  # files above this are shown but not edited
RAW_MAX = 64 * 1024 * 1024  # a plot larger than this is listed, but not sent to the viewer
GREP_LIMIT = 200  # content-search hits returned in one go
PLOT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "avif": "image/avif",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
}


class HTTPError(Exception):
    def __init__(self, status: int, msg: str, extra: dict | None = None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def decode(data: bytes):
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def plot_mime(name: str) -> str | None:
    """Content type when NAME is a plot the window can show, else None."""
    return PLOT_MIME.get(Path(name).suffix.lower().lstrip("."))


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".treedit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            shutil.copymode(str(path), tmp)
        except OSError:
            pass
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


QUOTE_LINES = 12  # a line comment remembers (at most) this many of its lines to re-find them after edits


def locate(span: list, quote: str, text: str) -> tuple:
    """(first, last, stale): the comment's span if its quote is still there, else the nearest
    place the quote moved to, else the old span marked stale."""
    a, b = span
    have, q = text.split("\n"), quote.split("\n")
    if have[a - 1 : a - 1 + len(q)] == q:
        return a, b, False
    hits = [i + 1 for i in range(len(have) - len(q) + 1) if have[i : i + len(q)] == q]
    if not hits:
        return a, b, True
    na = min(hits, key=lambda i: abs(i - a))
    return na, na + (b - a), False


def first_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[0]


def _rel_join(prefix: str, rel: str) -> str:
    rel = (rel or "").replace("\\", "/").removeprefix("./")
    if rel in ("", "."):
        return prefix
    return f"{prefix}/{rel}" if prefix else rel


def _snippet(text: str, query: str, case: bool, width: int = 200) -> str:
    """The matching line, shortened so the match stays in view."""
    text = text.replace("\t", "    ").strip()
    if len(text) <= width:
        return text
    hay = text if case else text.casefold()
    i = hay.find(query if case else query.casefold())
    if i < 0:
        return text[:width] + "…"
    start = max(0, i - 40)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def fb_label(f: dict) -> str:
    """'! #3 L10-14' - how open feedback is flagged in text listings."""
    where = ""
    if f.get("lines"):
        a, b = f["lines"]
        where = f" L{a}" + (f"-{b}" if b != a else "") + (" (moved?)" if f.get("stale") else "")
    return f"! #{f['id']}{where}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


CHARS_PER_TOKEN = 4  # a rough estimate for English text and code


def skill_tokens(tree: dict) -> None:
    """In a skill tree (one holding a SKILL.md), estimate what loading costs. Files get "tok". Folders
    get "tmin" - their SKILL.md alone, no leaf loaded (a folder without one: the entry SKILL.md of each
    skill inside) - and "tmax", every text file underneath. The root gets "skills": True."""

    def has_skill(n: dict) -> bool:
        return any(
            (c["type"] == "file" and c["name"] == "SKILL.md") or (c["type"] == "dir" and has_skill(c))
            for c in n.get("children") or []
        )

    if not has_skill(tree):
        return

    def walk(n: dict, inside: bool) -> tuple:  # inside: within a skill folder
        if n["type"] != "dir":
            n["tok"] = -(-n.get("chars", 0) // CHARS_PER_TOKEN) if n.get("kind") == "text" else 0
            return 0, n["tok"]
        kids = n.get("children") or []
        entry = next((c for c in kids if c["type"] == "file" and c["name"] == "SKILL.md"), None)
        sums = [walk(c, inside or entry is not None) for c in kids]
        lo = entry["tok"] if entry else sum(a for c, (a, _) in zip(kids, sums, strict=True) if c["type"] == "dir")
        hi = sum(b for _, b in sums)
        if inside or has_skill(n):  # folders unrelated to skills keep their word count
            n["tmin"], n["tmax"] = lo, hi
        return lo, hi

    walk(tree, False)
    tree["skills"] = True


def fmt_tokens(n: int) -> str:
    return f"{n}" if n < 1000 else f"{n / 1000:.1f}k" if n < 10000 else f"{round(n / 1000)}k"


def word_totals(tree: dict) -> dict:
    """{path: words}, folders summing their children (as the web view's heat does)."""
    out: dict = {}

    def walk(n: dict) -> int:
        w = sum(walk(c) for c in n.get("children") or []) if n["type"] == "dir" else n.get("words") or 0
        out[n["path"]] = w
        return w

    walk(tree)
    return out


def dir_paths(tree: dict) -> set:
    out: set = set()

    def walk(n: dict) -> None:
        if n["type"] == "dir":
            out.add(n["path"])
            for c in n.get("children") or []:
                walk(c)

    walk(tree)
    return out


def rank_by_size(words: dict, dirs: set) -> dict:
    """{path: 0..1}: how big each document is in this tree - its word count ranked among all files
    (folders among all folders), ties sharing a rank; small sets stay near neutral (0.5)."""
    groups: dict = {}
    for path, w in words.items():
        if path and w:
            groups.setdefault(path in dirs, []).append(w)
    sizes = {k: sorted(v) for k, v in groups.items()}
    out = {"": 0.5}  # the root: default colour
    for path, w in words.items():
        ws = sizes.get(path in dirs)
        if not path or not w or not ws:
            continue
        rank = 1.0 if len(ws) == 1 else (bisect_left(ws, w) + bisect_right(ws, w) - 1) / 2 / (len(ws) - 1)
        out[path] = 0.5 + (rank - 0.5) * min(1.0, (len(ws) - 1) / 8)
    return out


HEAT_RAMP = ((0.3, 245), (0.7, None), (0.88, 214), (1.01, 196))  # size rank -> 256-colour (None = default)


def heat_ansi(rank, bold: bool) -> str:
    """ANSI start code: dim for the lower ranks of a level, default colour in the middle, amber then red
    at the top; dark grey for items without words (rank None)."""
    code = 240 if rank is None else next(c for lim, c in HEAT_RAMP if rank < lim)
    parts = (["1"] if bold else []) + ([f"38;5;{code}"] if code else [])
    return f"\033[{';'.join(parts)}m" if parts else "\033[0m"


class Workspace:
    def __init__(
        self,
        root: Path,
        notes_path: Path,
        ignore: List[str],
        follow_outside: bool,
        gitignore: bool = True,
        show: Optional[List[str]] = None,
    ):
        self.root = root.resolve()
        self.notes_path = notes_path.resolve()
        self.ignore = ignore
        self.follow_outside = follow_outside
        self.lock = threading.RLock()
        self._info_cache: dict = {}
        self.gitignore = gitignore
        self.show = [k for k in ("/".join(self.parts(x)) for x in (show or [])) if k]
        self._git = (-1.0, frozenset())
        self.changes: dict = {}  # path -> {"at": epoch, "by": "agent" | "you"}: files that changed recently
        self._seen = None  # path -> (mtime, size) at the last scan; None until the first (baseline) scan
        self._ui = {}  # path -> when the editor itself wrote it, so that change is not the agent's
        self._texts: dict = {}  # path -> last text seen (bounded), to tell which lines a change touched
        self._text_bytes = 0
        self.line_changes: dict = {}  # path -> [{"at", "by", "ranges": [[first, last, add|chg|del], ...]}]
        state = state_dir()
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:16]
        self.drafts_path = state / f"{digest}-drafts.json"
        self.history_path = state / f"{digest}-history.jsonl"  # one JSON event per line, kept for CHANGE_TTL
        old = state.parent / "treedit"  # where releases up to 0.1.0a2 kept them; copied over once
        for new in (self.drafts_path, self.history_path):
            if not new.exists() and (old / new.name).is_file():
                try:
                    state.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old / new.name, new)
                except OSError:
                    pass
        self._load_history()

    # ---------- paths ----------
    def ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in self.ignore)

    def kept(self, rel: str) -> bool:
        return any(rel == k or rel.startswith(k + "/") for k in self.show)

    def git_ignored(self) -> frozenset:
        """Paths (relative to the root) that .gitignore hides, as git itself reports them, minus
        --show paths (where a nested repo's own .gitignore still applies). Cached for a second."""
        if not self.gitignore:
            return frozenset()
        at, hit = self._git
        if time.monotonic() - at < 1.0:
            return hit
        out: set = set()
        self._git_collect(self.root, "", out)
        for k in self.show:
            if (self.root / k / ".git").exists():
                self._git_collect(self.root / k, k + "/", out)
        hit = frozenset(out)
        self._git = (time.monotonic(), hit)
        return hit

    def _git_collect(self, d: Path, prefix: str, out: set) -> None:
        try:
            r = subprocess.run(
                ["git", "-C", str(d), "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if r.returncode != 0:  # not a git work tree, or no git
            return
        for raw in r.stdout.split(b"\0"):
            rel = prefix + os.fsdecode(raw).rstrip("/")
            if raw and not (prefix == "" and self.kept(rel)):
                out.add(rel)

    def inside(self, p: Path) -> bool:
        try:
            p.relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def parts(rel: str) -> list[str]:
        parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise HTTPError(400, "paths may not contain '..'")
        return parts

    def norm(self, rel: str) -> str:
        return "/".join(self.parts(rel))

    def node_path(self, rel: str) -> Path:
        """The directory entry itself (a symlink is not followed)."""
        parts = self.parts(rel)
        p = self.root.joinpath(*parts)
        if parts and not self.inside(p.parent.resolve()):
            raise HTTPError(403, "path is outside the root")
        return p

    def target_path(self, rel: str) -> Path:
        """Where content is read/written (symlinks followed)."""
        t = self.node_path(rel).resolve()
        if not self.follow_outside and not self.inside(t):
            raise HTTPError(403, "symlink points outside the root (start with --follow-outside to allow)")
        return t

    # ---------- scanning ----------
    TEXT_KEEP, TEXT_BUDGET = 512 * 1024, 64 * 1024 * 1024  # per file / in total, for line-change tracking

    def _by(self, rel: str) -> str:
        now = time.time()
        return (
            "you"
            if any((rel == k or rel.startswith(k + "/")) and now - t < 15 for k, t in self._ui.items())
            else "agent"
        )

    def _remember_text(self, rel: str, text: str, at: float) -> None:
        old = self._texts.get(rel)
        if old is not None and old != text:
            self._line_change(rel, old, text, at)
        self._text_bytes -= len(old) if old is not None else 0
        if len(text) <= self.TEXT_KEEP and self._text_bytes + len(text) <= self.TEXT_BUDGET:
            self._texts[rel] = text
            self._text_bytes += len(text)
        else:
            self._texts.pop(rel, None)

    def _line_change(self, rel: str, old: str, new: str, at: float) -> None:
        """Record the lines (numbered in the new text) that a change added, changed or deleted, and
        carry earlier records through this change so they keep pointing at the same lines."""
        ops = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False).get_opcodes()
        n_new = new.count("\n") + 1
        ranges = []
        for tag, _i1, _i2, j1, j2 in ops:
            if tag in ("replace", "insert"):
                ranges.append([j1 + 1, j2, "chg" if tag == "replace" else "add"])
            elif tag == "delete":
                k = min(j1 + 1, n_new)
                ranges.append([k, k, "del"])
        if not ranges:
            return

        def remap(line: int):  # old 1-based line -> new 1-based line, or None when it is gone
            i = line - 1
            for tag, i1, i2, j1, _j2 in ops:
                if i1 <= i < i2:
                    return j1 + (i - i1) + 1 if tag == "equal" else None
            return None

        events = []
        for ev in self.line_changes.get(rel, []):
            kept = []
            for a, b, kind in ev["ranges"]:
                moved = [m for m in (remap(x) for x in range(a, b + 1)) if m]
                if moved:
                    kept.append([min(moved), max(moved), kind])
            if kept:
                events.append({**ev, "ranges": kept})
        by = self._by(rel)
        events.append({"at": at, "by": by, "ranges": ranges})
        self.line_changes[rel] = [e for e in events if time.time() - e["at"] < self.CHANGE_TTL][-20:]
        self._append_history(rel, at, by, ranges)

    def text_info(self, path: Path, st, rel: str = "") -> dict:
        key = (str(path), st.st_mtime_ns, st.st_size)
        hit = self._info_cache.get(key)
        if hit is not None:
            return hit
        mime = plot_mime(rel or path.name)
        if mime:
            info = {"kind": "plot", "mime": mime}  # shown in the viewer; the bytes stay on disk
        elif st.st_size > MAX_TEXT:
            info = {"kind": "large"}
        else:
            try:
                text = decode(path.read_bytes())
            except OSError:
                text = None
            if text is None:
                info = {"kind": "binary"}
            else:
                info = {"kind": "text", "lines": count_lines(text), "words": len(text.split()), "chars": len(text)}
                if rel:
                    with self.lock:
                        self._remember_text(rel, text.replace("\r\n", "\n"), min(time.time(), st.st_mtime))
        if len(self._info_cache) > 20000:
            self._info_cache.clear()
        self._info_cache[key] = info
        return info

    def scan(self) -> dict:
        hidden = self.git_ignored()

        def walk(d: Path, rel: str) -> list:
            out = []
            try:
                entries = list(os.scandir(d))
            except OSError:
                return out
            for e in entries:
                if self.ignored(e.name):
                    continue
                r = f"{rel}/{e.name}" if rel else e.name
                if r in hidden:
                    continue
                p = Path(e.path)
                if p == self.notes_path:
                    continue
                node: dict = {"name": e.name, "path": r}
                is_link = e.is_symlink()
                if is_link:
                    try:
                        node["link"] = os.readlink(e.path)
                    except OSError:
                        node["link"] = "?"
                try:
                    st = os.stat(e.path)
                except OSError:
                    node["type"] = "broken"
                    out.append(node)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    node["type"] = "dir"
                    if is_link:
                        node["linked"] = True
                        node["children"] = []
                    else:
                        node["children"] = walk(p, r)
                else:
                    node["type"] = "file"
                    node["size"] = st.st_size
                    node["mtime"] = st.st_mtime
                    target = p.resolve()
                    if is_link and not self.follow_outside and not self.inside(target):
                        node["kind"] = "outside"
                    else:
                        node.update(self.text_info(target, st, r))
                out.append(node)
            out.sort(key=lambda n: (n["type"] != "dir", n["name"].lower()))
            return out

        tree = {"name": self.root.name, "path": "", "type": "dir", "children": walk(self.root, "")}
        skill_tokens(tree)
        self._track(tree)
        return tree

    CHANGE_TTL = 3600.0  # seconds a change is remembered (the page fades it out sooner)
    HISTORY_CAP = 256 * 1024  # rewrite the log once it passes this many bytes
    HISTORY_KEEP = 2000

    def _parse_history(self, line: str):
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        if not isinstance(rec, dict):
            return None
        path, at = rec.get("path"), rec.get("at")
        if not isinstance(path, str) or not path or ".." in path.split("/") or isinstance(at, bool):
            return None
        if not isinstance(at, (int, float)):
            return None
        by = rec.get("by") if rec.get("by") in ("you", "agent") else "agent"
        ranges = []
        for item in rec.get("ranges") or []:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 3
                and isinstance(item[0], int)
                and not isinstance(item[0], bool)
                and isinstance(item[1], int)
                and not isinstance(item[1], bool)
                and item[2] in ("add", "chg", "del")
            ):
                ranges.append([item[0], item[1], item[2]])
        return {"path": path, "at": float(at), "by": by, "ranges": ranges}

    def _load_history(self) -> None:
        """Bring the last hour of edits back after a restart. Line numbers are as of each edit."""
        now = time.time()
        try:
            lines = self.history_path.read_text("utf-8").splitlines()
        except OSError:
            return
        kept = [rec for line in lines if (rec := self._parse_history(line)) and now - rec["at"] < self.CHANGE_TTL]
        for rec in kept[-self.HISTORY_KEEP :]:
            if rec["ranges"]:
                self.line_changes.setdefault(rec["path"], []).append(
                    {"at": rec["at"], "by": rec["by"], "ranges": rec["ranges"]}
                )
            prev = self.changes.get(rec["path"])
            if prev is None or rec["at"] >= prev["at"]:
                self.changes[rec["path"]] = {"at": rec["at"], "by": rec["by"]}
        for path, evs in list(self.line_changes.items()):
            fresh = [e for e in evs if now - e["at"] < self.CHANGE_TTL]
            self.line_changes[path] = fresh[-20:] if fresh else []
            if not fresh:
                del self.line_changes[path]
            else:
                newest = max(fresh, key=lambda e: e["at"])
                self.changes[path] = {"at": newest["at"], "by": newest["by"]}

    def _append_history(self, rel: str, at: float, by: str, ranges: list) -> None:
        rec = {"path": rel, "at": at, "by": by, "ranges": ranges}
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if self.history_path.stat().st_size > self.HISTORY_CAP:
                self._compact_history()
        except OSError:
            return

    def _compact_history(self) -> None:
        now = time.time()
        try:
            lines = self.history_path.read_text("utf-8").splitlines()
        except OSError:
            return
        kept = [rec for line in lines if (rec := self._parse_history(line)) and now - rec["at"] < self.CHANGE_TTL]
        data = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in kept[-self.HISTORY_KEEP :]).encode()
        try:
            atomic_write(self.history_path, data)
        except OSError:
            return

    def history(self) -> list:
        """Edits still in memory: line events, plus file-level marks for plots and other non-text."""
        out = []
        for path, evs in self.line_changes.items():
            for ev in evs:
                out.append({"path": path, "at": ev["at"], "by": ev["by"], "ranges": ev["ranges"]})
        for path, c in self.changes.items():
            if path not in self.line_changes:
                out.append({"path": path, "at": c["at"], "by": c["by"], "ranges": []})
        out.sort(key=lambda e: e["at"])
        return out

    def _track(self, tree: dict) -> None:
        """Compare file mtimes/sizes with the previous scan and remember what changed, and who did it:
        writes made through the editor are "you", anything else (the agent, another editor) "agent".
        A text file's age is its newest surviving line event, so a later edit does not restart the
        fade of lines it left alone. Plots and other files with no line text keep the mtime."""
        now, cur = time.time(), {}

        def walk(n: dict) -> None:
            for c in n.get("children") or []:
                if c["type"] == "file":
                    cur[c["path"]] = (c.get("mtime"), c.get("size"))
                walk(c)

        walk(tree)
        with self.lock:
            if self._seen is not None:
                self._ui = {k: t for k, t in self._ui.items() if now - t < 15}
                for path, sig in cur.items():
                    if self._seen.get(path) == sig:
                        continue
                    events = self.line_changes.get(path)
                    if events:
                        newest = max(events, key=lambda e: e["at"])
                        self.changes[path] = {"at": newest["at"], "by": newest["by"]}
                    else:
                        by = "you" if self._by(path) == "you" else "agent"
                        at = min(now, sig[0] or now)  # when it happened, even if nobody was looking
                        self.changes[path] = {"at": at, "by": by}
                        self._append_history(path, at, by, [])
            self._seen = cur
            self.changes = {k: v for k, v in self.changes.items() if k in cur and now - v["at"] < self.CHANGE_TTL}
            self.line_changes = {
                k: fresh
                for k, v in self.line_changes.items()
                if k in cur and (fresh := [e for e in v if now - e["at"] < self.CHANGE_TTL])
            }
            for k in [k for k in self._texts if k not in cur]:  # gone (or renamed): forget its text
                self._text_bytes -= len(self._texts.pop(k))

    def mark_ui(self, rel: str) -> None:
        with self.lock:
            self._ui[self.norm(rel)] = time.time()

    # ---------- drafts: unsaved edits, kept as diffs outside the project ----------
    def read_drafts(self) -> dict:
        try:
            data = json.loads(self.drafts_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_draft(self, rel: str, draft) -> None:
        rel = self.norm(rel)
        if not rel:
            raise HTTPError(400, "a path is required")
        with self.lock:
            drafts = self.read_drafts()
            if draft:
                drafts[rel] = {**draft, "at": time.time()}
            elif rel in drafts:
                del drafts[rel]
            else:
                return
            if drafts:
                atomic_write(
                    self.drafts_path, (json.dumps(drafts, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
                )
            else:
                try:
                    self.drafts_path.unlink()
                except OSError:
                    pass

    def version(self) -> str:
        """Cheap fingerprint of the tree (names, sizes, mtimes) plus the notes file."""
        h = hashlib.sha1()
        hidden = self.git_ignored()

        def walk(d: str, rel: str):
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                return
            for e in entries:
                if self.ignored(e.name) or f"{rel}/{e.name}"[1:] in hidden:
                    continue
                try:
                    st = e.stat(follow_symlinks=True)
                    sig = f"{st.st_mtime_ns}|{st.st_size}"
                except OSError:
                    sig = "broken"
                h.update(f"{rel}/{e.name}|{sig}|{e.is_symlink()}\n".encode("utf-8", "surrogateescape"))
                if e.is_dir(follow_symlinks=False):
                    walk(e.path, f"{rel}/{e.name}")

        walk(str(self.root), "")
        try:
            st = self.notes_path.stat()
            h.update(f"notes|{st.st_mtime_ns}|{st.st_size}".encode())
        except OSError:
            pass
        return h.hexdigest()[:16]

    # ---------- notes & feedback ----------
    # The store is a flat {"path": "annotation"} map, or {"notes": {...}, "feedback": [...]} once
    # feedback exists. Feedback entries: {"id", "paths", "text", "status": open|done, "reply",
    # "created", "updated"} plus "lines": [first, last] and "quote" for line comments on one file.
    def _load(self) -> dict:
        try:
            data = json.loads(self.notes_path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise HTTPError(500, f"cannot read {self.notes_path.name}: {e}") from e
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _wrapped(data: dict) -> bool:
        return isinstance(data.get("notes"), dict) or isinstance(data.get("feedback"), list)

    def read_notes(self) -> dict:
        data = self._load()
        if self._wrapped(data):
            data = data.get("notes") or {}
        return {str(k).strip("/") or ".": v for k, v in data.items() if isinstance(v, str)}

    def read_feedback(self) -> list:
        data = self._load()
        fb = data.get("feedback") if self._wrapped(data) else None
        return [f for f in fb if isinstance(f, dict) and isinstance(f.get("id"), int)] if isinstance(fb, list) else []

    def _save(self, notes: dict, feedback: list) -> None:
        clean = {k: v for k, v in sorted(notes.items()) if v.strip()}
        if not clean and not feedback and not self.notes_path.exists():
            return
        doc = {"notes": clean, "feedback": sorted(feedback, key=lambda f: f["id"])} if feedback else clean
        atomic_write(self.notes_path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def write_notes(self, notes: dict) -> None:
        with self.lock:
            self._save(notes, self.read_feedback())

    def set_note(self, rel: str, text: str) -> None:
        key = self.norm(rel) or "."
        with self.lock:
            notes = self.read_notes()
            if text.strip():
                notes[key] = text.rstrip()
            else:
                notes.pop(key, None)
            self.write_notes(notes)

    def move_notes(self, src: str, dst: str) -> None:
        src, dst = self.norm(src) or ".", self.norm(dst) or "."

        def under(k: str) -> bool:
            return k == src or (src != "." and k.startswith(src + "/"))

        with self.lock:
            notes, feedback, moved, changed = self.read_notes(), self.read_feedback(), {}, False
            for k, v in notes.items():
                if under(k):
                    nk = dst + k[len(src) :]
                    moved[nk] = (moved[nk] + "\n\n" + v) if nk in moved else v
                    changed = True
            for k in list(notes):
                if under(k):
                    del notes[k]
            for k, v in moved.items():
                notes[k] = (notes[k] + "\n\n" + v) if k in notes else v
            for f in feedback:
                new = sorted({dst + k[len(src) :] if under(k) else k for k in f.get("paths", [])})
                if new != f.get("paths"):
                    f["paths"], changed = new, True
            if changed:
                self._save(notes, feedback)

    def drop_notes(self, rel: str) -> None:
        """Remove annotations under REL. Feedback is kept: it may have asked for the deletion."""
        key = self.norm(rel)
        with self.lock:
            notes = self.read_notes()
            keep = {k: v for k, v in notes.items() if not (k == key or k.startswith(key + "/"))}
            if len(keep) != len(notes):
                self.write_notes(keep)

    def add_feedback(self, paths: list, text: str, lines=None, quote: str = "") -> dict:
        keys = sorted({self.norm(p) or "." for p in paths})
        if not keys:
            raise HTTPError(400, "feedback needs at least one path")
        if not text.strip():
            raise HTTPError(400, "feedback text is empty")
        now = stamp()
        with self.lock:
            feedback = self.read_feedback()
            e = {
                "id": max((f["id"] for f in feedback), default=0) + 1,
                "paths": keys,
                "text": text.rstrip(),
                "status": "open",
                "reply": "",
                "created": now,
                "updated": now,
            }
            if lines:
                if len(keys) != 1:
                    raise HTTPError(400, "line comments attach to exactly one file")
                a, b = sorted(int(x) for x in lines)
                e.update(lines=[max(1, a), max(1, b)], quote=quote)
            feedback.append(e)
            self._save(self.read_notes(), feedback)
            return e

    def update_feedback(self, fid: int, **changes) -> dict:
        with self.lock:
            feedback = self.read_feedback()
            e = next((f for f in feedback if f["id"] == fid), None)
            if e is None:
                raise HTTPError(404, f"no feedback #{fid}")
            for k, v in changes.items():
                if v is None:
                    continue
                if k == "status" and v not in ("open", "done"):
                    raise HTTPError(400, "status must be open or done")
                if k in ("text", "reply", "status"):
                    e[k] = str(v).rstrip()
            e["updated"] = stamp()
            self._save(self.read_notes(), feedback)
            return e

    def delete_feedback(self, fid: int) -> None:
        with self.lock:
            feedback = self.read_feedback()
            keep = [f for f in feedback if f["id"] != fid]
            if len(keep) == len(feedback):
                raise HTTPError(404, f"no feedback #{fid}")
            self._save(self.read_notes(), keep)

    def refresh_anchors(self) -> list:
        """Follow line comments to where their quoted lines moved; persist only real changes."""
        with self.lock:
            feedback, changed = self.read_feedback(), False
            for f in feedback:
                if not f.get("lines") or not f.get("quote"):
                    continue
                try:
                    text = decode(self.target_path(f["paths"][0]).read_bytes())
                except (OSError, HTTPError):
                    text = None
                a, b, stale = locate(f["lines"], f["quote"], text) if text is not None else (*f["lines"], True)
                if [a, b] != f["lines"] or stale != bool(f.get("stale")):
                    f["lines"] = [a, b]
                    if stale:
                        f["stale"] = True
                    else:
                        f.pop("stale", None)
                    changed = True
            if changed:
                self._save(self.read_notes(), feedback)
            return feedback

    @staticmethod
    def _row_note(key: str, notes: dict, single: dict):
        parts = [notes[key]] if key in notes else []
        parts += [f"{fb_label(f)} {first_line(f['text'])}" for f in single.get(key, [])]
        return "\n".join(parts) or None

    # ---------- files ----------
    def read_file(self, rel: str) -> dict:
        rel = self.norm(rel)
        t = self.target_path(rel)
        if not t.exists():
            raise HTTPError(404, "file not found")
        if not t.is_file():
            raise HTTPError(400, "not a file")
        mime = plot_mime(rel)
        if mime:
            h, size = hashlib.sha1(), 0
            with t.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    size += len(chunk)
                    h.update(chunk)
            out = {"path": rel, "hash": h.hexdigest(), "size": size, "kind": "plot", "mime": mime}
            if mime == "image/svg+xml" and size <= MAX_TEXT:  # source stays editable
                data = t.read_bytes()
                text = decode(data)
                if text is not None:
                    out["content"] = text.replace("\r\n", "\n")
                    out["crlf"] = b"\r\n" in data
            return out
        data = t.read_bytes()
        out = {"path": rel, "hash": sha1(data), "size": len(data)}
        if len(data) > MAX_TEXT:
            out["kind"] = "large"
            return out
        text = decode(data)
        if text is None:
            out["kind"] = "binary"
            return out
        out.update(kind="text", content=text.replace("\r\n", "\n"), crlf="\r\n" in text)
        return out

    def raw_file(self, rel: str) -> tuple[bytes, str]:
        """Bytes and content type of a plot, for the viewer. Anything else is refused."""
        rel = self.norm(rel)
        mime = plot_mime(rel)
        if not mime:
            raise HTTPError(415, "not a plot")
        t = self.target_path(rel)
        if not t.exists():
            raise HTTPError(404, "file not found")
        if not t.is_file():
            raise HTTPError(400, "not a file")
        data = t.read_bytes()
        if len(data) > RAW_MAX:
            raise HTTPError(413, "plot is too large to show")
        return data, mime

    def write_file(self, rel: str, content: str, base_hash, force: bool) -> dict:
        rel = self.norm(rel)
        if not rel:
            raise HTTPError(400, "cannot write the root")
        with self.lock:
            t = self.target_path(rel)
            old = t.read_bytes() if t.is_file() else None
            cur = sha1(old) if old is not None else None
            if not force and cur != base_hash:
                disk = self.read_file(rel) if old is not None else None
                raise HTTPError(409, "file changed on disk", {"disk": disk})
            text = content.replace("\r\n", "\n")
            if old is not None and b"\r\n" in old:
                text = text.replace("\n", "\r\n")
            data = text.encode("utf-8")
            self.mark_ui(rel)
            atomic_write(t, data)
            staged = old is None and self.git_add(rel)  # a file brought back (Restore) goes to git too
            return {"hash": sha1(data), "git": staged}

    def _git_where(self, rel: str):
        """(folder to run git in, pathspec) for REL: a file's own folder, so nested repos answer for it."""
        rel = self.norm(rel)
        p = self.node_path(rel)
        if rel and not p.is_dir():
            return p.parent, p.name
        return p, "."

    def _run_git(self, cwd: Path, *args: str, limit: int = 400_000) -> str:
        try:
            r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HTTPError(500, f"git failed: {e}") from e
        if r.returncode != 0:
            msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise HTTPError(404 if "not a git repository" in " ".join(msg) else 500, msg[0] if msg else "git failed")
        out = r.stdout[:limit].decode("utf-8", "replace")
        return out + ("\n... (cut: output too long)\n" if len(r.stdout) > limit else "")

    def git_log(self, rel: str, whole: bool, n: int) -> dict:
        """Commits touching REL (or the whole repository), newest first, plus its uncommitted changes."""
        cwd, spec = self._git_where(rel)
        top = self._run_git(cwd, "rev-parse", "--show-toplevel").strip()
        specs = [] if whole else ["--", spec]
        raw = self._run_git(
            cwd, "log", f"-n{max(1, min(n, 1000))}", "--format=%H%x1f%h%x1f%an%x1f%at%x1f%s%x1e", *specs
        )
        commits = []
        for rec in raw.split("\x1e"):
            parts = rec.strip("\n").split("\x1f")
            if len(parts) == 5:
                commits.append(
                    {"hash": parts[0], "short": parts[1], "author": parts[2], "at": int(parts[3]), "subject": parts[4]}
                )
        status = self._run_git(cwd, "status", "--short", *([] if whole else ["--", spec])).splitlines()
        return {"repo": top, "commits": commits, "status": status, "scope": "repository" if whole else (rel or ".")}

    def git_show(self, rel: str, whole: bool, commit: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{4,40}", commit or ""):
            raise HTTPError(400, "bad commit id")
        cwd, spec = self._git_where(rel)
        return self._run_git(
            cwd, "show", "--stat", "--patch", "--format=fuller", commit, *([] if whole else ["--", spec])
        )

    # ---------- content search ----------
    def grep(self, query: str, case: bool = False, limit: int = GREP_LIMIT) -> dict:
        """Lines matching QUERY in the files the tree shows. case: match case. At most `limit` hits
        (never more than GREP_LIMIT); truncated when some were left out."""
        query = str(query or "")
        if not query.strip():
            raise HTTPError(400, "a search query is required")
        if len(query) > 400 or "\n" in query or "\0" in query:
            raise HTTPError(400, "query is too long")
        limit = max(1, min(int(limit), GREP_LIMIT))
        hits: list = []
        if self._inside_git(self.root):
            truncated = self._git_grep(self.root, "", query, case, hits, limit, no_exclude=not self.gitignore)
            if self.gitignore:
                for k in self.show:
                    if len(hits) >= limit:
                        truncated = True
                        break
                    if not self._ignored_by_git(k):
                        continue  # the root search already covered it
                    p = self.root.joinpath(*self.parts(k))
                    if not p.exists():
                        continue
                    if p.is_dir() and not p.is_symlink():
                        truncated = self._grep_dir(p, k, query, case, hits, limit) or truncated
                    else:
                        truncated = self._grep_one(k, query, case, hits, limit) or truncated
        else:
            truncated = self._grep_walk(self.root, "", query, case, hits, limit)
        hits.sort(key=lambda h: (h["path"], h["line"]))
        return {"hits": hits, "truncated": bool(truncated)}

    def _inside_git(self, d: Path) -> bool:
        try:
            r = subprocess.run(
                ["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"], capture_output=True, timeout=5, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and r.stdout.strip() == b"true"

    def _ignored_by_git(self, rel: str) -> bool:
        try:
            r = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "-q", "--", rel],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def _searchable(self, rel: str) -> bool:
        """A file the tree would show and the editor could open."""
        try:
            parts = self.parts(rel)
        except HTTPError:
            return False
        if not parts or any(self.ignored(p) for p in parts):
            return False
        if self.gitignore:
            hidden, cur = self.git_ignored(), ""
            for p in parts:
                cur = f"{cur}/{p}" if cur else p
                if cur in hidden:
                    return False
        try:
            node = self.node_path(rel)
            target = node.resolve()
        except (HTTPError, OSError):
            return False
        if not node.is_file() or target == self.notes_path:
            return False
        if not self.follow_outside and not self.inside(target):
            return False
        try:
            return target.stat().st_size <= MAX_TEXT
        except OSError:
            return False

    def _grep_one(self, rel: str, query: str, case: bool, hits: list, limit: int) -> bool:
        """Search one file. True when a further match in it would not fit."""
        if not self._searchable(rel):
            return False
        try:
            data = self.target_path(rel).read_bytes()
        except (OSError, HTTPError):
            return False
        text = decode(data)
        if text is None:
            return False
        needle = query if case else query.casefold()
        for i, line in enumerate(text.splitlines(), 1):
            if needle not in (line if case else line.casefold()):
                continue
            if len(hits) >= limit:
                return True
            hits.append({"path": rel, "line": i, "text": _snippet(line, query, case)})
        return False

    def _grep_walk(self, d: Path, prefix: str, query: str, case: bool, hits: list, limit: int) -> bool:
        """Search files under D the way the tree walks them. True when a file was left unsearched
        because the hit list was already full."""
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return False
        for e in entries:
            if len(hits) >= limit:
                return True
            if self.ignored(e.name):
                continue
            rel = f"{prefix}/{e.name}" if prefix else e.name
            if self.gitignore and rel in self.git_ignored():
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not e.is_symlink() and self._grep_walk(Path(e.path), rel, query, case, hits, limit):
                    return True
                continue
            if e.is_file(follow_symlinks=True) and self._grep_one(rel, query, case, hits, limit):
                return True
        return False

    def _grep_dir(self, d: Path, prefix: str, query: str, case: bool, hits: list, limit: int) -> bool:
        """Search a --show folder git would have skipped. A nested repo keeps its own ignore rules."""
        if (d / ".git").exists() and self._inside_git(d):
            return self._git_grep(d, prefix, query, case, hits, limit, no_exclude=False)
        if self._inside_git(d):  # still the parent repo: its ignore rules are exactly what hid this folder
            return self._git_grep(d, prefix, query, case, hits, limit, no_exclude=True)
        return self._grep_walk(d, prefix, query, case, hits, limit)

    def _consume_grep(self, raw: bytes, prefix: str, query: str, case: bool, hits: list, limit: int) -> bool:
        """Parse `git grep -z -n` records (path, line, text, split by NUL). True if a record was left
        over because the list was already full."""
        for rec in raw.split(b"\n"):
            if not rec:
                continue
            if len(hits) >= limit:
                return True
            path_b, sep, rest = rec.partition(b"\0")
            line_b, sep2, text_b = rest.partition(b"\0")
            if not sep or not sep2:
                continue
            try:
                line_no = int(line_b)
            except ValueError:
                continue
            rel = _rel_join(prefix, os.fsdecode(path_b))
            if not self._searchable(rel) or any(h["path"] == rel and h["line"] == line_no for h in hits):
                continue
            text = text_b.decode("utf-8", "replace").rstrip("\r")
            hits.append({"path": rel, "line": line_no, "text": _snippet(text, query, case)})
        return False

    def _git_grep(
        self, cwd: Path, prefix: str, query: str, case: bool, hits: list, limit: int, no_exclude: bool
    ) -> bool:
        """Run git grep in CWD. Paths in the output are relative to CWD; prefix them. True when the
        hit list filled up with more matches still unread."""
        cmd = ["git", "-C", str(cwd), "grep", "-n", "-I", "-F", "-z", "--untracked"]
        if no_exclude:
            cmd.append("--no-exclude-standard")
        if not case:
            cmd.append("-i")
        cmd += ["-e", query]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return self._grep_walk(cwd, prefix, query, case, hits, limit)
        pending, truncated, deadline = b"", False, time.monotonic() + 15
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        try:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise HTTPError(500, "search took too long")
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                pending += chunk
                if b"\n" not in pending:
                    continue
                done, pending = pending.rsplit(b"\n", 1)
                if self._consume_grep(done, prefix, query, case, hits, limit):
                    truncated = True
                    proc.kill()
                    break
            if not truncated and pending.strip(b"\0"):
                truncated = self._consume_grep(pending, prefix, query, case, hits, limit)
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
        if proc.returncode == 128 and not hits:
            return self._grep_walk(cwd, prefix, query, case, hits, limit)
        return truncated

    def git_add(self, rel: str) -> bool:
        """Stage a file the editor created, in the repository that holds it (a nested notes/ repo is its
        own); ignored paths are not forced in, and outside git nothing happens. True when staged."""
        p = self.node_path(self.norm(rel))
        try:
            r = subprocess.run(
                ["git", "-C", str(p.parent), "add", "--", p.name], capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def create(self, rel: str, kind: str) -> bool:
        rel = self.norm(rel)
        if not rel:
            raise HTTPError(400, "a name is required")
        p = self.node_path(rel)
        if os.path.lexists(p):
            raise HTTPError(409, f"{rel} already exists")
        self.mark_ui(rel)
        if kind == "dir":
            p.mkdir(parents=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "x", encoding="utf-8"):
                pass
            return self.git_add(rel)
        return False

    def rename(self, src: str, dst: str) -> None:
        src, dst = self.norm(src), self.norm(dst)
        if not src or not dst:
            raise HTTPError(400, "cannot move the root")
        if dst == src or dst.startswith(src + "/"):
            raise HTTPError(400, "cannot move a folder into itself")
        ps, pd = self.node_path(src), self.node_path(dst)
        if not os.path.lexists(ps):
            raise HTTPError(404, f"{src} not found")
        if os.path.lexists(pd):
            raise HTTPError(409, f"{dst} already exists")
        with self.lock:
            pd.parent.mkdir(parents=True, exist_ok=True)
            self.mark_ui(dst)
            os.rename(ps, pd)
            self.move_notes(src, dst)
            drafts = self.read_drafts()
            for k in [k for k in drafts if k == src or k.startswith(src + "/")]:
                self.set_draft(dst + k[len(src) :], {x: y for x, y in drafts[k].items() if x != "at"})
                self.set_draft(k, None)

    def delete(self, rel: str) -> None:
        rel = self.norm(rel)
        if not rel:
            raise HTTPError(400, "cannot delete the root")
        p = self.node_path(rel)
        with self.lock:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            else:
                raise HTTPError(404, f"{rel} not found")
            self.drop_notes(rel)
            for k in [k for k in self.read_drafts() if k == rel or k.startswith(rel + "/")]:
                self.set_draft(k, None)  # deleting in the editor was confirmed, drafts included

    # ---------- export ----------
    def export(self, counts: bool = True, color: bool = False) -> str:
        tree, notes = self.scan(), self.read_notes()
        feedback = [f for f in self.refresh_anchors() if f.get("status") != "done"]
        single = {}
        for f in feedback:
            if len(f["paths"]) == 1:
                single.setdefault(f["paths"][0], []).append(f)
        rows, paths = [], {""}
        ranks = rank_by_size(word_totals(tree), dir_paths(tree))

        def label(n: dict) -> str:
            s = n["name"] + ("/" if n["type"] == "dir" else "")
            if n.get("link"):
                s += f" -> {n['link']}"
            if counts and n.get("kind") == "text":
                s += f"  {plural(n['lines'], 'line')}, {plural(n['words'], 'word')}"
                if "tok" in n:
                    s += f", ~{fmt_tokens(n['tok'])} tokens"
            elif counts and n["type"] == "dir" and n.get("tmax"):
                lo, hi = fmt_tokens(n["tmin"]), fmt_tokens(n["tmax"])
                s += f"  ~{lo} tokens" if lo == hi else f"  ~{lo}-{hi} tokens"
            elif counts and n.get("kind") == "plot":
                s += f"  (plot, {n['size']} bytes)"
            elif counts and n.get("kind") in ("binary", "large"):
                s += f"  ({n['kind']}, {n['size']} bytes)"
            return s

        def paint(n: dict, text: str) -> str:
            if not color:
                return text
            return heat_ansi(ranks.get(n["path"]), n["type"] == "dir") + text + "\033[0m"

        def walk(children: list, prefix: str):
            for i, n in enumerate(children):
                paths.add(n["path"])
                last = i == len(children) - 1
                cont = prefix + ("    " if last else "│   ")
                kids = n.get("children") or []
                branch, text = prefix + ("└── " if last else "├── "), label(n)
                rows.append(
                    (
                        branch + text,
                        branch + paint(n, text),
                        self._row_note(n["path"], notes, single),
                        cont + ("│" if kids else ""),
                    )
                )
                walk(kids, cont)

        root = tree["name"] + "/"
        rows.append((root, paint(tree, root), self._row_note(".", notes, single), "│" if tree["children"] else ""))
        walk(tree["children"], "")
        noted = [len(r[0]) for r in rows if r[2]]
        col = min(max(noted) + 2, 72) if noted else 0
        out = []
        for head, shown, note, cont in rows:
            if not note:
                out.append(shown)
                continue
            lines = note.strip().splitlines() or [""]
            pad = max(col, len(head) + 2)
            out.append(shown + " " * (pad - len(head)) + "# " + lines[0])
            out.extend(cont.ljust(pad) + "# " + ln for ln in lines[1:])
        if counts and tree.get("skills"):
            out += [
                "",
                f"# tokens (~{CHARS_PER_TOKEN} chars each): folder min-max = its SKILL.md alone, no leaf loaded "
                "(a folder without one: the SKILL.md of each skill inside) - every file underneath loaded",
            ]
        multi = [f for f in feedback if len(f["paths"]) > 1]
        if multi:
            out += ["", "# open feedback on several paths:"]
            out += [f"#   {fb_label(f)} {', '.join(f['paths'])}: {first_line(f['text'])}" for f in multi]
        orphans = sorted(k for k in notes if (k if k != "." else "") not in paths)
        if orphans:
            out += ["", "# notes on paths that no longer exist:"]
            out += [f"#   {k}: {notes[k].strip().splitlines()[0] if notes[k].strip() else ''}" for k in orphans]
        return "\n".join(out) + "\n"


def mount_names(roots: list) -> list:
    """A unique top-level name per root: its folder name, then parent/name, then a -2, -3 suffix."""
    out: list = []
    for r in roots:
        name = r.name or "root"
        if name in out:
            name = f"{r.parent.name}/{name}".replace("/", "-")
        base, i = name, 2
        while name in out:
            name, i = f"{base}-{i}", i + 1
        out.append(name)
    return out


class Mounts:
    """Several roots shown as one tree, each as a top-level folder named by mount_names(). Paths are
    "name/path/in/root"; every call goes to that root's Workspace, so each keeps its own notes file,
    gitignore, drafts and change tracking. Feedback ids become "name:N".
    The top level, and feedback on paths in several folders, go to the notes file of the folders'
    common parent (where the agent pane starts), with paths relative to it and ids "*:N"."""

    TOP = "*"

    def __init__(self, workspaces: list):
        self.mounts = dict(zip(mount_names([w.root for w in workspaces]), workspaces, strict=True))
        self.root = Path(os.path.commonpath([str(w.root) for w in workspaces]))  # where the terminal starts
        self.label = " + ".join(self.mounts)
        w0 = workspaces[0]
        same = [w for w in workspaces if w.root == self.root]  # a folder holding the others: share its notes
        self.top = (
            same[0]
            if same
            else Workspace(self.root, notes_file(self.root), w0.ignore, w0.follow_outside, w0.gitignore, w0.show)
        )
        self.own_top = not same
        self.notes_path = os.pathsep.join(str(w.notes_path) for w in workspaces + ([self.top] if self.own_top else []))

    @staticmethod
    def _pre(name: str, rel: str) -> str:
        return name if rel in ("", ".") else f"{name}/{rel}"

    def split(self, rel: str):
        """(name, workspace, path inside it) for REL; a 400 for the shared top level."""
        parts = Workspace.parts(rel)
        if not parts or parts[0] not in self.mounts:
            raise HTTPError(400, "pick a path inside one of the opened folders")
        return parts[0], self.mounts[parts[0]], "/".join(parts[1:])

    def _one(self, rels: list):
        hits = {self.split(r)[0] for r in rels}
        if len(hits) != 1:
            raise HTTPError(400, "paths must be inside one opened folder")
        name = hits.pop()
        return name, self.mounts[name], [self.split(r)[2] for r in rels]

    def _to_top(self, rel: str) -> str:
        """REL in the tree as a path in the common parent's notes file ('.' = the parent itself)."""
        if not Workspace.parts(rel):
            return "."
        _, w, sub = self.split(rel)
        return w.root.joinpath(*Workspace.parts(sub)).relative_to(self.root).as_posix()

    def _from_top(self, key: str) -> str:
        if key in ("", "."):
            return ""
        p = self.root / key
        for name, w in self.mounts.items():
            if p == w.root or w.root in p.parents:
                return self._pre(name, p.relative_to(w.root).as_posix())
        return key

    def _top_out(self, f: dict) -> dict:
        if not self.own_top:
            return self._fb_out(next(n for n, w in self.mounts.items() if w is self.top), f)
        return {
            **f,
            "id": f"{self.TOP}:{f['id']}",
            "num": f["id"],
            "root": str(self.root),
            "paths": [self._from_top(p) for p in f["paths"]],
        }

    def _prefix_map(self, get) -> dict:
        return {self._pre(name, k): v for name, w in self.mounts.items() for k, v in get(w).items()}

    def _fid(self, fid):
        name, _, num = str(fid).rpartition(":")
        if name == self.TOP and self.own_top and num.isdigit():
            return self.top, int(num)
        if name not in self.mounts or not num.isdigit():
            raise HTTPError(404, f"no feedback #{fid}")
        return self.mounts[name], int(num)

    def _fb_out(self, name: str, f: dict) -> dict:
        return {
            **f,
            "id": f"{name}:{f['id']}",
            "num": f["id"],
            "root": str(self.mounts[name].root),
            "paths": [self._pre(name, p) for p in f["paths"]],
        }

    # ---------- the tree ----------
    def scan(self) -> dict:
        kids = []
        for name, w in self.mounts.items():
            t = w.scan()

            def walk(n: dict, name=name) -> None:
                n["path"] = self._pre(name, n["path"])
                for c in n.get("children") or []:
                    walk(c)

            walk(t)
            t["name"], t["mount"] = name, str(w.root)
            kids.append(t)
        return {"name": self.label, "path": "", "type": "dir", "multi": True, "children": kids}

    def version(self) -> str:
        sig = [w.version() for w in self.mounts.values()]
        if self.own_top:  # only the notes file: the parent may hold far more than the opened folders
            try:
                st = self.top.notes_path.stat()
                sig.append(f"{st.st_mtime_ns}|{st.st_size}")
            except OSError:
                pass
        return sha1("|".join(sig).encode())[:16]

    @property
    def changes(self) -> dict:
        return self._prefix_map(lambda w: w.changes)

    @property
    def line_changes(self) -> dict:
        return self._prefix_map(lambda w: w.line_changes)

    def history(self) -> list:
        out = []
        for name, w in self.mounts.items():
            for ev in w.history():
                out.append({**ev, "path": self._pre(name, ev["path"])})
        out.sort(key=lambda e: e["at"])
        return out

    def changes_for(self, root: str):
        """(root, changes) of the opened folder at ROOT, for `jura write edits` in that folder."""
        w = next((w for w in self.mounts.values() if str(w.root) == root), None)
        return (str(w.root), w.changes) if w else (self.label, {})

    def mark_ui(self, rel: str) -> None:
        _, w, sub = self.split(rel)
        w.mark_ui(sub)

    def export(self, counts: bool = True, color: bool = False) -> str:
        out = "\n".join(w.export(counts, color) for w in self.mounts.values())
        if not self.own_top:
            return out
        note = self.top.read_notes().get(".")
        fb = [f for f in self.top.refresh_anchors() if f.get("status") != "done"]
        if note or fb:
            out += f"\n# on the opened folders together ({self.top.notes_path}):\n"
            out += "".join(f"#   {ln}\n" for ln in (note or "").strip().splitlines())
            out += "".join(f"#   {fb_label(f)} {', '.join(f['paths'])}: {first_line(f['text'])}\n" for f in fb)
        return out

    # ---------- notes & feedback ----------
    def read_notes(self) -> dict:
        top = {self._from_top(k) or ".": v for k, v in self.top.read_notes().items()} if self.own_top else {}
        return {**top, **self._prefix_map(lambda w: w.read_notes())}

    def set_note(self, rel: str, text: str) -> None:
        if not Workspace.parts(rel):
            return self.top.set_note(".", text)
        _, w, sub = self.split(rel)
        w.set_note(sub, text)

    def move_notes(self, src: str, dst: str) -> None:
        _, w, (s, d) = self._one([src, dst])
        w.move_notes(s, d)

    def refresh_anchors(self) -> list:
        out = [self._fb_out(name, f) for name, w in self.mounts.items() for f in w.refresh_anchors()]
        return out + ([self._top_out(f) for f in self.top.refresh_anchors()] if self.own_top else [])

    def add_feedback(self, paths: list, text: str, lines=None, quote: str = "") -> dict:
        if not paths:
            raise HTTPError(400, "feedback needs at least one path")
        if any(not Workspace.parts(p) for p in paths) or len({self.split(p)[0] for p in paths}) > 1:
            return self._top_out(self.top.add_feedback([self._to_top(p) for p in paths], text, lines, quote))
        name, w, subs = self._one(paths)
        return self._fb_out(name, w.add_feedback(subs, text, lines, quote))

    def update_feedback(self, fid, **changes) -> dict:
        w, num = self._fid(fid)
        if w is self.top and str(fid).startswith(self.TOP + ":"):
            return self._top_out(w.update_feedback(num, **changes))
        name = next(n for n, x in self.mounts.items() if x is w)
        return self._fb_out(name, w.update_feedback(num, **changes))

    def delete_feedback(self, fid) -> None:
        w, num = self._fid(fid)
        w.delete_feedback(num)

    # ---------- drafts ----------
    def read_drafts(self) -> dict:
        return self._prefix_map(lambda w: w.read_drafts())

    def set_draft(self, rel: str, draft) -> None:
        _, w, sub = self.split(rel)
        w.set_draft(sub, draft)

    # ---------- files ----------
    def read_file(self, rel: str) -> dict:
        name, w, sub = self.split(rel)
        return {**w.read_file(sub), "path": self._pre(name, w.norm(sub))}

    def raw_file(self, rel: str) -> tuple[bytes, str]:
        _, w, sub = self.split(rel)
        return w.raw_file(sub)

    def write_file(self, rel: str, content: str, base_hash, force: bool) -> dict:
        name, w, sub = self.split(rel)
        try:
            return w.write_file(sub, content, base_hash, force)
        except HTTPError as e:
            if e.extra.get("disk"):
                e.extra["disk"]["path"] = self._pre(name, e.extra["disk"]["path"])
            raise

    def git_log(self, rel: str, whole: bool, n: int) -> dict:
        name, w, sub = self.split(rel)
        out = w.git_log(sub, whole, n)
        return {**out, "scope": out["scope"] if whole else self._pre(name, sub)}

    def git_show(self, rel: str, whole: bool, commit: str) -> str:
        _, w, sub = self.split(rel)
        return w.git_show(sub, whole, commit)

    def grep(self, query: str, case: bool = False, limit: int = GREP_LIMIT) -> dict:
        hits, truncated = [], False
        for name, w in self.mounts.items():
            if len(hits) >= limit:
                truncated = True
                break
            part = w.grep(query, case, limit - len(hits))
            hits.extend({**h, "path": self._pre(name, h["path"])} for h in part["hits"])
            truncated = truncated or part["truncated"]
        hits.sort(key=lambda h: (h["path"], h["line"]))
        return {"hits": hits, "truncated": truncated}

    def create(self, rel: str, kind: str) -> bool:
        _, w, sub = self.split(rel)
        return w.create(sub, kind)

    def rename(self, src: str, dst: str) -> None:
        _, w, (s, d) = self._one([src, dst])
        if not s:
            raise HTTPError(400, "an opened folder cannot be moved from Write")
        w.rename(s, d)

    def delete(self, rel: str) -> None:
        _, w, sub = self.split(rel)
        if not sub:
            raise HTTPError(400, "an opened folder cannot be deleted from Write")
        w.delete(sub)


class Handler(BaseHTTPRequestHandler):
    ws: Workspace
    loopback = True
    server_version = "jura-write"
    token = secrets.token_urlsafe(18)  # proves a terminal connection comes from the page we served
    terms = None  # Sessions: the agent pane's terminals, when it is available (loopback only)
    agent = ""
    context: ClassVar[dict] = {}  # what the user has selected in the window, for `jura write context`
    window = None  # the app window process we launched; Ctrl+W / Ctrl+Q in the page close it
    app = False  # the page is shown in the app window (so it should handle Ctrl+W / Ctrl+Q itself)
    extras = None  # workbench: sources, skills, preview, auto-git, queue (write/extras.py)

    def log_message(self, *args):
        pass

    def send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, obj):
        self.send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError as e:
            raise HTTPError(400, "invalid JSON") from e

    def do_GET(self):
        self.dispatch("GET")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method: str):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if self.loopback:  # guard against DNS rebinding
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                if host not in ("127.0.0.1", "localhost", "::1"):
                    raise HTTPError(403, "bad Host header")
            if u.path.startswith("/api/") and method != "GET" and self.headers.get("X-Treedit") != "1":
                raise HTTPError(403, "missing X-Treedit header")
            self.route(method, u.path, q)
        except HTTPError as e:
            self.send_json(e.status, {"error": e.msg, **e.extra})
        except FileNotFoundError as e:
            self.send_json(404, {"error": str(e)})
        except PermissionError as e:
            self.send_json(403, {"error": str(e)})
        except ConnectionError:
            pass  # the client went away; nobody to answer
        except Exception as e:
            try:
                self.send_json(500, {"error": f"{type(e).__name__}: {e}"})
            except ConnectionError:
                pass

    def route(self, method: str, path: str, q: dict):
        ws = self.ws
        if self.extras is not None and self.extras.route(self, method, path, q):
            return None
        if method == "GET" and path in ("/", "/index.html"):
            cfg = json.dumps(
                {
                    "token": self.token,
                    "agent": self.agent,
                    "term": self.terms is not None,
                    "app": self.app,
                    **(self.extras.cfg() if self.extras is not None else {}),
                }
            )
            page = PAGE.replace("/*TREEDIT_CONFIG*/{}", cfg.replace("</", "<\\/"))
            return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if method == "GET" and path == "/logo.svg":
            return self.send(200, (resources.files(__package__) / "logo.svg").read_bytes(), "image/svg+xml")
        if method == "GET" and path.startswith("/vendor/"):
            name = path[len("/vendor/") :]
            if name not in VENDOR:
                raise HTTPError(404, "no such file")
            return self.send(200, (resources.files(__package__) / "vendor" / name).read_bytes(), VENDOR[name])
        if method == "GET" and path == "/api/term":
            return self.terminal(q)
        if method == "GET" and path == "/api/terms":
            return self.send_json(200, {"terms": self.sessions().list()})
        if method == "GET" and path == "/api/git/log":
            return self.send_json(200, ws.git_log(q.get("path", ""), q.get("whole") == "1", int(q.get("n", "150"))))
        if method == "GET" and path == "/api/git/show":
            return self.send(
                200,
                ws.git_show(q.get("path", ""), q.get("whole") == "1", q.get("commit", "")).encode("utf-8"),
                "text/plain; charset=utf-8",
            )
        if method == "GET" and path == "/api/grep":
            return self.send_json(200, ws.grep(q.get("q", ""), q.get("case") == "1"))
        if method == "GET" and path == "/api/changes":
            root, changes = ws.changes_for(q.get("root", "")) if isinstance(ws, Mounts) else (str(ws.root), ws.changes)
            return self.send_json(200, {"root": root, "changes": changes, "now": time.time()})
        if method == "GET" and path == "/api/history":
            return self.send_json(200, {"events": ws.history(), "now": time.time()})
        if method == "GET" and path == "/api/context":
            return self.send_json(200, Handler.context)
        if method == "GET" and path == "/api/version":
            return self.send_json(200, {"version": ws.version()})
        if method == "GET" and path == "/api/tree":
            v = ws.version()
            multi = isinstance(ws, Mounts)
            return self.send_json(
                200,
                {
                    "root": ws.label if multi else ws.root.name,
                    "rootPath": str(ws.root),
                    "notesFile": str(ws.notes_path),
                    "version": v,
                    "tree": ws.scan(),
                    "notes": ws.read_notes(),
                    "feedback": ws.refresh_anchors(),
                    "changes": ws.changes,
                    "drafts": ws.read_drafts(),
                    "lines": ws.line_changes,
                    "now": time.time(),
                },
            )
        if method == "GET" and path == "/api/file":
            return self.send_json(200, ws.read_file(q.get("path", "")))
        if method == "GET" and path == "/api/raw":
            data, mime = ws.raw_file(q.get("path", ""))
            return self.send(
                200,
                data,
                mime,
                {
                    "X-Content-Type-Options": "nosniff",
                    # a plot opened on its own must not run as a page on this origin
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; sandbox",
                },
            )
        b = self.body()
        if method == "PUT" and path == "/api/draft":
            draft = {k: b[k] for k in ("base_hash", "diff", "full", "base") if b.get(k) is not None}
            ws.set_draft(b.get("path", ""), draft if ("diff" in draft or "full" in draft) else None)
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/draft-drop":
            ws.set_draft(b.get("path", ""), None)
            return self.send_json(200, {"ok": True})
        if method == "PUT" and path == "/api/file":
            return self.send_json(
                200,
                ws.write_file(b.get("path", ""), str(b.get("content", "")), b.get("base_hash"), bool(b.get("force"))),
            )
        if method == "PUT" and path == "/api/note":
            ws.set_note(b.get("path", ""), str(b.get("note", "")))
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/log":  # page errors, shown with WORKBENCH_DEBUG=1
            if os.environ.get("WORKBENCH_DEBUG"):
                print(f"page: {str(b.get('msg', ''))[:2000]}", file=sys.stderr, flush=True)
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/context":
            Handler.context = {k: b[k] for k in ("paths", "lines", "quote", "focus") if b.get(k)}
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/term/new":
            sid = self.sessions().add(str(b.get("agent") or ""), str(b.get("name") or ""))
            return self.send_json(200, {"id": sid, "terms": self.sessions().list()})
        if method == "POST" and path == "/api/term/close":
            if not self.sessions().close(str(b.get("id", ""))):
                raise HTTPError(404, "no such terminal")
            return self.send_json(200, {"terms": self.sessions().list()})
        if method == "POST" and path == "/api/feedback":
            return self.send_json(
                200,
                ws.add_feedback(
                    list(b.get("paths") or []), str(b.get("text", "")), b.get("lines"), str(b.get("quote") or "")
                ),
            )
        if method == "PUT" and path == "/api/feedback":
            return self.send_json(
                200, ws.update_feedback(self.fid(b), text=b.get("text"), status=b.get("status"), reply=b.get("reply"))
            )
        if method == "POST" and path == "/api/feedback-delete":
            ws.delete_feedback(self.fid(b))
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/note-move":
            ws.move_notes(b.get("from", ""), b.get("to", ""))
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/new":
            return self.send_json(200, {"ok": True, "git": ws.create(b.get("path", ""), b.get("kind", "file"))})
        if method == "POST" and path == "/api/rename":
            ws.rename(b.get("from", ""), b.get("to", ""))
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/delete":
            ws.delete(b.get("path", ""))
            return self.send_json(200, {"ok": True})
        raise HTTPError(404, "no such endpoint")

    def fid(self, b: dict):
        """A feedback id from a request: N, or "name:N" when several folders are open."""
        return str(b.get("id", "")) if isinstance(self.ws, Mounts) else int(b.get("id", 0))

    def sessions(self) -> Sessions:
        if self.terms is None:
            raise HTTPError(404, "the agent terminal is off (it needs a loopback --host)")
        return self.terms

    def terminal(self, q: dict):
        """Upgrade to a WebSocket onto one of the agent pane's terminals (?id=, default the first)."""
        terms = self.sessions()
        term = terms.get(q.get("id") or next(iter(terms.terms), ""))
        if term is None:
            raise HTTPError(404, "no such terminal")
        if not secrets.compare_digest(q.get("token", ""), self.token):
            raise HTTPError(403, "bad terminal token")
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            raise HTTPError(403, "terminal connections must come from the Write page")
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            raise HTTPError(400, "expected a WebSocket upgrade")
        self.protocol_version = "HTTP/1.1"  # WebKit (the app window) rejects a 101 sent as HTTP/1.0
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        try:
            term.serve(self.connection, self.rfile)
        except OSError:
            pass  # the page went away; never answer an upgraded socket with HTTP


VENDOR = {
    "xterm.js": "text/javascript; charset=utf-8",
    "xterm.css": "text/css; charset=utf-8",
    "addon-fit.js": "text/javascript; charset=utf-8",
}
PAGE = (resources.files(__package__) / "page.html").read_text("utf-8")


def _ws(
    root: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool = False,
    show: Optional[List[str]] = None,
) -> Workspace:
    r = Path(root).expanduser()
    if not r.is_dir():
        sys.exit(f"write: {r} is not a folder")
    n = Path(notes).expanduser() if notes else notes_file(r)
    return Workspace(
        r,
        n,
        DEFAULT_IGNORE + list(ignore or []),
        follow_outside,
        not no_gitignore,
        DEFAULT_SHOW if show is None else show,
    )


def roots_of(root) -> list:
    """The folders to open, once each (a single path is fine too)."""
    return list(dict.fromkeys([root] if isinstance(root, str) else root or ["."]))


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a closed tab is not an error
            super().handle_error(request, client_address)


def free_port(port: int, host: str = "127.0.0.1") -> int:
    """The first port in PORT..PORT+19 that HOST can bind."""
    import socket

    for p in range(port, port + 20):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    sys.exit(f"write: no free port in {port}-{port + 19}")


def serve(
    root: str,
    port: int,
    agent: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Serve the Write tab for ROOT (the case workdir) on 127.0.0.1:PORT (0: any free port).
    The workbench supervisor starts this and reads the `WRITE_URL <url>` line. Headless: the
    workbench shows the page in its own window. SIGTERM stops the terminals and the server."""
    ws = _ws(root, notes, ignore, follow_outside, no_gitignore, show)
    Handler.ws = ws
    Handler.loopback = True
    srv = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/"
    ws.scan()  # baseline for change tracking
    agent = agent or os.environ.get("WORKBENCH_AGENT", "")
    Handler.agent = agent
    from workbench.sandbox import login_shell

    env = {
        **os.environ,
        "SHELL": login_shell(),  # your shell, as in any terminal (the app may start without $SHELL)
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "WORKBENCH_WRITE_URL": url,  # for `jura write context` / `edits`; the case dir is $WORKBENCH_CASE_DIR
    }
    from .extras import Extras

    extras = Handler.extras = Extras(ws, env)
    Handler.terms = Sessions(str(ws.root), env, agent.strip() or None, wrap=extras.wrap)
    extras.start()

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"write  {ws.root}\n  notes  {ws.notes_path}\n  agent  {agent or 'closed'}", file=sys.stderr)
    print(f"WRITE_URL {url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.terms.stop()
        extras.stop()
        srv.server_close()


def print_tree(
    root: List[str],
    no_counts: bool,
    no_color: bool,
    color: bool,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Print the tree with annotations and open feedback as '# ...' comments. On a terminal rows are
    coloured by document size in the tree (dim, default, amber, red; dark grey = no words)."""
    use = not no_color and (color or (sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
    for i, r in enumerate(roots_of(root)):
        sys.stdout.write(
            ("\n" if i else "")
            + _ws(r, notes, ignore, follow_outside, no_gitignore, show).export(counts=not no_counts, color=use)
        )


def note_get(
    path: str, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """Print the user's annotation on PATH ('.' is the root); exit 1 if there is none."""
    key = "/".join(Workspace.parts(path)) or "."
    text = _ws(root, notes, ignore, follow_outside, no_gitignore, show).read_notes().get(key)
    if text is None:
        sys.exit(1)
    print(text)


def note_set(
    path: str,
    text: str,
    root: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Set the annotation on PATH; an empty TEXT removes it. For the user: agents never write annotations."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).set_note(path, text)


def note_mv(
    src: str,
    dst: str,
    root: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Re-key the annotations of SRC (and everything under it) to DST, without touching files."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).move_notes(src, dst)


def note_ls(
    root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """List every annotation as 'path<TAB>first line'; paths that no longer exist are marked '(gone)'."""
    ws = _ws(root, notes, ignore, follow_outside, no_gitignore, show)
    for k, v in sorted(ws.read_notes().items()):
        gone = "" if k == "." or os.path.lexists(ws.root / k) else "  (gone)"
        print(f"{k}\t{(v.strip().splitlines() or [''])[0]}{gone}")


def fb_ls(
    all: bool, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """List feedback for the agent to act on: id, status, path(s), lines, the quoted code, the
    request and any reply. Open items only unless --all. Line numbers follow the quoted code."""
    items = _ws(root, notes, ignore, follow_outside, no_gitignore, show).refresh_anchors()
    for f in items:
        if f.get("status") == "done" and not all:
            continue
        print(f"{fb_label(f)[2:]}  {f.get('status', 'open')}  {', '.join(f['paths'])}")
        for ln in (f.get("quote") or "").split("\n") if f.get("quote") else []:
            print(f"    | {ln}")
        for ln in f["text"].splitlines():
            print(f"    {ln}")
        if f.get("reply"):
            print("    reply: " + "\n           ".join(f["reply"].splitlines()))
        print()


def fb_add(
    text: str,
    paths: List[str],
    lines: str,
    root: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Add feedback on one or more paths; --lines A-B anchors it to lines of a single file."""
    ws = _ws(root, notes, ignore, follow_outside, no_gitignore, show)
    span, quote = None, ""
    if lines:
        a, _, b = lines.partition("-")
        span = [int(a), int(b or a)]
        body = decode(ws.target_path(paths[0]).read_bytes()) or ""
        quote = "\n".join(body.split("\n")[span[0] - 1 : span[0] - 1 + min(QUOTE_LINES, span[1] - span[0] + 1)])
    print(f"#{ws.add_feedback(paths, text, span, quote)['id']}")


def fb_reply(
    id: int,
    text: str,
    done: bool,
    root: str,
    notes: str,
    ignore: List[str],
    follow_outside: bool,
    no_gitignore: bool,
    show: List[str],
) -> None:
    """Answer feedback ID (what was changed, or why not); --done also resolves it."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).update_feedback(
        id, reply=text, status="done" if done else None
    )


def fb_done(
    id: int, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """Mark feedback ID done."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).update_feedback(id, status="done")


def fb_reopen(
    id: int, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """Mark feedback ID open again."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).update_feedback(id, status="open")


def fb_rm(
    id: int, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """Delete feedback ID."""
    _ws(root, notes, ignore, follow_outside, no_gitignore, show).delete_feedback(id)


def show_context(url: str) -> None:
    """Print what the user has selected in the Write window: paths, and the line span with its
    quoted text when lines are selected. Inside the agent pane the window's URL is in $WORKBENCH_WRITE_URL."""
    base = (url or os.environ.get("WORKBENCH_WRITE_URL") or "http://127.0.0.1:8765/").rstrip("/")
    try:
        with urlopen(base + "/api/context", timeout=3) as r:
            ctx = json.loads(r.read())
    except (URLError, OSError, ValueError) as e:
        sys.exit(f"write: no Write window at {base} ({e})")
    paths = ctx.get("paths") or []
    if not paths:
        print("nothing selected")
        return
    where = ""
    if ctx.get("lines"):
        a, b = ctx["lines"]
        where = f" L{a}" + (f"-{b}" if b != a else "")
    for p in paths:
        print(p + (where if p == (ctx.get("focus") or paths[0]) else ""))
    for ln in (ctx.get("quote") or "").split("\n") if ctx.get("quote") else []:
        print(f"    | {ln}")


def show_edits(
    url: str, root: str, notes: str, ignore: List[str], follow_outside: bool, no_gitignore: bool, show: List[str]
) -> None:
    """The user's own edits, for an agent to keep apart from its work: unsaved drafts from the editor
    (shown as diffs against the file they started from) and, when the window is running, the files
    the user saved in the editor recently. Commit saved ones as the user's before your own work."""
    ws = _ws(root, notes, ignore, follow_outside, no_gitignore, show)
    now = time.time()

    def age(at) -> str:
        m = int((now - (at or now)) // 60)
        return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{m // 60} h ago"

    drafts = ws.read_drafts()
    print(f"unsaved in the editor ({len(drafts)}):" if drafts else "unsaved in the editor: none")
    for path, d in sorted(drafts.items()):
        print(f"  {path}  (draft, {age(d.get('at'))})")
        body = d.get("diff") if d.get("diff") is not None else "(file too large to diff; whole new text kept)"
        for ln in body.rstrip("\n").splitlines():
            print(f"    {ln}")
    base = (url or os.environ.get("WORKBENCH_WRITE_URL") or "").rstrip("/")
    if not base:
        print("saved in the editor: unknown (no Write window; set $WORKBENCH_WRITE_URL or --url)")
        return
    try:
        with urlopen(f"{base}/api/changes?{urlencode({'root': str(ws.root)})}", timeout=3) as r:
            data = json.loads(r.read())
    except (URLError, OSError, ValueError):
        print(f"saved in the editor: unknown (no Write window at {base})")
        return
    if data.get("root") != str(ws.root):
        print(f"saved in the editor: unknown (the window at {base} shows {data.get('root')})")
        return
    mine = sorted((k, v) for k, v in data.get("changes", {}).items() if v.get("by") == "you")
    print(f"saved in the editor ({len(mine)}):" if mine else "saved in the editor: none recently")
    for path, v in mine:
        print(f"  {path}  ({age(v.get('at'))})")


ROOT = argument(name="root", arg_type=str, nargs="*", default=["."], help="folder(s) to open")
NOTE_ROOT = option(flags=["--root", "-C"], arg_type=str, default=".", help="tree root")
SHARED = [
    option(
        flags=["--show"],
        arg_type=str,
        nargs="*",
        default=DEFAULT_SHOW,
        help="paths kept visible though .gitignore hides them",
    ),
    option(flags=["--no-gitignore"], flag=True, help="show what .gitignore hides"),
    option(
        flags=["--notes"],
        arg_type=str,
        default="",
        help=f"notes file (empty: notes/{NOTES_NAME} when a notes folder is present, else ROOT/{NOTES_NAME})",
    ),
    option(flags=["--ignore"], arg_type=str, nargs="*", default=[], help="extra name globs to hide"),
    option(flags=["--follow-outside"], flag=True, help="allow symlink targets outside the root"),
]
FB_ID = argument(name="id", arg_type=int, help="feedback number (#N)")
NOTE_PATH = argument(name="path", arg_type=str, help="path relative to the root ('.' = root)")

app = cli(
    name="write",
    help="Review a directory tree with an agent: annotate files and folders, leave feedback (also on lines), "
    "see it coloured by word count; agents read both and answer feedback from the CLI.",
    version=__version__,
    subgroups=[
        cli(
            name="serve",
            help="Serve the Write tab for one case workdir (the workbench starts this).",
            callback=serve,
            arguments=[argument(name="root", arg_type=str, help="case workdir")],
            options=[
                *SHARED,
                option(flags=["--port", "-p"], arg_type=int, default=0, help="port (0: any free one)"),
                option(
                    flags=["--agent", "-a"],
                    arg_type=str,
                    default="",
                    help="command for the first agent terminal (default $WORKBENCH_AGENT). Unset, the pane stays closed",
                ),
            ],
        ),
        cli(
            name="print",
            help="Print the annotated tree, coloured by word count per level.",
            callback=print_tree,
            arguments=[ROOT],
            options=[
                *SHARED,
                option(flags=["--no-counts"], flag=True, help="omit line/word counts"),
                option(flags=["--no-color"], flag=True, help="never colour"),
                option(flags=["--color"], flag=True, help="colour even when not a terminal"),
            ],
        ),
        group(
            name="annotation",
            help="The user's comments on files and folders, for the agent to read.",
            options=[NOTE_ROOT, *SHARED],
            commands=[
                command(name="get", help="Print one annotation.", callback=note_get, arguments=[NOTE_PATH]),
                command(
                    name="set",
                    help="Set or clear one (user only).",
                    callback=note_set,
                    arguments=[NOTE_PATH, argument(name="text", arg_type=str, help="annotation ('' clears)")],
                ),
                command(
                    name="mv",
                    help="Move annotations to a new path.",
                    callback=note_mv,
                    arguments=[argument(name="src", arg_type=str), argument(name="dst", arg_type=str)],
                ),
                command(name="ls", help="List all annotations.", callback=note_ls),
            ],
        ),
        cli(
            name="edits",
            help="The user's own edits: unsaved drafts (diffs) and files saved in the editor.",
            callback=show_edits,
            options=[
                NOTE_ROOT,
                *SHARED,
                option(flags=["--url"], arg_type=str, default="", help="window URL (default $WORKBENCH_WRITE_URL)"),
            ],
        ),
        cli(
            name="context",
            help="What the user has selected in the Write window.",
            callback=show_context,
            options=[option(flags=["--url"], arg_type=str, default="", help="window URL (default $WORKBENCH_WRITE_URL)")],
        ),
        group(
            name="fb",
            help="Feedback for the agent: list it, answer it, resolve it.",
            options=[NOTE_ROOT, *SHARED],
            commands=[
                command(
                    name="ls",
                    help="List open feedback.",
                    callback=fb_ls,
                    options=[option(flags=["--all", "-a"], flag=True, help="include done items")],
                ),
                command(
                    name="add",
                    help="Add feedback on paths.",
                    callback=fb_add,
                    arguments=[argument(name="text", arg_type=str), argument(name="paths", arg_type=str, nargs="+")],
                    options=[option(flags=["--lines", "-l"], arg_type=str, default="", help="A-B (one file)")],
                ),
                command(
                    name="reply",
                    help="Reply to feedback.",
                    callback=fb_reply,
                    arguments=[FB_ID, argument(name="text", arg_type=str)],
                    options=[option(flags=["--done", "-d"], flag=True, help="also mark it done")],
                ),
                command(name="done", help="Mark feedback done.", callback=fb_done, arguments=[FB_ID]),
                command(name="reopen", help="Reopen feedback.", callback=fb_reopen, arguments=[FB_ID]),
                command(name="rm", help="Delete feedback.", callback=fb_rm, arguments=[FB_ID]),
            ],
        ),
    ],
)


def main(argv=None):
    if argv is not None:
        sys.argv = ["write", *argv]
    try:
        app.run()
    except HTTPError as e:
        sys.exit(f"write: {e.msg}")
    except BrokenPipeError:  # e.g. `jura write print | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)


if __name__ == "__main__":
    main()
