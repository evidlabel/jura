"""Cases: one workdir per case under the cases root, each a git repo with a ``workbench.yml``.

    ~/cases/<id>/            the agent's workdir (Write tab); git-tracked
      workbench.yml          id, name, linked evid and did sets, skill dirs, skills that are off
      AGENTS.md              how agents work in this workdir, and where every file goes
      main.typ               the case map: question, line, what is still open
      docs/                  every memo, notat, brief or letter: docs/<topic>/<name>.typ
      refs/                  refs.yml (Gather writes it) and extra/*.yml (hand-added citations)
      out/                   every compiled PDF (not in git)
      evid/                  local evid db: handed-off PDFs and local documents
    ~/did_workdirs/<set>/    did projects (raw PII) — the sets the did pane shows

``$WORKBENCH_CASES`` and ``$WORKBENCH_DID`` move the two roots (tests use them).
An older case may still have ``case.toml`` (and ``sources.yml``, ``skills.yml``,
``evid.yml``). Those keep loading until the next write, which folds them into
``workbench.yml``. A ``case.toml`` may name one ``evid_set`` and one ``did_project``;
those values keep loading. A new case does not create a did directory or a global evid set.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workbench import templates
from workbench.casefile import (
    CONFIG_NAME,
    DEFAULT_SKILL_DIRS,
    DEFAULT_SKILLS_OFF,
    CaseConfig,
    read_config,
    write_config,
)
from workbench.layout import DOCS, LAYOUT, OUT, REFS_EXTRA

# A new case's harness buttons: the catalogue rows of these names. One not installed is greyed out.
DEFAULT_AGENTS = ("claude", "codex", "grok", "opencode")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

AGENTS_MD = """# {id} · {name}

Workdir for the writing agents of this case. The workbench commits every change in the
background: the user's saves as `user:`, other changes as `agent:`.

- Read only the evid sets linked in `workbench.yml` (`evid -h` to discover the CLI).
  Global sets are the evid store. Local sets, including PDFs handed off from a did
  set, are `evid/` in this workdir (`evid --db evid`).
- Quote only through labquote keys (`#blockq(<key>)`, `#inlineq`, `#cite-ref`). Never retype a quote.
- Citations: this case has exactly one citation file, `refs/refs.yml` at the root of this
  workdir. Every `.typ`, in any folder, loads it as `#setup(yaml("/refs/refs.yml"))`.
  `jura gather` rewrites it from the linked sets and `refs/extra/*.yml`. Any other
  `refs.yml` is in the wrong place and is not read: do not create one, do not run
  `evid set gather` into the workdir, and do not search for the location (here or in the
  workbench's code). labquote prints only the entries a document cites, so the one file
  serves every memo.
- Where every other file goes is below. Do not derive it from anywhere else.
- Notes and feedback: `jura write fb ls`, `jura write edits`,
  `jura write context`. Answer each with `jura write fb reply ID "what changed (hash)" --done`.
- Personal data stays in `~/did_workdirs`. That directory is outside this sandbox on
  purpose. Do not try to reach it.
- `main.typ` is the case map. Memos and briefs are other files, under `docs/`. Keep the map current.
"""


# Placeholders __ID__ __NAME__ are replaced with Typst string literals, the others with a
# template's markup (templates.py).
# The file compiles on an empty case: no refs.yml, no labquote import, so the
# Write tab can preview it the moment the case exists.
MAIN_TYP = r"""// Strategy:
// - this file is the map of the case; a memo that answers the question is another file
// - it has to compile before any labels exist, so it does not import labquote
// - quote text never lands here; later it is a key into refs/refs.yml (the case's only citation file)

#let case-id = __ID__
#let case-name = __NAME__

#set document(title: "Steering · " + case-id, author: case-name)
#set page(
  paper: "a4",
  margin: (top: 2.2cm, bottom: 2cm, x: 2.1cm),
  header: context {
    set text(size: 8pt, fill: luma(115))
    align(right)[#case-id · #case-name]
  },
  footer: context {
    set text(size: 8pt, fill: luma(115))
    align(center, counter(page).display("1 / 1", both: true))
  },
)
#set text(size: 11pt, lang: "en")
#set heading(numbering: "1.1")
#set par(justify: true, leading: 0.65em)
#show heading.where(level: 1): it => {
  it
  v(0.3em)
  line(length: 100%, stroke: 0.6pt + luma(170))
}

#align(right)[#text(size: 9pt, fill: luma(90), datetime.today().display("[day] [month repr:long] [year]"))]

= Steering

The map for this case. It states the question, the line to try first, and what is still open.

== Question

__QUESTION__

== Line

One angle. A second angle is another memo.

- *Main.* __MAIN__
- *What would defeat it.* __DEFEAT__
- *Left alone.* __LEFT__

== Sources
__COLLECT__
Linked sets are in `workbench.yml`. Evid pills are global sets, or local documents under `evid/plain/`. Did pills pull PDFs into `evid/anon/`. A label such as \[PERSON 1\] is an identity: leave it as written.

Quotes are gathered into `refs/refs.yml`, the case's one citation file, and drawn at compile time. A `.typ` in any folder loads it as `yaml("/refs/refs.yml")`.

== To write

+ A memo that answers the question.
+ The passages it rests on, as labquote keys (`#blockq`, `#inlineq`, `#cite-ref`), once the labels exist.

#block(width: 100%, inset: (x: 12pt, y: 9pt), fill: luma(248), stroke: (left: 2.5pt + luma(70)))[
  Quote text is not typed into a `.typ` file. It is gathered into `refs/refs.yml` and drawn at compile time. Until that file exists, this note does not import labquote.
]

== Open

__OPEN__
"""


def cases_root() -> Path:
    return Path(os.environ.get("WORKBENCH_CASES") or Path.home() / "cases").expanduser()


def did_root() -> Path:
    return Path(os.environ.get("WORKBENCH_DID") or Path.home() / "did_workdirs").expanduser()


@dataclass(frozen=True)
class Case:
    id: str
    name: str
    workdir: Path
    evid_set: str
    did_project: Path | None

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "workdir": str(self.workdir),
            "evidSet": self.evid_set,
            "didProject": str(self.did_project) if self.did_project else "",
        }


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main_source(cid: str, name: str, template: str = "blank") -> str:
    """`main.typ` for a case: the map, its sections from TEMPLATE. ID and NAME become Typst string literals."""
    t = templates.get(template)
    collect = "".join(f"- {x}\n" for x in t.collect)
    parts = {
        "__QUESTION__": t.question,
        "__MAIN__": t.main,
        "__DEFEAT__": t.defeat,
        "__LEFT__": t.left_alone,
        "__COLLECT__": f"\nTo collect:\n\n{collect}" if collect else "",
        "__OPEN__": "\n".join(f"- {x}" for x in t.open),
        "__ID__": _toml_str(cid),
        "__NAME__": _toml_str(name),
    }
    return re.sub("|".join(parts), lambda m: parts[m.group(0)], MAIN_TYP)


def load(workdir: Path) -> Case:
    cfg = read_config(workdir)
    return Case(
        id=cfg.id,
        name=cfg.name,
        workdir=workdir.resolve(),
        evid_set=cfg.evid_set,
        did_project=Path(cfg.did_project).expanduser() if cfg.did_project else None,
    )


def list_cases() -> list[Case]:
    root = cases_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d / CONFIG_NAME).is_file() or (d / "case.toml").is_file():
            out.append(load(d))
    return out


def get(cid: str) -> Case:
    for c in list_cases():
        if c.id == cid:
            return c
    raise KeyError(cid)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def create(cid: str, name: str = "", template: str = "blank") -> Case:
    """A new case: the git-tracked workdir, its map from TEMPLATE (templates.py). Without a NAME it
    takes the template's, else the id. Did sets stay under the did root, outside the case."""
    t = templates.get(template)
    name = name or t.name or cid
    if not ID_RE.match(cid):
        raise ValueError(f"bad case id {cid!r}: letters, digits, '.', '_' or '-'")
    workdir = cases_root() / cid
    if workdir.exists():
        raise FileExistsError(f"{workdir} already exists")
    root = did_root().resolve()
    cases = cases_root().resolve()
    if root == cases or _is_relative_to(root, cases) or _is_relative_to(cases, root):
        raise ValueError("did's workspace must not be under the cases root")
    workdir.mkdir(parents=True)
    write_config(
        workdir,
        CaseConfig(
            id=cid,
            name=name,
            evid={"global": [], "local": []},
            did=[],
            skills_off=list(DEFAULT_SKILLS_OFF),
            skills_dirs=list(DEFAULT_SKILL_DIRS),
            agents=[{"name": n, "command": n} for n in DEFAULT_AGENTS],
        ),
    )
    (workdir / "AGENTS.md").write_text(AGENTS_MD.format(id=cid, name=name) + "\n" + LAYOUT, "utf-8")
    (workdir / "main.typ").write_text(main_source(cid, name, t.key), "utf-8")
    for keep in (DOCS, REFS_EXTRA):  # git keeps no empty folder
        (workdir / keep).mkdir(parents=True)
        (workdir / keep / ".gitkeep").write_text("", "utf-8")
    (workdir / OUT).mkdir()  # in .gitignore
    # evid's lock files and vector index sit inside evid/; its sets here have no .gitignore of their own.
    (workdir / ".gitignore").write_text(f"{OUT}/\n.pull-state.json\n.labels.lock\n.feedback.lock\nvecdb/\n", "utf-8")
    _git(workdir, "init", "-q")
    _git(workdir, "add", CONFIG_NAME, "AGENTS.md", "main.typ", f"{DOCS}/.gitkeep", f"{REFS_EXTRA}/.gitkeep", ".gitignore")
    _git(workdir, "commit", "-q", "-m", f"workbench: new case {cid}" + ("" if t.key == "blank" else f" ({t.key})"))
    return load(workdir)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        return path.is_relative_to(parent)
    except ValueError:
        return False
