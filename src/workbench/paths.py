"""Host paths and on-disk layouts shared by more than one module.

The workbench keeps its own state under ``$XDG_STATE_HOME/workbench`` (drafts, the queue
and each case's private Claude Code state) and reads overridable catalogues from
``$XDG_CONFIG_HOME/workbench`` (``agents.toml``, ``skills.toml``). Both fall back to the
usual ``~/.local/state`` and ``~/.config``.

``EVID_LAYOUT`` is where evid stores a set inside a data directory: ``(bucket, prefix,
anon)``. New sets live in ``anon/anon_set_<slug>`` and ``plain/plain_set_<slug>``; an
older store still uses ``sets/<slug>``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(name: str, fallback: Path) -> Path:
    base = os.environ.get(name, "").strip()
    return Path(base).expanduser() if base else fallback


def state_dir() -> Path:
    """``$XDG_STATE_HOME/workbench`` — drafts, the queue, each case's Claude state."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / "workbench"


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/workbench`` — where the ``agents.toml`` / ``skills.toml`` overrides live."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "workbench"


EVID_LAYOUT = (("anon", "anon_set_", True), ("plain", "plain_set_", False), ("sets", "", False))

# The one citation file of a case workdir, relative to its root. Gather writes it; every .typ
# loads it as yaml("/refs/refs.yml") (compiled with --root at the workdir). Any other refs.yml
# in the workdir is in the wrong place and is not read.
REFS = "refs/refs.yml"


def stray_refs(workdir: Path) -> list[str]:
    """Every ``refs.yml`` in WORKDIR other than :data:`REFS` (skipping ``.git``, hidden dirs and ``evid/``)."""
    out = []
    for top, dirs, files in os.walk(workdir):
        rel = Path(top).relative_to(workdir)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (rel == Path(".") and d == "evid"))
        if "refs.yml" in files and (rel / "refs.yml").as_posix() != REFS:
            out.append((rel / "refs.yml").as_posix())
    return sorted(out)
