"""workbench CLI: open the window, manage cases, and (for agents) the Write tab's notes and feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from treeparse import argument, cli, command, group, option

from . import __version__, cases
from .templates import TEMPLATES
from .write import core as write


def open_window(port: int, browser: bool, headless: bool, agent: str) -> None:
    from .server import serve

    serve(port=port, browser=browser, headless=headless, agent=agent)


def case_new(id: str, name: str, template: str) -> None:
    try:
        c = cases.create(id, name, template)
    except (ValueError, FileExistsError) as e:
        sys.exit(f"workbench: {e}")
    print(f"{c.id}  {c.workdir}\n  did  {cases.did_root()}")


def _case_root(root: str) -> Path:
    """The case workdir that holds ROOT (it has a workbench.yml), for the agent commands."""
    start = Path(root).resolve()
    workdir = next((d for d in (start, *start.parents) if (d / "workbench.yml").is_file() or (d / "case.toml").is_file()), None)
    if workdir is None:
        sys.exit(f"workbench: {start} is not inside a case workdir (no workbench.yml)")
    return workdir


def gather(root: str) -> None:
    """Rewrite the case's one citation file, refs/refs.yml, from its linked sets and refs/extra/."""
    from .paths import REFS
    from .write import sources

    workdir = _case_root(root)
    r = sources.gather(workdir)
    for e in r["errors"]:
        print(f"error  {e}", file=sys.stderr)
    for p in r["stray"]:
        print(f"stray  {p}  (not read: the case's citations are {REFS} only)", file=sys.stderr)
    if not r["ref"]:
        sys.exit("workbench: nothing gathered (no linked sets, or every set failed)")
    print(workdir / r["ref"])


def layout_check(root: str) -> None:
    """Print where things go in a case, then every file that is somewhere else, with its fix."""
    from . import layout

    workdir = _case_root(root)
    print(layout.LAYOUT)
    wrong = layout.misplaced(workdir)
    if not wrong:
        print("Everything is in its place.")
        return
    print(f"In the wrong place ({len(wrong)}):")
    for m in wrong:
        print(f"  {m.path}  ({m.why})\n    {m.fix}")
    sys.exit(1)


def compile_doc(path: str) -> None:
    """typst compile PATH into out/, mirroring its path: docs/x/memo.typ -> out/docs/x/memo.pdf."""
    from . import layout

    src = Path(path).resolve()
    workdir = _case_root(str(src.parent))
    r = layout.compile_pdf(workdir, src.relative_to(workdir).as_posix() if src.is_relative_to(workdir) else path)
    if "error" in r:
        sys.exit(r["error"])
    print(workdir / r["pdf"])


def doctor() -> None:
    """Every tool the workbench runs: where it is, its version, and what is wrong. Exit 1 if a required one is."""
    if not _doctor_table():
        print("\n`jura setup` installs what is missing or too old (it asks before each step).")
        sys.exit(1)


def setup(yes: bool, dry_run: bool, languages: str) -> None:
    """Install what doctor finds missing, asking first; then doctor's table."""
    from . import setup as setup_

    setup_.run(yes=yes, dry_run=dry_run, languages=languages)
    print()
    ok = _doctor_table()
    print("\nNext: jura open" if ok else "\nStill missing: see the rows marked !! above, or " + setup_.START)
    if not ok and not dry_run:
        sys.exit(1)


def _doctor_table() -> bool:
    """Print doctor's rows. False if a required tool is missing or too old."""
    import shutil

    from . import requires
    from .server import find_app
    from .write.preview import find_tinymist

    found = {"tinymist": find_tinymist, "pdftotext": lambda: shutil.which("pdftotext"), "pdftoppm": lambda: shutil.which("pdftoppm"), "workbench-app": find_app}
    bad = False
    for name in requires.REQUIRED:
        t = requires.check(name)
        bad = bad or bool(t.problem)
        need = f"  (needs {requires.MINIMUM[name]}+)" if name in requires.MINIMUM else ""
        print(f"{'!!' if t.problem else 'ok'}  {name:<14} {t.version or '-':<8} {t.path or 'not found'}{need}")
        if t.problem:
            print(f"    {t.problem}")
    for name, without in requires.OPTIONAL.items():
        path = found[name]()
        print(f"{'ok' if path else '--'}  {name:<14} {'-':<8} {path or f'not found ({without})'}")
    return not bad


def case_ls(format: str) -> None:
    all_cases = cases.list_cases()
    if format == "json":
        print(json.dumps([c.as_json() for c in all_cases], indent=2))
        return
    if not all_cases:
        print(f"no cases in {cases.cases_root()}")
    for c in all_cases:
        print(f"{c.id:<16} {c.name:<32} {c.workdir}")


# `jura write ...` is the Write tab's own CLI (serve, print, annotation, edits, context, fb).
write.app.name = "write"
write.app.help = "The Write tab: serve one case workdir; notes, feedback and the user's edits for agents."

app = cli(
    name="jura",
    help="jura workbench: write a case with agents next to its datasets (evid) and anonymization (did).",
    version=__version__,
    subgroups=[
        cli(
            name="open",
            help="Open the workbench window (workbench-app, else the browser).",
            callback=open_window,
            options=[
                option(flags=["--port", "-p"], arg_type=int, default=8780, help="port (next free one if taken)"),
                option(flags=["--browser"], flag=True, help="use the web browser instead of the app window"),
                option(flags=["--headless"], flag=True, help="only serve; open no window"),
                option(
                    flags=["--agent", "-a"],
                    arg_type=str,
                    default="",
                    help="command for the first agent terminal in Write (default $WORKBENCH_AGENT). Unset, the pane stays closed",
                ),
            ],
        ),
        group(
            name="case",
            help="Cases: one git-tracked workdir each. Did sets live under $WORKBENCH_DID.",
            commands=[
                command(
                    name="new",
                    help="Create a case workdir under $WORKBENCH_CASES (~/cases). Did sets stay under $WORKBENCH_DID (~/did_workdirs).",
                    callback=case_new,
                    arguments=[
                        argument(name="id", arg_type=str, help="case id, e.g. 2026-014"),
                        argument(name="name", arg_type=str, nargs="?", default="", help="display name (default: the template's, else the id)"),
                    ],
                    options=[
                        option(
                            flags=["--template", "-t"],
                            arg_type=str,
                            default="blank",
                            choices=list(TEMPLATES),
                            help="what main.typ, the case map, starts with: a plain question to correct",
                        )
                    ],
                ),
                command(
                    name="ls",
                    help="List cases.",
                    callback=case_ls,
                    options=[
                        option(
                            flags=["--format", "-f"],
                            arg_type=str,
                            default="table",
                            choices=["table", "json"],
                            help="output format",
                        )
                    ],
                ),
            ],
        ),
        cli(
            name="layout",
            help="Where things go in a case workdir, and every file that is somewhere else (with the git mv / git rm). Exit 1 if any.",
            callback=layout_check,
            options=[option(flags=["--root", "-C"], arg_type=str, default=".", help="any folder inside the case workdir")],
        ),
        cli(
            name="compile",
            help="Compile a .typ to out/, mirroring its path: docs/x/memo.typ -> out/docs/x/memo.pdf.",
            callback=compile_doc,
            arguments=[argument(name="path", arg_type=str, help="the .typ file")],
        ),
        cli(
            name="gather",
            help="Rewrite the case's one citation file, refs/refs.yml, from the linked sets and refs/extra/*.yml. Lists stray refs.yml files.",
            callback=gather,
            options=[option(flags=["--root", "-C"], arg_type=str, default=".", help="any folder inside the case workdir")],
        ),
        cli(
            name="doctor",
            help="Check the tools the workbench runs (evid, did, typst, git, bwrap; tinymist and workbench-app optional). Exit 1 if one is missing or too old.",
            callback=doctor,
        ),
        cli(
            name="setup",
            help="Install what doctor finds missing or too old: evid and did (uv), git and bwrap (your package manager, with sudo), typst and tinymist (~/.local/bin), did's language models. Asks before each step.",
            callback=setup,
            options=[
                option(flags=["--yes", "-y"], flag=True, help="run every step without asking, except the ones that need sudo (printed instead)"),
                option(flags=["--dry-run", "-n"], flag=True, help="only print the steps"),
                option(flags=["--languages", "-l"], arg_type=str, default="", help="did's model languages, e.g. 'da en' (default: asked; da en)"),
            ],
        ),
        write.app,
    ],
)


def main(argv=None):
    if argv is not None:
        sys.argv = ["jura", *argv]
    try:
        app.run()
    except write.HTTPError as e:
        sys.exit(f"jura: {e.msg}")
    except BrokenPipeError:
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)


if __name__ == "__main__":
    main()
