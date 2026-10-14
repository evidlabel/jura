"""The Write tab's Skillset pane: which agent skills a case's agents can load.

Where to look is ``skills.dirs`` in the case ``workbench.yml``. A case that omits
that key uses ``skills.toml`` (claude, grok, hermes, codex), or
``~/.config/workbench/skills.toml`` when that file exists. ``$WORKBENCH_SKILLS``
replaces either list with one directory. The pane shows the tree of those skills —
the folders above a ``SKILL.md`` inside a configured directory — not the directory
a symlink points at. Expanding a skill lists the markdown files in that folder.
The choice of which skills are on is the same section::

    skills:
      dirs:
        - ~/.claude/skills
        - ~/.grok/skills
      off:
        - did

New skills are on unless listed. ``did`` starts off: Write agents never handle raw case files.
Only the skills that are on are bound into the agent sandbox. An older ``skills.yml`` is
read until the next write, which folds it into ``workbench.yml``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from workbench import paths
from workbench.casefile import DEFAULT_SKILLS_OFF as DEFAULT_OFF
from workbench.casefile import read_config, write_config
MAX_FILE = 400_000
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}


def _tilde(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except (OSError, ValueError):
        return str(path)


def shipped_text() -> str:
    return (Path(__file__).resolve().parents[1] / "skills.toml").read_text(encoding="utf-8")


def config_path() -> Path | None:
    """The override file. None means the shipped catalogue. ``$WORKBENCH_SKILLS`` is a directory, not this."""
    user = paths.config_dir() / "skills.toml"
    return user if user.is_file() else None


def parse_dirs(text: str) -> list[dict]:
    """``[[dir]]`` rows. Unreadable TOML is an empty list."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    rows = data.get("dir") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        out.append({"name": str(row.get("name") or "").strip(), "path": path})
    return out


def dir_label(path: Path) -> str:
    """``~/.claude/skills`` is ``claude``. Any other directory uses its own name."""
    path = Path(os.path.expanduser(str(path)))
    if path.name == "skills":
        parent = path.parent.name[1:] if path.parent.name.startswith(".") else path.parent.name
        if parent and parent not in (path.anchor,):
            return parent
    name = path.name[1:] if path.name.startswith(".") else path.name
    return name or str(path)


def _from_paths(paths: list[str]) -> list[tuple[str, Path]]:
    """``(label, directory)`` for paths that exist. A missing directory stays in the file."""
    out = []
    seen: set[str] = set()
    for raw in paths:
        directory = Path(os.path.expanduser(str(raw)))
        key = str(directory)
        if key in seen or not directory.is_dir():
            continue
        seen.add(key)
        out.append((dir_label(directory), directory))
    return out


def skill_dirs(workdir: Path | None = None) -> list[tuple[str, Path]]:
    """``(label, directory)``. ``$WORKBENCH_SKILLS`` is that one directory, label empty.

    A case ``skills.dirs`` list replaces the catalogue. No ``dirs`` key keeps
    ``~/.config/workbench/skills.toml``, then the shipped file.
    """
    env = os.environ.get("WORKBENCH_SKILLS", "").strip()
    if env:
        return [("", Path(env).expanduser())]
    if workdir is not None:
        listed = read_config(workdir).skills_dirs
        if listed is not None:
            return _from_paths(listed)
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8") if path is not None else shipped_text()
    except (OSError, UnicodeError):
        return []
    out = []
    for row in parse_dirs(text):
        directory = Path(row["path"]).expanduser()
        if directory.is_dir():
            out.append((row["name"], directory))
    return out


def roots_label(workdir: Path | None = None) -> tuple[str, list[str]]:
    """Short label for the pane, and the directories it came from."""
    labels, paths = [], []
    for name, path in skill_dirs(workdir):
        shown = _tilde(path)
        paths.append(shown)
        labels.append(name or shown)
    return " · ".join(labels), paths


def read_off(workdir: Path) -> list[str]:
    """Skills that are off. A missing section means ``did`` only."""
    cfg = read_config(workdir)
    if cfg.skills_off is None:
        return list(DEFAULT_OFF)
    return list(cfg.skills_off)


def write_off(workdir: Path, off: list[str]) -> None:
    """Replace the off-list. Identity and the linked sets stay as they were."""
    cfg = read_config(workdir)
    cfg.skills_off = sorted(off)
    write_config(workdir, cfg)


def frontmatter(text: str) -> dict:
    """name / description from a SKILL.md's YAML frontmatter (single-line values, quotes stripped)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    lines = text[3:end].splitlines()
    for i, line in enumerate(lines):
        k, sep, v = line.partition(":")
        if not sep or k.strip() not in ("name", "description") or line.startswith((" ", "\t")):
            continue
        v = v.strip()
        if v[:1] in (">", "|"):  # a block scalar: the indented lines that follow
            block = []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt.strip())
            v = (" " if v[0] == ">" else "\n").join(s for s in block if s)
        out[k.strip()] = v.strip("'\"")
    return out


def _clean_names(names) -> list[str]:
    """Skill names are one path segment. Anything else is ignored, not written into workbench.yml."""
    out: list[str] = []
    if not isinstance(names, list):
        return out
    for raw in names:
        n = str(raw).strip()
        if not n or n.startswith(".") or "/" in n or "\\" in n or n in out:
            continue
        out.append(n)
    return out


def _has_skill_md(path: Path) -> bool:
    try:
        return path.is_dir() and (path / "SKILL.md").is_file()
    except OSError:
        return False


def _iter_skills(root: Path):
    """Skills under root: a folder with SKILL.md, not the folders inside it.

    Yields ``(leaf, resolved, parents, is_link)``. ``parents`` are the real directories
    between root and the skill. A symlink is one skill when its target has SKILL.md,
    and the target is not walked.
    """

    def walk(directory: Path, parents: tuple[str, ...]):
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        for child in sorted(children, key=lambda p: p.name.lower()):
            if child.name.startswith(".") or child.name in _SKIP:
                continue
            try:
                link = child.is_symlink()
            except OSError:
                continue
            if link:
                try:
                    target = child.resolve()
                except OSError:
                    continue
                if _has_skill_md(target):
                    yield child.name, target, parents, True
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if _has_skill_md(child):
                try:
                    target = child.resolve()
                except OSError:
                    continue
                yield child.name, target, parents, False
                continue
            yield from walk(child, (*parents, child.name))

    yield from walk(root, ())


def _unique(leaf: str, group: list[str], used: set[str]) -> str:
    """One path segment. A second skill with the same leaf name takes its group as a prefix."""
    if leaf not in used:
        used.add(leaf)
        return leaf
    parts = [p for p in [*group, leaf] if p]
    name = "-".join(parts) or leaf
    base, n = name, 2
    while name in used:
        name = f"{base}-{n}"
        n += 1
    used.add(name)
    return name


def files_of(d: Path, limit: int = 200) -> list[str]:
    """Markdown files in the skill folder itself. The directories under it are not listed."""
    out: list[str] = []
    try:
        kids = list(d.iterdir())
    except OSError:
        return out
    for kid in sorted(kids, key=lambda p: p.name):
        if kid.name.startswith(".") or kid.name in _SKIP:
            continue
        try:
            if kid.is_symlink() or not kid.is_file() or kid.suffix.lower() != ".md":
                continue
        except OSError:
            continue
        out.append(kid.name)
        if len(out) >= limit:
            break
    return out


def _records(workdir: Path | None = None) -> list[dict]:
    """Every skill in the configured directories. The same folder is listed once."""
    seen: set[str] = set()
    used: set[str] = set()
    out = []
    for label, root in skill_dirs(workdir):
        for leaf, target, parents, link in _iter_skills(root):
            key = str(target)
            if key in seen:
                continue
            seen.add(key)
            group = [p for p in (label, *parents) if p]
            name = _unique(leaf, group, used)
            md = target / "SKILL.md"
            try:
                fm = frontmatter(md.read_text("utf-8", errors="replace")[:8000])
            except OSError:
                fm = {}
            out.append(
                {
                    "name": name,
                    "title": fm.get("name") or leaf,
                    "description": fm.get("description", ""),
                    "dir": str(target),
                    "link": link,
                    "files": files_of(target),
                    "group": group,
                    "folder": target.name if target.name != leaf else "",
                }
            )
    return out


def list_skills(workdir: Path) -> list[dict]:
    off = set(read_off(workdir))
    return [{**s, "on": s["name"] not in off} for s in _records(workdir)]


def set_many(workdir: Path, names: list, on: bool) -> list[str]:
    """Switch these skills on or off. Names that are not a single path segment are ignored."""
    chosen = _clean_names(names)
    if not chosen:
        return read_off(workdir)
    skip = set(chosen)
    off = [s for s in read_off(workdir) if s not in skip]
    if not on:
        off.extend(n for n in chosen if n not in off)
    write_off(workdir, off)
    return off


def set_on(workdir: Path, name: str, on: bool) -> list[str]:
    return set_many(workdir, [name], on)


def clear(workdir: Path) -> list[str]:
    """Switch every skill that exists now off. A skill added later still starts on."""
    off = sorted({s["name"] for s in list_skills(workdir)})
    write_off(workdir, off)
    return off


def read_file(name: str, rel: str, workdir: Path | None = None) -> dict:
    """One file of a skill (read-only in the page)."""
    if "/" in name or name.startswith("."):
        raise FileNotFoundError(f"no skill {name}")
    match = next((s for s in _records(workdir) if s["name"] == name), None)
    if match is None:
        raise FileNotFoundError(f"no skill {name}")
    d = Path(match["dir"])
    p = (d / rel).resolve()
    if not p.is_relative_to(d) or not p.is_file():
        raise FileNotFoundError(f"no file {rel} in skill {name}")
    data = p.read_bytes()[:MAX_FILE]
    return {
        "name": name,
        "path": rel,
        "text": data.decode("utf-8", errors="replace"),
        "truncated": p.stat().st_size > MAX_FILE,
    }


def enabled_dirs(workdir: Path) -> list[tuple[str, Path]]:
    """(name, real folder) of every skill that is on — what the sandbox binds."""
    return [(s["name"], Path(s["dir"])) for s in list_skills(workdir) if s["on"]]
