"""The workbench window's server: the shell page, the case list and the child servers.

Stdlib only, loopback only. Guards: the Host header must be loopback (DNS rebinding), and every
mutating ``/api`` request carries ``X-Workbench: 1`` (a header a foreign page cannot set without
a CORS preflight we never answer).

Tabs (each an iframe onto its own child server, kept running while hidden):

- Write          ``python -m workbench.write serve <workdir>`` — one per opened case
- Datasets       ``evid gui --headless --replace`` — this workbench's own process.
                 ``--replace`` quits an evid GUI already serving the data directory
                 so the process is not left running under an older workbench.
                 The open case's ``evid/`` is connected beside the global data directory,
                 so both can be labelled
- Anonymization  ``did gui --headless`` — one. Its projects are the did sets. Write connects
                 to those sets and hands them off as PDFs into the case's ``evid/``.

``POST /api/quit`` (Ctrl+W / Ctrl+Q) closes the window and stops this process.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import __version__, cases, requires, templates
from .supervisor import Child, Supervisor

STATIC = resources.files(__package__) / "static"
VENDOR = resources.files(__package__) / "write" / "vendor"
TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class HTTPError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def free_port(start: int) -> int:
    """The first port from START that a server can listen on. The probe allows reuse as the
    server does, so a port left in TIME_WAIT by a workbench that just quit counts as free."""
    for p in range(start, start + 50):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}-{start + 49}")


def evid_db_args() -> list[str]:
    """`--db DIR` when the workbench runs on its own evid data dir ($WORKBENCH_EVID_DB)."""
    db = os.environ.get("WORKBENCH_EVID_DB", "").strip()
    return ["--db", str(Path(db).expanduser())] if db else []


def tool(name: str) -> str:
    """The evid / did command: $WORKBENCH_EVID_BIN / $WORKBENCH_DID_BIN, else on PATH."""
    return os.environ.get(f"WORKBENCH_{name.upper()}_BIN") or shutil.which(name) or name


class Workbench:
    """State shared by every request: the supervisor and the current case."""

    def __init__(self, agent: str = ""):
        self.sup = Supervisor()
        self.agent = agent
        self.current: str | None = None
        self.lock = threading.Lock()
        self.badges: dict[str, int] = {}
        self.url = ""  # this server's own URL, for the Write children to find evid through
        self.window: subprocess.Popen | None = None  # the workbench-app process, when serve() opened one
        self.httpd: Server | None = None
        self.problems: list[str] | None = None  # tools missing or too old (requires.py); None until checked

    # ---------- children ----------
    def write_child(self, case: cases.Case) -> Child:
        def make():
            argv = [sys.executable, "-m", "workbench.write", "serve", str(case.workdir), "--port", "0"]
            if self.agent:
                argv += ["--agent", self.agent]
            return Child(
                f"write:{case.id}",
                argv,
                env={
                    "WORKBENCH_CASE": case.id,
                    "WORKBENCH_CASE_DIR": str(case.workdir),
                    "WORKBENCH_SHELL_URL": self.url,
                },
                cwd=str(case.workdir),
            )

        return self.sup.ensure(f"write:{case.id}", make)

    def evid_child(self) -> Child:
        # The Datasets tab keeps evid's own Agent pane. evid serves every case, so the pane's
        # terminals cannot know the case sandbox: EVID_TERM_WRAP sends each one through
        # workbench.sandbox_exec, which asks this server for the current case's Write prefix.
        return self.sup.ensure(
            "evid",
            lambda: Child(
                "evid",
                [tool("evid"), *evid_db_args(), "gui", "--headless", "--replace", "-p", str(free_port(8790))],
                env={
                    "EVID_TERM_WRAP": f"{shlex.quote(sys.executable)} -m workbench.sandbox_exec",
                    "WORKBENCH_SHELL_URL": self.url,
                },
            ),
        )

    def sandbox(self) -> dict:
        """The bwrap prefix for a terminal on the current case: the Write child's own policy.

        The Datasets tab's Agent pane runs in evid's process, which serves every case; the Write
        child of the open case knows the case dir, the linked sets, the skills and did's paths.
        """
        with self.lock:
            cid = self.current
        if not cid:
            raise HTTPError(404, "no case is open")
        try:
            case = cases.get(cid)
        except KeyError:
            raise HTTPError(404, "no case is open") from None
        child = self.write_child(case)
        url = child.wait_url(timeout=60)
        if not url:
            raise HTTPError(503, child.error or "the Write tab did not start")
        try:
            with urlopen(Request(url.rstrip("/") + "/api/sandbox"), timeout=60) as r:
                return json.loads(r.read())
        except (OSError, ValueError) as e:
            raise HTTPError(503, f"the Write tab did not answer: {e}") from e

    def did_child(self) -> Child:
        root = cases.did_root()
        created = not root.exists()
        root.mkdir(parents=True, exist_ok=True)
        if created:
            root.chmod(0o700)  # PII lives here; the directory is outside every case

        db = evid_db_args()  # the same data dir as the evid GUI: did's handoff lands there too
        return self.sup.ensure(
            "did",
            lambda: Child(
                "did",
                [tool("did"), "gui", "--headless", "-p", str(free_port(8791))],
                env={"EVID_DB": db[1]} if db else None,
            ),
        )

    def start_apps(self) -> None:
        """evid and did start in the background at launch: did warms its detector, which takes a while."""
        for start in (self.evid_child, self.did_child):
            threading.Thread(target=start, daemon=True).start()

    # ---------- cases ----------
    def select(self, cid: str) -> dict:
        case = cases.get(cid)
        with self.lock:
            self.current = case.id
        self.write_child(case)
        self.connect_workdir(case)
        return self.state()

    def connect_workdir(self, case: cases.Case) -> None:
        """Ask the evid GUI to show this case's ``evid/`` as another trunk.

        Other trunks stay. The same directory again is a no-op on the evid side.
        Nothing happens until evid is up: the first case is selected before evid starts.
        """
        child = self.sup.get("evid")
        if child is None or not child.url:
            return
        path = case.workdir / "evid"
        try:
            path.mkdir(parents=True, exist_ok=True)
            req = Request(
                child.url.rstrip("/") + "/api/roots",
                data=json.dumps({"path": str(path)}).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "X-Evid": "1"},
            )
            with urlopen(req, timeout=3) as resp:
                resp.read()
        except (OSError, ValueError) as exc:
            print(f"workbench: could not connect {path} to evid: {exc}", file=sys.stderr, flush=True)

    def state(self) -> dict:
        with self.lock:
            current, badges = self.current, dict(self.badges)
        all_cases = cases.list_cases()
        current = current if any(c.id == current for c in all_cases) else None
        if current is None and all_cases:
            current = all_cases[0].id
        tabs = {}
        if current:
            w = self.sup.get(f"write:{current}")
            tabs["write"] = w.as_json() if w else None
        for key in ("evid", "did"):
            c = self.sup.get(key)
            tabs[key] = c.as_json() if c else None
        return {
            "version": __version__,
            "cases": [c.as_json() for c in all_cases],
            "current": current,
            "casesRoot": str(cases.cases_root()),
            "didRoot": str(cases.did_root()),
            "tabs": tabs,
            "badges": badges,
            "problems": self.problems,
            "templates": [{"key": t.key, "name": templates.label(t)} for t in templates.TEMPLATES.values()],
        }

    def stop(self) -> None:
        self.sup.stop()

    def request_quit(self) -> None:
        """Unblock ``serve``: close the app window if this process opened one, else stop the HTTP server.

        The main thread is in ``win.wait()`` (app) or ``serve_forever`` (browser / headless). Tauri
        closes its own window when the headless server it started exits, so this process has to end.
        """

        def _quit() -> None:
            time.sleep(0.15)  # the 200 has to leave before the socket closes
            win = self.window
            if win is not None and win.poll() is None:
                win.terminate()  # main returns from wait() and closes the server in ``finally``
                return
            if self.httpd is not None:
                self.httpd.shutdown()

        threading.Thread(target=_quit, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    wb: Workbench
    on_quit = None  # tests record the quit; the real server closes the window and exits
    server_version = "workbench"

    def log_message(self, fmt, *args):  # quiet; errors still reach stderr
        pass

    # ---------- plumbing ----------
    def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data) -> None:
        self.send(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError as e:
            raise HTTPError(400, f"bad JSON: {e}") from e
        if not isinstance(data, dict):
            raise HTTPError(400, "expected a JSON object")
        return data

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method: str) -> None:
        u = urlparse(self.path)
        try:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise HTTPError(403, "bad Host header")
            if u.path.startswith("/api/") and method != "GET" and self.headers.get("X-Workbench") != "1":
                raise HTTPError(403, "missing X-Workbench header")
            self.route(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()})
        except HTTPError as e:
            self.send_json(e.status, {"error": e.msg})
        except KeyError as e:
            self.send_json(404, {"error": f"no such case: {e.args[0]}"})
        except (ValueError, FileExistsError) as e:
            self.send_json(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # surface it, never swallow
            print(f"workbench: {method} {u.path}: {e!r}", file=sys.stderr, flush=True)
            with contextlib.suppress(OSError):
                self.send_json(500, {"error": str(e)})

    # ---------- routes ----------
    def route(self, method: str, path: str, q: dict) -> None:
        wb = self.wb
        if method == "GET" and path in ("/", "/index.html"):
            page = (STATIC / "shell.html").read_text("utf-8")
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/static/"):
            return self.static(STATIC, path[len("/static/") :])
        if method == "GET" and path.startswith("/vendor/"):
            return self.static(VENDOR, path[len("/vendor/") :])
        if method == "GET" and path == "/api/state":
            return self.send_json(200, wb.state())
        if method == "GET" and path == "/api/tab":
            return self.send_json(200, self.tab_url(q.get("tab", ""), q.get("case", "")))
        if method == "GET" and path == "/api/sandbox":
            return self.send_json(200, wb.sandbox())
        if method == "POST" and path == "/api/case/select":
            return self.send_json(200, wb.select(str(self.body().get("id", ""))))
        if method == "POST" and path == "/api/case/new":
            b = self.body()
            case = cases.create(str(b.get("id", "")).strip(), str(b.get("name", "")).strip(), str(b.get("template", "") or "blank"))
            return self.send_json(200, wb.select(case.id))
        if method == "POST" and path == "/api/badge":
            b = self.body()
            with wb.lock:
                wb.badges[str(b.get("tab", ""))] = int(b.get("count") or 0)
            return self.send_json(200, {"ok": True})
        if method == "POST" and path == "/api/quit":
            self.send_json(200, {"ok": True})
            with contextlib.suppress(OSError):
                self.wfile.flush()
            hook = type(self).on_quit
            if hook is not None:
                hook()
                return
            wb.request_quit()
            return
        raise HTTPError(404, f"no route {method} {path}")

    def static(self, base, name: str) -> None:
        parts = name.split("/")
        if not name or any(not s or s.startswith(".") for s in parts):
            raise HTTPError(404, "not found")
        f = base
        for s in parts:
            f = f / s
        if not f.is_file():
            raise HTTPError(404, "not found")
        ext = "." + name.rsplit(".", 1)[-1]
        headers = {"Cache-Control": "max-age=3600"}
        if base == STATIC and (name == "fonts.css" or name.startswith("fonts/")):
            headers["Access-Control-Allow-Origin"] = "*"  # evid and did load the workbench fonts from their own port
        return self.send(200, f.read_bytes(), TYPES.get(ext, "application/octet-stream"), headers)

    def tab_url(self, tab: str, cid: str) -> dict:
        """The URL a tab's iframe should load, waiting (briefly) for its child to come up."""
        wb = self.wb
        if tab == "write":
            child = wb.write_child(cases.get(cid))
        elif tab == "evid":
            child = wb.evid_child()
        elif tab == "did":
            child = wb.did_child()
        else:
            raise HTTPError(400, f"no tab {tab!r}")
        url = child.wait_url(timeout=60)
        if tab == "evid" and url:
            case_id = cid or wb.current
            if case_id:
                with contextlib.suppress(KeyError):
                    wb.connect_workdir(cases.get(case_id))
        return {"tab": tab, "url": url, "error": child.error if not url else ""}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def find_app() -> str | None:
    """The workbench-app window: $WORKBENCH_APP, else on PATH, else the newest build in app/target."""
    env = os.environ.get("WORKBENCH_APP")
    if env:
        return env if os.access(env, os.X_OK) else None
    hit = shutil.which("workbench-app")
    if hit:
        return hit
    target = Path(__file__).resolve().parents[2] / "app" / "target"
    builds = [
        b
        for b in (target / "release" / "workbench-app", target / "debug" / "workbench-app")
        if b.is_file() and os.access(b, os.X_OK)
    ]
    return str(max(builds, key=lambda b: b.stat().st_mtime)) if builds else None


def serve(port: int = 8780, browser: bool = False, headless: bool = False, agent: str = "") -> None:
    """Serve the workbench on 127.0.0.1 and show it in workbench-app (else the browser)."""
    wb = Workbench(agent or os.environ.get("WORKBENCH_AGENT", ""))
    Handler.wb = wb
    srv = Server(("127.0.0.1", free_port(port)), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/"
    wb.url = url
    wb.httpd = srv
    print(f"workbench  {cases.cases_root()}\n  open {url}", flush=True)

    def check_tools():
        wb.problems = requires.problems()
        for line in wb.problems:
            print(f"  warning: {line} (jura doctor)", flush=True)

    threading.Thread(target=check_tools, daemon=True).start()
    existing = cases.list_cases()
    if existing:
        wb.select(existing[0].id)
    wb.start_apps()
    exe = None if headless or browser else find_app()
    try:
        if exe:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            win = subprocess.Popen([exe, "--url", url])
            wb.window = win
            try:
                win.wait()
            except KeyboardInterrupt:
                win.terminate()
            return
        if not headless and not browser:
            print("  (workbench-app not built — `make app-build` or set WORKBENCH_APP; using the browser)")
        print("  (Ctrl+C to stop)", flush=True)
        if not headless:
            threading.Timer(0.4, webbrowser.open, [url]).start()
        with contextlib.suppress(KeyboardInterrupt):
            srv.serve_forever()
    finally:
        wb.stop()
        srv.server_close()
        time.sleep(0.1)
