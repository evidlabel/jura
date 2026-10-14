"""The agent sandbox: every terminal in the Write tab runs inside bubblewrap.

A Write terminal is the same terminal you have anywhere else (as in treedit): your shell and
its config, your history, every agent harness (claude, hermes, grok, opencode, codex, …) with
its own login and settings, the network. What the sandbox takes away is only what may hold a
case's personal data:

- did's folder (``$WORKBENCH_DID``, ``~/did_workdirs``): raw case files' review maps and keys;
- a did set whose project folder sits outside that directory, and the folders of its source files;
- the raw source files did has seen (each did project's ``draft/sources.json``): their
  folders are hidden, or the file itself when it sits directly in $HOME;
- evid sets the case did not link in ``workbench.yml`` (the global evid store shows only those);
- the other cases' workdirs;
- other projects' Claude Code transcripts, prompt history, file history and pastes (this
  case gets its own, kept in the workbench state dir);
- other programs' scratch files in ``/tmp`` (the terminal gets its own ``/tmp``);
- anything in ``$WORKBENCH_SANDBOX_HIDE`` (``:``-separated paths: folders or files).

Skills are the ones the case switched on (``workbench.yml``). ``$WORKBENCH_SANDBOX=0`` turns
the sandbox off (each terminal shows whether it runs sandboxed).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from workbench import paths


def enabled() -> bool:
    return os.environ.get("WORKBENCH_SANDBOX", "1") != "0" and shutil.which("bwrap") is not None


def _env_paths(name: str) -> list[Path]:
    return [Path(p).expanduser() for p in os.environ.get(name, "").split(":") if p.strip()]


def did_sources(did_root: Path) -> list[Path]:
    """Every raw source file the did projects under DID_ROOT reference (draft/sources.json)."""
    out: list[Path] = []
    if not did_root.is_dir():
        return out
    for reg in did_root.glob("**/draft/sources.json"):
        try:
            data = json.loads(reg.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        for item in data.get("sources") or []:
            if isinstance(item, dict) and item.get("path"):
                out.append(Path(str(item["path"])).expanduser())
    return out


def conceal(home: Path, did_root: Path, case_dir: Path, paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Hide did-pane sets that sit outside ``did_root``. Paths under ``did_root`` are already covered."""
    home, case_dir = home.resolve(), case_dir.resolve()
    try:
        root = did_root.expanduser().resolve()
    except OSError:
        root = did_root
    never = {Path("/"), home, *case_dir.parents, case_dir}
    folders: list[Path] = []
    files: list[Path] = []

    def under_root(p: Path) -> bool:
        try:
            return p == root or p.is_relative_to(root)
        except ValueError:
            return False

    for raw in paths:
        try:
            p = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if under_root(p) or p in never or case_dir.is_relative_to(p) or p.is_relative_to(case_dir):
            continue
        target = p if p.is_dir() else p.parent
        if target in never or case_dir.is_relative_to(target):
            if p.is_file():
                files.append(p)
            continue
        if target.is_dir():
            folders.append(target)
        elif p.is_file():
            files.append(p)
    return sorted(set(folders)), sorted(set(files))


def hidden_paths(home: Path, did_root: Path, case_dir: Path) -> tuple[list[Path], list[Path]]:
    """(folders, files) to hide: did's folder, the folders of the raw files it read, extras."""
    home, case_dir = home.resolve(), case_dir.resolve()
    folders: list[Path] = [did_root]
    files: list[Path] = []
    never = {Path("/"), home, *case_dir.parents}  # hiding these would hide the case itself
    for src in did_sources(did_root):
        parent = src.parent.resolve()
        if parent in never or case_dir.is_relative_to(parent):
            files.append(src)
        else:
            folders.append(parent)
    for p in _env_paths("WORKBENCH_SANDBOX_HIDE"):
        (folders if p.is_dir() else files).append(p)

    def hideable(p: Path) -> bool:
        return p.exists() and p.resolve() not in never and not case_dir.is_relative_to(p.resolve())

    folders = sorted({p.resolve() for p in folders if hideable(p)})
    files = sorted({p.resolve() for p in files if p.is_file()})
    return folders, files


def claude_binds(home: Path, case_dir: Path) -> list[str]:
    """Claude Code keeps its login and settings; other projects' transcripts, prompts, file
    history and pastes are swapped for this case's own (kept in the workbench state dir)."""
    claude = home / ".claude"
    if not claude.is_dir():
        return []
    own = paths.state_dir() / "claude" / hashlib.sha1(str(case_dir).encode()).hexdigest()[:16]
    a: list[str] = []
    for name in ("projects", "file-history", "paste-cache"):
        if (claude / name).is_dir():
            d = own / name
            d.mkdir(parents=True, exist_ok=True)
            a += ["--bind", str(d), str(claude / name)]
    hist = claude / "history.jsonl"
    if hist.exists():
        h = own / "history.jsonl"
        h.parent.mkdir(parents=True, exist_ok=True)
        h.touch(exist_ok=True)
        a += ["--bind", str(h), str(hist)]
    return a


# New sets live in anon/anon_set_<slug> and plain/plain_set_<slug>; an older store uses sets/<slug>.
# The layout table is shared with the Write tab (workbench.paths).
def evid_set_binds(evid_data: Path, evid_sets: list[str] | None) -> list[str]:
    """Hide every evid set root, then show only the sets this case listed."""
    roots: list[tuple[Path, str]] = []
    for bucket, prefix, _anon in paths.EVID_LAYOUT:
        root = (evid_data / bucket).resolve()
        if root.is_dir():
            roots.append((root, prefix))
    if not roots:
        return []
    argv: list[str] = []
    for root, _prefix in roots:
        argv += ["--tmpfs", str(root)]
    for slug in evid_sets or []:
        if not slug or "/" in slug or slug.startswith("."):
            continue
        for root, prefix in roots:
            d = root / f"{prefix}{slug}"
            if d.is_dir():
                argv += ["--ro-bind", str(d), str(d)]
    return argv


def bwrap_argv(
    case_dir: Path,
    evid_data: Path | None = None,
    evid_sets: list[str] | None = None,
    home: Path | None = None,
    skills: list[tuple[str, Path]] | None = None,
    did_root: Path | None = None,
    cases_root: Path | None = None,
    also: list[Path] | None = None,
) -> list[str]:
    """The `bwrap ... --` prefix for one agent terminal of CASE_DIR."""
    home = home or Path.home()
    case_dir = case_dir.resolve()
    did_root = (did_root or Path(os.environ.get("WORKBENCH_DID") or home / "did_workdirs")).expanduser()
    cases_root = (cases_root or Path(os.environ.get("WORKBENCH_CASES") or home / "cases")).expanduser()
    # / as it is, but a private /tmp: other programs' scratch files (did's agent among them) stay out
    a = ["bwrap", "--die-with-parent", "--unshare-pid", "--dev-bind", "/", "/", "--proc", "/proc", "--tmpfs", "/tmp"]

    folders, files = hidden_paths(home, did_root, case_dir)
    if also:
        more_dirs, more_files = conceal(home, did_root, case_dir, also)
        folders = sorted(set(folders) | set(more_dirs))
        files = sorted(set(files) | set(more_files))
    for d in folders:
        a += ["--tmpfs", str(d)]
    for f in files:
        a += ["--ro-bind", "/dev/null", str(f)]

    if evid_data is not None:  # the evid store shows only the listed sets
        a += evid_set_binds(evid_data, evid_sets)

    if cases_root.is_dir() and case_dir.is_relative_to(cases_root.resolve()):  # no other case
        a += ["--tmpfs", str(cases_root.resolve())]

    a += claude_binds(home, case_dir)

    if skills is not None:  # only the skills this case switched on, each from its real folder
        sk = home / ".claude" / "skills"
        a += ["--tmpfs", str(sk)]
        for name, src in skills:
            if name and "/" not in name and not name.startswith(".") and src.is_dir():
                a += ["--ro-bind", str(src), str(sk / name)]

    a += ["--bind", str(case_dir), str(case_dir), "--chdir", str(case_dir)]
    return [*a, "--"]


def sandbox_env(env: dict, evid_data: Path | None) -> dict:
    out = {**env, "WORKBENCH_SANDBOXED": "1"}
    if evid_data is not None:
        out["EVID_DB"] = str(evid_data)
    return out


def login_shell() -> str:
    """$SHELL, else the account's login shell (the app may start without $SHELL)."""
    if os.environ.get("SHELL"):
        return os.environ["SHELL"]
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except (ImportError, KeyError):
        return "/bin/sh"
