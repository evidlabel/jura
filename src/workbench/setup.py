"""``jura setup``: install what ``jura doctor`` finds missing or too old, asking first.

``plan`` turns what is missing into steps and runs nothing. ``run`` shows each step and runs it
after a yes. A step that needs sudo is asked for separately, and with ``--yes`` it is printed
and not run, so an agent harness that runs setup has to ask the user. Nothing that is
already installed is replaced, except evid or did older than this workbench needs.

- evid, did: ``uv tool install --upgrade`` from GitHub.
- git, bwrap: the distribution's package manager (sudo).
- typst, tinymist: a pinned release binary into ``~/.local/bin`` (no sudo).
- pdftotext (poppler), optional: the package manager (sudo).
- did's language models: ``did models <languages>``.
- Agent harnesses are only reported: logging in to one is the user's.
"""

from __future__ import annotations

import io
import lzma
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import agents, requires

START = "https://jura.sh/start/"
UV_SOURCES = {  # the same lines as the install steps on jura.sh
    "evid": ("evid @ git+https://github.com/evidlabel/evid",),
    "did": ("--python", "3.12", "did[gui,models] @ git+https://github.com/evidlabel/did"),
}
TYPST = "0.13.1"  # as in CI
TINYMIST = "0.15.8"
ARCH = {"x86_64": ("x86_64", "x64"), "aarch64": ("aarch64", "arm64"), "arm64": ("aarch64", "arm64")}
PACKAGES = {"git": "git", "bwrap": "bubblewrap"}  # the same name on every manager below
POPPLER = {"apt-get": "poppler-utils", "dnf": "poppler-utils", "pacman": "poppler", "zypper": "poppler-tools"}  # pdftotext
MANAGERS = {
    "apt-get": ("apt-get", "install", "-y"),
    "dnf": ("dnf", "install", "-y"),
    "pacman": ("pacman", "-S", "--needed", "--noconfirm"),
    "zypper": ("zypper", "--non-interactive", "install"),
}
LANGUAGES = ("da", "en")


@dataclass(frozen=True)
class Step:
    tool: str
    what: str  # why, in a few words
    argv: tuple[str, ...] = ()  # a command to run
    url: str = ""  # else a release binary (or .tar.xz holding one) for ~/.local/bin
    sudo: bool = False

    @property
    def runnable(self) -> bool:
        return bool(self.argv or self.url)

    @property
    def line(self) -> str:
        if self.argv:
            return shlex.join(self.argv)
        return f"download {self.url} into ~/.local/bin" if self.url else ""


def package_manager() -> str:
    return next((m for m in MANAGERS if shutil.which(m)), "")


def plan(
    missing: dict[str, str],
    *,
    uv: bool,
    manager: str,
    arch: str,
    tinymist: bool = True,
    poppler: bool = False,
    languages: tuple[str, ...] = (),
) -> list[Step]:
    """The steps for MISSING ({tool: problem}, as requires.check reports it), in the order they run.
    TINYMIST, POPPLER: whether those optional tools are missing. LANGUAGES: did models to fetch."""
    steps: list[Step] = []
    for name, src in UV_SOURCES.items():
        if name not in missing:
            continue
        if uv:
            steps.append(Step(name, missing[name], ("uv", "tool", "install", "--upgrade", *src)))
        else:
            steps.append(Step(name, f"{missing[name]}. Install uv first (https://docs.astral.sh/uv/), then run jura setup again"))
    system = [PACKAGES[n] for n in PACKAGES if n in missing]
    if system:
        if manager:
            steps.append(Step(" ".join(system), "system packages, with sudo", ("sudo", *MANAGERS[manager], *system), sudo=True))
        else:
            steps.append(Step(" ".join(system), f"install {' and '.join(system)} with your distribution's package manager"))
    typst_arch, tinymist_arch = ARCH.get(arch, ("", ""))
    if "typst" in missing:
        if typst_arch:
            url = f"https://github.com/typst/typst/releases/download/v{TYPST}/typst-{typst_arch}-unknown-linux-musl.tar.xz"
            steps.append(Step("typst", f"typst {TYPST}, compiles the case's documents", url=url))
        else:
            steps.append(Step("typst", f"no typst release for {arch}: see https://github.com/typst/typst"))
    if tinymist and tinymist_arch:
        url = f"https://github.com/Myriad-Dreamin/tinymist/releases/download/v{TINYMIST}/tinymist-linux-{tinymist_arch}"
        steps.append(Step("tinymist", f"tinymist {TINYMIST}, optional: the live Typst preview in Write", url=url))
    if poppler and manager:
        steps.append(Step(POPPLER[manager], "optional: pdftotext and pdftoppm (poppler) — did's plain-text fallback and the Write tab's PDF viewer", ("sudo", *MANAGERS[manager], POPPLER[manager]), sudo=True))
    if languages:
        steps.append(Step("did models", "the language models that find names", ("did", "models", *languages)))
    return steps


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def fetch(step: Step) -> Path:
    """Download STEP's binary (from a .tar.xz when it is one) into ~/.local/bin."""
    with urllib.request.urlopen(step.url, timeout=120) as r:
        data = r.read()
    if step.url.endswith(".tar.xz"):
        with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data))) as tar:
            member = next(m for m in tar.getmembers() if m.isfile() and Path(m.name).name == step.tool)
            data = tar.extractfile(member).read()
    dest = bin_dir() / step.tool
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        f.write(data)
    os.chmod(f.name, 0o755)
    os.replace(f.name, dest)
    return dest


def ask(question: str, default: bool = True) -> bool:
    try:
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def _missing() -> dict[str, str]:
    return {t.name: t.problem for t in map(requires.check, requires.REQUIRED) if t.problem}


def run(yes: bool = False, dry_run: bool = False, languages: str = "") -> None:
    """Show the plan, then run each step after a yes. --yes runs all but the sudo ones."""
    from .write.preview import find_tinymist

    if hasattr(sys.stdout, "reconfigure"):  # piped (curl | sh): keep our lines in order with the steps' output
        sys.stdout.reconfigure(line_buffering=True)
    missing = _missing()
    langs = tuple(languages.split()) if languages else LANGUAGES
    steps = plan(
        missing,
        uv=bool(shutil.which("uv")),
        manager=package_manager(),
        arch=platform.machine(),
        tinymist=find_tinymist() is None,
        poppler=shutil.which("pdftotext") is None or shutil.which("pdftoppm") is None,
    )
    print("jura setup: " + ("everything the workbench needs is installed." if not missing else f"{len(missing)} tool(s) to install or update."))
    for step in steps:
        print(f"\n{step.tool}: {step.what}")
        if not step.runnable:
            continue
        print(f"  {step.line}")
        if dry_run:
            continue
        if step.sudo and yes:
            print("  needs sudo: not run under --yes. Run it yourself, or ask the user to.")
            continue
        if not yes and not ask("  run it?"):
            continue
        _do(step)
    # did's models: did may only just have been installed
    if not requires.check("did").problem:
        if not yes and not dry_run:
            try:
                got = input(f"\ndid's language models, for the languages your documents are in [{' '.join(langs)}]: ").split()
            except EOFError:
                got = []
            langs = tuple(got) or langs
        (models,) = plan({}, uv=False, manager="", arch="", tinymist=False, languages=langs)
        print(f"\n{models.tool}: {models.what}\n  {models.line}")
        if not dry_run and (yes or ask("  run it?")):
            _do(models)
    harnesses = [a["name"] for a in agents.available() if a.get("command")]
    print("\nagent harnesses: " + (", ".join(harnesses) if harnesses else f"none found. Install one (Claude Code, Codex, OpenCode): {START}#agents"))
    if str(bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep) and any(s.url for s in steps):
        print(f"\nnote: {bin_dir()} is not on PATH; add it in your shell's startup file.")


def _do(step: Step) -> None:
    try:
        if step.url:
            print(f"  installed {fetch(step)}")
        else:
            r = subprocess.run(step.argv)
            print("  done" if r.returncode == 0 else f"  failed (exit {r.returncode})")
    except (OSError, ValueError, StopIteration, tarfile.TarError, lzma.LZMAError) as e:
        print(f"  failed: {e}")
