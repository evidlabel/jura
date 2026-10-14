"""Where things go in a case workdir: one opinionated layout, the check, and the PDF build.

``LAYOUT`` is the text an agent reads (it is written into every new case's ``AGENTS.md``
and printed by ``jura layout``). ``misplaced`` checks a workdir against it, so a file
in the wrong place is found by a command instead of derived from anywhere. Only kinds of
file with one clear home are checked: PDFs, Typst sources and citation files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import REFS

OUT = "out"  # every compiled PDF, mirroring its source path; in .gitignore
DOCS = "docs"  # every memo, notat, brief or letter: docs/<topic>/<name>.typ
REFS_EXTRA = "refs/extra"  # hand-added citations from outside evid; gather merges them into REFS
STORE = "evid"  # evid's local store (Hand off writes it)
ROOT_TYP = ("main.typ",)

LAYOUT = f"""\
## Where things go

Every file has one place. `jura layout` lists anything elsewhere, with the fix
(`git mv` it to its place, or `git rm` it).

```
AGENTS.md                these rules
workbench.yml            linked sets, skills, harnesses (the workbench writes it)
main.typ                 the case map: the question, the line, what is open
{DOCS}/<topic>/<name>.typ   every memo, notat, brief or letter: one folder per document,
                         its images and data beside it
{REFS}            the one citation file: `jura gather` writes it, never edit it
{REFS_EXTRA}/<name>.yml    citations from outside evid (a lawcite statute, …), merged into it
{STORE}/                    evid's local store (Hand off writes it): never touch
{OUT}/<path>.pdf             every PDF, mirroring its source, not in git:
                         {DOCS}/risk/notat.typ -> {OUT}/{DOCS}/risk/notat.pdf
```

- Build a PDF with `jura compile {DOCS}/risk/notat.typ`. It writes `{OUT}/{DOCS}/risk/notat.pdf`.
- A PDF anywhere but `{OUT}/` is in the wrong place: `git rm` it and compile again.
- A `.typ` outside `{DOCS}/` (other than `main.typ`) belongs in `{DOCS}/<topic>/`.
- Every `.typ`, in any folder, loads citations as `#setup(yaml("/{REFS}"))`. Any other
  `refs.yml` is not read.
"""


def out_path(rel: str) -> str:
    """Where the PDF of the Typst source REL goes: ``out/<rel>.pdf``."""
    return (Path(OUT) / Path(rel).with_suffix(".pdf")).as_posix()


@dataclass(frozen=True)
class Misplaced:
    path: str  # relative to the workdir
    why: str
    fix: str  # a command for the agent


def _files(workdir: Path) -> tuple[list[str], set[str]]:
    """(files git would see, the tracked ones). Ignored files are not in the wrong place."""
    def ls(*args: str) -> set[str]:
        r = subprocess.run(["git", "-C", str(workdir), "ls-files", "-z", *args], capture_output=True, text=True, check=True)
        return {p for p in r.stdout.split("\0") if p}

    try:
        tracked = ls("--cached")
        return sorted(tracked | ls("--others", "--exclude-standard")), tracked
    except (OSError, subprocess.CalledProcessError):
        out = []
        for top, dirs, names in os.walk(workdir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            out += [(Path(top) / n).relative_to(workdir).as_posix() for n in names]
        return sorted(out), set()


def misplaced(workdir: Path) -> list[Misplaced]:
    """Files in WORKDIR that are not where ``LAYOUT`` puts them."""
    files, tracked = _files(workdir)
    found = []
    for rel in files:
        top = rel.split("/", 1)[0]
        if top in (STORE, OUT) or Path(rel).name == ".gitkeep" or not (workdir / rel).is_file():
            continue
        rm = f"git rm -q -- {rel}" if rel in tracked else f"rm -- {rel}"
        name = Path(rel).name
        if rel.endswith(".pdf"):
            found.append(Misplaced(rel, f"a PDF outside {OUT}/", rm))
        elif rel.endswith(".typ") and rel not in ROOT_TYP and top != DOCS:
            dest = f"{DOCS}/{Path(rel).stem}/{name}"
            found.append(Misplaced(rel, f"Typst source outside {DOCS}/", f"git mv -- {rel} {dest}"))
        elif name == "refs.yml" and rel != REFS:
            found.append(Misplaced(rel, f"not read: the one citation file is {REFS}",
                                   f"move entries missing from {REFS} to {REFS_EXTRA}/<name>.yml, then {rm}"))
        elif top == "refs" and rel != REFS and not (rel.startswith(REFS_EXTRA + "/") and rel.endswith(".yml")):
            found.append(Misplaced(rel, f"refs/ holds only {REFS} and {REFS_EXTRA}/*.yml",
                                   f"git mv -- {rel} {REFS_EXTRA}/{Path(rel).stem}.yml" if rel.endswith(".yml") else rm))
    return found


def ensure_ignored(workdir: Path, entry: str = OUT + "/") -> None:
    """Add ENTRY to the workdir's .gitignore if it is not there (cases made before out/ existed)."""
    gi = workdir / ".gitignore"
    lines = gi.read_text("utf-8").splitlines() if gi.is_file() else []
    if entry not in lines and entry.rstrip("/") not in lines:
        gi.write_text("\n".join([*lines, entry]) + "\n", "utf-8")


def compile_pdf(workdir: Path, rel: str) -> dict:
    """typst compile REL (a .typ in WORKDIR) to ``out/<rel>.pdf``: {"pdf": path} or {"error": text}."""
    root = workdir.resolve()
    src = (root / rel).resolve()
    if not src.is_relative_to(root) or src.suffix != ".typ" or not src.is_file():
        return {"error": f"not a Typst file in the workdir: {rel}"}
    exe = os.environ.get("TYPST") or shutil.which("typst")
    if not exe:
        return {"error": "typst not found on PATH"}
    out = root / out_path(src.relative_to(root).as_posix())
    out.parent.mkdir(parents=True, exist_ok=True)
    ensure_ignored(root)
    r = subprocess.run([exe, "compile", "--root", str(root), str(src), str(out)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[-4000:] or f"typst exited {r.returncode}"}
    return {"pdf": out.relative_to(root).as_posix()}
