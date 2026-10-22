"""Run a command inside the workbench sandbox, for a child app that has no case of its own.

evid's Agent pane starts a set's terminal with ``EVID_TERM_WRAP`` pointing here. The pane runs
in evid's process, which serves every case and so cannot know the case sandbox; this asks the
workbench server for the prefix its Write terminals use on the current case (the same
``workbench.sandbox.bwrap_argv``, from the Write child) and execs it with the command evid
appended: ``EVID_TERM_WRAP="<python> -m workbench.sandbox_exec"``, then ``[shell, "-i"]``.

It fails closed: if the workbench does not answer, no terminal starts. When the sandbox is off
there (``WORKBENCH_SANDBOX=0`` or no bubblewrap), the prefix is empty and the shell runs plainly,
exactly as a Write terminal would.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen


def prefix() -> list[str]:
    """The bwrap argv prefix for a terminal on the workbench's current case."""
    base = (os.environ.get("WORKBENCH_SHELL_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("WORKBENCH_SHELL_URL is not set")
    with urlopen(Request(base + "/api/sandbox"), timeout=60) as r:
        data = json.load(r)
    return [str(a) for a in (data.get("argv") or [])]


def main(argv: list[str]) -> int:
    if not argv:
        print("jura: sandbox-exec needs a command, e.g. /bin/bash -i", file=sys.stderr)
        return 2
    try:
        pre = prefix()
    except (OSError, ValueError, RuntimeError) as e:
        print(f"jura: the workbench did not answer for the case sandbox: {e}", file=sys.stderr)
        return 1
    if not pre:
        os.execvp(argv[0], argv)  # the sandbox is off, as in the Write tab
    os.execvp(pre[0], [*pre, *argv])
    return 1  # not reached


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
