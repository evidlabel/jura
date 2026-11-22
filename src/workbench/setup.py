"""``jura setup``: install what ``jura doctor`` finds missing or too old, asking first.

``plan`` turns what is missing into steps and runs nothing. ``run`` shows each step and runs it
after a yes. A step that needs sudo is asked for separately, and with ``--yes`` it is printed
and not run, so an agent harness that runs setup has to ask the user. Nothing that is
already installed is replaced, except evid or did older than this workbench needs.

- evid, did, and the source tools hudoc, lawcite, domdb: ``uv tool install --upgrade`` from GitHub.
- git, bwrap: the distribution's package manager (sudo).
- typst, tinymist: a pinned release binary into ``~/.local/bin`` (no sudo).
- pdftotext (poppler), optional: the package manager (sudo).
- workbench-app, optional: the native Tauri window, into ``~/.local/bin`` (no sudo). It is
  built for each workbench release and attached to that release (tag ``v<version>``), so
  setup fetches the one for this workbench's version, and replaces one in ``~/.local/bin``
  built for another. The webkit2gtk runtime libraries it links against are a separate sudo
  step, asked for on their own. Without it the browser is the window; building from source
  is ``make app-deps && make app-build`` (a checkout with cargo).
- did's language models: ``did models <languages>``.
- Agent harnesses are only reported: logging in to one is the user's.
"""

from __future__ import annotations

import io
import lzma
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, agents, requires

START = "https://jura.sh/start/"
UV_SOURCES = {  # the same lines as the install steps on jura.sh
    "evid": ("evid @ git+https://github.com/evidlabel/evid",),
    "did": ("--python", "3.12", "did[gui,models] @ git+https://github.com/evidlabel/did"),
    "hudoc": ("hudoc @ git+https://github.com/evidlabel/hudoc",),
    "lawcite": ("lawcite @ git+https://github.com/evidlabel/lawcite",),
    "domdb": ("domdb @ git+https://github.com/evidlabel/domdb",),
}
TYPST = "0.13.1"  # as in CI
TINYMIST = "0.15.8"
APP_URL = "https://github.com/evidlabel/jura/releases/download/v{version}/workbench-app-{arch}.tar.xz"
_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
ARCH = {"x86_64": ("x86_64", "x64"), "aarch64": ("aarch64", "arm64"), "arm64": ("aarch64", "arm64")}
PACKAGES = {"git": "git", "bwrap": "bubblewrap"}  # the same name on every manager below
POPPLER = {"apt-get": "poppler-utils", "dnf": "poppler-utils", "pacman": "poppler", "zypper": "poppler-tools"}  # pdftotext
RUNTIME = {  # the webkit2gtk-4.1 runtime libraries workbench-app links against
    "apt-get": ("libwebkit2gtk-4.1-0", "libgtk-3-0", "libsoup-3.0-0", "libjavascriptcoregtk-4.1-0"),
    "dnf": ("webkit2gtk4.1", "gtk3", "libsoup3"),
    "pacman": ("webkit2gtk-4.1", "gtk3", "libsoup3"),
}  # zypper and others: the advise step names what to look for
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


def app_version(version: str = __version__) -> str:
    """The workbench version as the window's (Cargo, semver) version: 0.3.1a1 -> 0.3.1-alpha.1.
    The window is built for exactly one workbench release, so the two always match."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if not m:
        return version
    base, pre, n = m.groups()
    return f"{base}-{_PRE[pre]}.{n}" if pre else base


def installed_app_version(exe: str) -> str:
    """The version EXE says it was built for (``workbench-app <version>``), or "". A window from
    before 0.3.1a1 has no --version: it would start a workbench with that flag, so it runs with
    WORKBENCH_BIN=false, which makes it exit at once (and its version reads as unknown)."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                           env={**os.environ, "WORKBENCH_BIN": "false"})
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.fullmatch(r"workbench-app (\S+)", r.stdout.strip())
    return m.group(1) if r.returncode == 0 and m else ""


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
    app: bool = False,
    webkit: bool = False,
    app_stale: bool = False,
    app_have: str = "",
    languages: tuple[str, ...] = (),
) -> list[Step]:
    """The steps for MISSING ({tool: problem}, as requires.check reports it), in the order they run.
    TINYMIST, POPPLER: whether those optional tools are missing. APP: workbench-app is to be installed;
    APP_STALE: it replaces one built for another version (APP_HAVE, "" when unknown); WEBKIT: the runtime
    libraries the app links against are missing (then a sudo step before the download).
    LANGUAGES: did models to fetch."""
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
    if app:
        if typst_arch:
            if webkit:
                if manager in RUNTIME:
                    steps.append(Step("webkit2gtk runtime", "the libraries the app window links against, with sudo", ("sudo", *MANAGERS[manager], *RUNTIME[manager]), sudo=True))
                else:
                    steps.append(Step("webkit2gtk runtime", "optional: install the webkit2gtk-4.1 runtime libraries with your distribution's package manager"))
            url = APP_URL.format(version=__version__, arch=typst_arch)
            if app_stale:
                what = f"workbench-app {app_have or '(no version: from before 0.3.1a1)'} → {app_version()}: the window for this workbench"
            else:
                what = f"workbench-app {app_version()}, optional: the native window (else the browser)"
            steps.append(Step("workbench-app", f"{what}; or build from source: make app-deps && make app-build", url=url))
        else:
            steps.append(Step("workbench-app", f"no workbench-app release for {arch}: the browser is the window. Build from source: make app-deps && make app-build"))
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
    """Required tools that are missing or too old, then the source tools (asked like the rest)."""
    required = {t.name: t.problem for t in map(requires.check, requires.REQUIRED) if t.problem}
    return {**required, **requires.source_problems()}


def webkit_runtime() -> bool:
    """True when the webkit2gtk-4.1 libraries workbench-app links against are missing."""
    try:
        r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return "libwebkit2gtk-4.1.so" not in r.stdout


def run(yes: bool = False, dry_run: bool = False, languages: str = "") -> None:
    """Show the plan, then run each step after a yes. --yes runs all but the sudo ones."""
    from .server import find_app
    from .write.preview import find_tinymist

    if hasattr(sys.stdout, "reconfigure"):  # piped (curl | sh): keep our lines in order with the steps' output
        sys.stdout.reconfigure(line_buffering=True)
    missing = _missing()
    app_path = find_app()
    app_have = installed_app_version(app_path) if app_path else ""
    # only a window setup put in ~/.local/bin is replaced; one from WORKBENCH_APP or a build is the user's
    app_stale = bool(app_path) and Path(app_path).parent == bin_dir() and app_have != app_version()
    app_missing = app_path is None or app_stale
    langs = tuple(languages.split()) if languages else LANGUAGES
    steps = plan(
        missing,
        uv=bool(shutil.which("uv")),
        manager=package_manager(),
        arch=platform.machine(),
        tinymist=find_tinymist() is None,
        poppler=shutil.which("pdftotext") is None or shutil.which("pdftoppm") is None,
        app=app_missing,
        webkit=webkit_runtime() if app_missing else False,
        app_stale=app_stale,
        app_have=app_have,
    )
    print("jura setup: " + ("everything the workbench needs is installed." if not missing and not app_missing else f"{len(missing) + app_missing} tool(s) to install or update."))
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
