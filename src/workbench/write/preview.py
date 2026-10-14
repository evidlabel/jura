"""The Write tab's Typst preview: one `tinymist preview` per open .typ file.

tinymist serves the rendered pages (data plane, shown in an iframe next to the editor) and takes
editor events on its control plane: unsaved buffers (`updateMemoryFiles`) so the preview follows
typing, and cursor jumps (`panelScrollTo`). It asks the editor to scroll with `editorScrollTo`.

The binary: $TINYMIST, else `tinymist` on PATH, else the newest VS Code extension's copy.
Previews nobody looked at for IDLE seconds stop; at most MAX run at once (oldest stops first).

A WebSocket close frame makes tinymist shut the control listener (`unsupported close message`)
while the process keeps serving the last render. The page therefore keeps the socket open, and
`open(..., restart=True)` starts a new process once that socket has died.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from workbench.supervisor import _spawner

IDLE = 600.0
MAX = 3
ERROR_GAP = 1.0  # seconds: error blocks closer than this came from one compile
LISTEN = re.compile(r"(Control panel|Data plane) server listening on: (127\.0\.0\.1:\d+)")
# Any of these means the control listener is gone while the process may still be serving.
_CONTROL_DEAD = (
    "unsupported close message",
    "unsupported binary message",
    "unsupported ping message",
    "unsupported pong message",
    "unsupported frame message",
    "Connection reset without closing handshake",
)


def find_tinymist() -> str | None:
    env = os.environ.get("TINYMIST")
    if env:
        return env if os.access(env, os.X_OK) else None
    hit = shutil.which("tinymist")
    if hit:
        return hit
    ext = Path.home() / ".vscode" / "extensions"
    builds = sorted(ext.glob("myriad-dreamin.tinymist-*/out/tinymist"), key=lambda p: p.stat().st_mtime)
    return str(builds[-1]) if builds else None


class Preview:
    def __init__(self, rel: str, proc: subprocess.Popen):
        self.rel, self.proc = rel, proc
        self.data = self.control = ""
        self.ready = threading.Event()
        self.used = time.monotonic()
        self.broken = False  # control listener shut itself; the data plane may still be up
        self.log: list[str] = []
        self.root = ""  # stripped from the paths in an error, so they read as workdir paths
        self.errors: list[str] = []  # the error blocks of the latest failed compile
        self._cur: list[str] | None = None
        self._last = 0.0

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _diag(self, line: str) -> bool:
        """Collect tinymist's ``error:`` blocks (each ends at a blank line). Blocks printed within
        ERROR_GAP seconds of each other are one compile; a later one starts a new list."""
        if line.startswith("error:"):
            self._cur = [line]
            return True
        if self._cur is None:
            return False
        if line:
            self._cur.append(line.replace(self.root, "") if self.root else line)
            return True
        now = time.monotonic()
        block, self._cur = "\n".join(self._cur), None
        kept = [b for b in self.errors if b != block] if now - self._last < ERROR_GAP else []
        self.errors = [*kept[-4:], block]
        self._last = now
        return True

    def read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip()
            self.log = [*self.log, line][-40:]
            if self._diag(line):
                continue
            m = LISTEN.search(line)
            if m:
                if m.group(1) == "Data plane":
                    self.data = f"http://{m.group(2)}/"
                else:
                    self.control = f"ws://{m.group(2)}/"
                if self.data and self.control:
                    self.ready.set()
            elif " ERROR " in line or " WARN " in line:
                if any(s in line for s in _CONTROL_DEAD):
                    self.broken = True
                print(f"[tinymist {self.rel}] {line}", file=sys.stderr, flush=True)
        self.ready.set()  # it exited: wake the waiter

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Previews:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.Lock()
        self.running: dict[str, Preview] = {}
        threading.Thread(target=self._reap, daemon=True).start()

    def open(self, rel: str, *, restart: bool = False) -> dict:
        """Start (or reuse) the preview of REL; {"data": url, "control": ws url} or {"error": ...}.

        restart drops a live process first. The control listener does not survive a socket close,
        so a preview whose socket died has to be started again.
        """
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root) or path.suffix != ".typ" or not path.is_file():
            return {"error": f"not a Typst file in the workdir: {rel}"}
        exe = find_tinymist()
        if not exe:
            return {"error": "tinymist not found — `make deps` installs it, or set $TINYMIST"}
        with self.lock:
            p = self.running.get(rel)
            if p and (restart or p.broken or not p.alive()):
                self.running.pop(rel).stop()
                p = None
            if p is None:
                while len(self.running) >= MAX:
                    oldest = min(self.running.values(), key=lambda x: x.used)
                    self.running.pop(oldest.rel).stop()
                proc = _spawner.popen(
                    args=[
                        exe,
                        "preview",
                        "--no-open",
                        "--data-plane-host",
                        "127.0.0.1:0",
                        "--control-plane-host",
                        "127.0.0.1:0",
                        "--root",
                        str(self.root),
                        str(path),
                    ],
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                p = self.running[rel] = Preview(rel, proc)
                p.root = str(self.root) + "/"
                threading.Thread(target=p.read, daemon=True).start()
            p.used = time.monotonic()
        if not p.ready.wait(20) or not p.data:
            return {"error": "tinymist did not start:\n" + "\n".join(p.log[-8:])}
        return {"data": p.data, "control": p.control, "path": rel}

    def errors(self, rel: str) -> list[str]:
        """The error blocks of REL's latest failed compile (empty when none was printed)."""
        with self.lock:
            p = self.running.get(rel)
            return list(p.errors) if p else []

    def touch(self, rel: str) -> None:
        with self.lock:
            p = self.running.get(rel)
            if p:
                p.used = time.monotonic()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            with self.lock:
                for rel, p in list(self.running.items()):
                    if not p.alive() or time.monotonic() - p.used > IDLE:
                        self.running.pop(rel).stop()

    def stop(self) -> None:
        with self.lock:
            for p in self.running.values():
                p.stop()
            self.running.clear()
