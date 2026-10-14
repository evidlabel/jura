"""Child servers: each tab's app runs as its own process on a loopback port.

A child prints its URL on stdout (``WRITE_URL <url>`` for Write, ``open <url>`` for evid and
did). The supervisor reads that line, keeps the URL, echoes the child's stderr with a prefix,
and starts the child again if it dies (with a growing pause, so a crash loop does not spin).
"""

from __future__ import annotations

import ctypes
import os
import re
import signal
import subprocess
import sys
import threading
import time


def _child_setup() -> None:
    """In the child, before exec: its own session (Ctrl+C in a terminal does not hit it), and
    SIGTERM when the workbench dies however it dies, so no server outlives the window."""
    os.setsid()
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class _Spawner:
    """Every child is forked from this one long-lived thread. PR_SET_PDEATHSIG fires when the
    *thread* that forked a child ends, so a child forked from a request thread would be killed
    as soon as that request finished."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.cv = threading.Condition()
        threading.Thread(target=self._run, name="workbench-spawner", daemon=True).start()

    def _run(self) -> None:
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                fn, kwargs, box, done = self.jobs.pop(0)
            try:
                box["proc"] = subprocess.Popen(**kwargs, preexec_fn=fn)
            except OSError as e:
                box["error"] = e
            done.set()

    def popen(self, **kwargs) -> subprocess.Popen:
        box: dict = {}
        done = threading.Event()
        with self.cv:
            self.jobs.append((_child_setup, kwargs, box, done))
            self.cv.notify()
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["proc"]


_spawner = _Spawner()

URL_RE = re.compile(r"(?:WRITE_URL|open)\s+(http://127\.0\.0\.1:\d+/)")
RAISED_RE = re.compile(r"Raised the running .* GUI \((http://127\.0\.0\.1:\d+/)\)")


class Child:
    """One child server. ``url`` is None until it has printed one."""

    def __init__(self, name: str, argv: list[str], env: dict | None = None, cwd: str | None = None):
        self.name, self.argv, self.cwd = name, argv, cwd
        # Python children block-buffer a piped stdout; the URL line must arrive at once.
        self.env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
        self.url: str | None = None
        self.error: str = ""
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()
        self.stopping = False
        self.restarts = 0
        self.external = False  # the URL is an instance that was already running (it raised and we reuse it)
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.stopping = False
            self.ready.clear()
            try:
                self.proc = _spawner.popen(
                    args=self.argv,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                self.error = f"cannot start {self.argv[0]}: {e}"
                print(f"workbench: {self.name}: {self.error}", file=sys.stderr, flush=True)
                self.ready.set()
                return
        proc = self.proc
        threading.Thread(target=self._read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()

    def _read_out(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            raised = RAISED_RE.search(line)
            m = raised or URL_RE.search(line)
            if m and not self.ready.is_set():
                self.url, self.error, self.external = m.group(1), "", bool(raised)
                self.restarts = 0
                self.ready.set()
            print(f"[{self.name}] {line.rstrip()}", file=sys.stderr, flush=True)

    def _read_err(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            print(f"[{self.name}] {line.rstrip()}", file=sys.stderr, flush=True)

    def _watch(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        if self.stopping or proc is not self.proc:
            return
        if self.external and code == 0:
            return  # it handed us the instance that was already running; nothing to restart
        self.error = f"{self.name} exited with code {code}"
        print(f"workbench: {self.error}; restarting", file=sys.stderr, flush=True)
        self.url = None
        self.ready.set()  # wake waiters; they see the error
        self.restarts += 1
        time.sleep(min(30.0, 0.5 * 2 ** min(self.restarts, 6)))
        if not self.stopping:
            self.start()

    def wait_url(self, timeout: float = 30.0) -> str | None:
        self.ready.wait(timeout)
        return self.url

    def stop(self) -> None:
        self.stopping = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def as_json(self) -> dict:
        alive = bool(self.proc and self.proc.poll() is None)
        return {"name": self.name, "url": self.url, "alive": alive, "error": self.error}


class Supervisor:
    """All children, by key (``write:<case>``, ``evid``, ``did``)."""

    def __init__(self) -> None:
        self.children: dict[str, Child] = {}
        self.lock = threading.Lock()

    def ensure(self, key: str, make) -> Child:
        with self.lock:
            child = self.children.get(key)
            if child is None:
                child = self.children[key] = make()
        child.start()
        return child

    def get(self, key: str) -> Child | None:
        return self.children.get(key)

    def stop(self) -> None:
        for c in list(self.children.values()):
            c.stop()
