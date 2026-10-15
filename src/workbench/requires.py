"""The tools this workbench needs, and whether the installed ones are there and new enough.

evid and did change their on-disk layout and their protocol with the workbench together
with it, so each has a minimum version. The rest only need to be on PATH. ``problems``
is what the window and ``jura open`` show; ``jura doctor`` prints every row.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

MINIMUM = {"evid": "0.8.0", "did": "0.3.2"}
REQUIRED = ("evid", "did", "typst", "git", "bwrap")
OPTIONAL = {
    "tinymist": "no live Typst preview",
    "pdftotext": "did reads fewer PDFs with a broken text layer",
    "workbench-app": "the browser is the window",
}  # what is lost without it

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Tool:
    name: str
    path: str  # "" when not found
    version: str  # "" when unknown
    problem: str  # "" when fine


def _exe(name: str) -> str:
    """$WORKBENCH_<NAME>_BIN when set (and "" if that is not a command), else NAME on PATH."""
    env = os.environ.get(f"WORKBENCH_{name.upper().replace('-', '_')}_BIN", "").strip()
    if env:
        return env if os.access(env, os.X_OK) else (shutil.which(env) or "")
    return shutil.which(name) or ""


def _key(v: str) -> tuple[int, ...]:
    m = _VERSION.search(v)
    return tuple(int(x) for x in m.groups()) if m else ()


def version_of(exe: str) -> str:
    """The first x.y.z that ``EXE --version`` prints, or ""."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = _VERSION.search(r.stdout + r.stderr)
    return m.group(0) if m else ""


def check(name: str) -> Tool:
    exe = _exe(name)
    if not exe:
        need = f" {MINIMUM[name]} or newer" if name in MINIMUM else ""
        return Tool(name, "", "", f"{name}{need} is not installed (not on PATH)")
    if name not in MINIMUM:
        return Tool(name, exe, "", "")
    have = version_of(exe)
    if not have:
        return Tool(name, exe, "", f"{name} did not print a version; this workbench needs {MINIMUM[name]} or newer")
    if _key(have) < _key(MINIMUM[name]):
        return Tool(name, exe, have, f"{name} {have} is older than this workbench needs ({MINIMUM[name]} or newer)")
    return Tool(name, exe, have, "")


def problems() -> list[str]:
    """One line per required tool that is missing or too old."""
    return [t.problem for t in map(check, REQUIRED) if t.problem]
