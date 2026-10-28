"""Harness buttons for the Write tab.

The shipped catalogue is ``agents.toml`` beside this module. ``$WORKBENCH_AGENTS``,
or ``~/.config/workbench/agents.toml`` when that file exists, replaces it. A button
appears when ``bin`` is on PATH (default: the first word of ``command``). An empty
command is the login shell and is always offered.

A row may list several configs of one harness (e.g. OpenCode on a local model and on a
global one) as separate rows. An optional ``env`` table is set on that command only,
without touching the shell around it — ``OPENCODE_CONFIG`` picks an opencode profile,
``<VAR>`` values are ``~``-expanded and quoted for the login shell. ``command`` itself
is typed verbatim, so extra flags fit there too.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

from workbench import paths

_SHELL = {"name": "shell", "command": "", "bin": "", "title": ""}
_SAFE_WORD = re.compile(r"[A-Za-z0-9_./:@%+=-]+")


def config_path() -> Path | None:
    """The override file. None means the shipped catalogue."""
    env = os.environ.get("WORKBENCH_AGENTS", "").strip()
    if env:
        return Path(env).expanduser()
    user = paths.config_dir() / "agents.toml"
    return user if user.is_file() else None


def shipped_text() -> str:
    return Path(__file__).with_name("agents.toml").read_text(encoding="utf-8")


def parse(text: str) -> list[dict]:
    """Catalogue rows. Unreadable TOML is the shell only. A missing shell row is appended."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [dict(_SHELL)]
    rows = data.get("agent") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [dict(_SHELL)]
    out: list[dict] = []
    has_shell = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        command = str(row.get("command") or "").strip()
        raw_env = row.get("env")
        env = {str(k).strip(): str(v) for k, v in raw_env.items() if str(k).strip()} if isinstance(raw_env, dict) else {}
        out.append(
            {
                "name": name,
                "command": command,
                "bin": str(row.get("bin") or "").strip(),
                "title": str(row.get("title") or "").strip(),
                "env": env,
            }
        )
        if command == "":
            has_shell = True
    if not has_shell:
        out.append(dict(_SHELL))
    return out or [dict(_SHELL)]


def catalogue() -> list[dict]:
    """Rows from the override file, else the shipped catalogue. A missing file is the shell only."""
    path = config_path()
    try:
        text = shipped_text() if path is None else path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [dict(_SHELL)]
    return parse(text)


def _word(value: str) -> str:
    """A shell word for one ``env`` value: ``~`` expanded, quoted only when it has to be."""
    value = os.path.expanduser(value)
    if _SAFE_WORD.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _line(row: dict) -> str:
    """``command`` as typed into the login shell, with the row's ``env`` in front of it."""
    command = row["command"]
    env = row.get("env") or {}
    if not command or not env:
        return command
    return " ".join(f"{k}={_word(v)}" for k, v in env.items()) + " " + command


def available(extra: list[dict] | None = None) -> list[dict]:
    """The harness buttons. ``extra`` (a case's own rows) comes first, and a name it repeats
    replaces the catalogue's (names match without case). A bare case row (``- claude``, or a
    row with only ``name:``) takes the catalogue row of that name. A case row whose ``bin`` is not on PATH stays, marked
    ``missing`` (the page greys it out); a catalogue row that is not installed is left out.
    The shell is always included."""
    cat = catalogue()
    by_name = {str(r.get("name") or "").strip().lower(): r for r in cat}
    out, seen = [], set()
    for row, own in [*((r, True) for r in extra or []), *((r, False) for r in cat)]:
        name = str(row.get("name") or "").strip()
        command = str(row.get("command") or "").strip()
        # `- claude`, or a row with only a name: the catalogue row of that name
        bare = own and command in ("", name) and not row.get("bin") and not row.get("title")
        if bare and name.lower() in by_name:
            row = by_name[name.lower()]
            name, command = str(row.get("name") or name).strip(), str(row.get("command") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        missing, probe = False, ""
        if command:
            probe = str(row.get("bin") or "").strip() or command.split()[0]
            missing = shutil.which(probe) is None
            if missing and not own:
                continue
        entry = {"name": name, "command": _line({**row, "command": command}), "title": str(row.get("title") or "").strip()}
        if missing:
            entry.update(missing=True, bin=probe)  # the program it looked for
        out.append(entry)
    if not any(r["command"] == "" for r in out):
        out.append({"name": "shell", "command": "", "title": ""})
    return out
