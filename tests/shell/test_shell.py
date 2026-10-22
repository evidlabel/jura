"""The workbench shell: cases, the child supervisor and the shell server."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from workbench import casefile, cases
from workbench.supervisor import Child, Supervisor


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CASES", str(tmp_path / "cases"))
    monkeypatch.setenv("WORKBENCH_DID", str(tmp_path / "did"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("WORKBENCH_EVID_BIN", "/nonexistent/evid")  # never touch the real evid store
    monkeypatch.setenv("WORKBENCH_DID_BIN", "/nonexistent/did")
    return tmp_path


# ---------- cases ----------
def test_create_case(roots):
    c = cases.create("2026-014", "Tenancy dispute")
    assert c.workdir == (roots / "cases" / "2026-014").resolve()
    assert c.evid_set == "" and c.did_project is None
    for name in ("workbench.yml", "AGENTS.md", "main.typ", ".gitignore", "docs/.gitkeep", "refs/extra/.gitkeep"):
        assert (c.workdir / name).is_file()
    assert (c.workdir / "out").is_dir() and not (c.workdir / "steering.typ").exists()
    # evid's documents stay out of git; their labels, metadata and text do not
    ignore = (c.workdir / ".gitignore").read_text()
    assert cases.EVID_PDF_IGNORE in ignore and "vecdb/" in ignore
    for name in ("case.toml", "sources.yml", "skills.yml", "evid.yml"):
        assert not (c.workdir / name).exists()
    assert not (roots / "did" / "2026-014").exists()
    text = (c.workdir / "workbench.yml").read_text()
    assert text.startswith("# workbench case") and "did: []" in text and "    - did\n" in text
    for path in ("~/.claude/skills", "~/.grok/skills", "~/.hermes/skills", "~/.codex/skills"):
        assert f'    - "{path}"\n' in text
    agents = (c.workdir / "AGENTS.md").read_text()
    assert "jura write fb ls" in agents and "jura write fb reply" in agents
    assert "treedit" not in agents
    main = (c.workdir / "main.typ").read_text()
    assert 'case-name = "Tenancy dispute"' in main
    assert "__ID__" not in main and "#import" not in main and "evid-set" not in main
    assert [a["name"] for a in casefile.read_config(c.workdir).agents] == ["claude", "codex", "grok", "opencode"]
    tracked = subprocess.run(["git", "-C", str(c.workdir), "ls-files"], capture_output=True, text=True).stdout.split()
    assert sorted(tracked) == [".gitignore", "AGENTS.md", "docs/.gitkeep", "main.typ", "refs/extra/.gitkeep", "workbench.yml"]
    log = subprocess.run(["git", "-C", str(c.workdir), "log", "--oneline"], capture_output=True, text=True)
    assert "workbench: new case 2026-014" in log.stdout
    assert [x.id for x in cases.list_cases()] == ["2026-014"]
    assert cases.get("2026-014").name == "Tenancy dispute"


def test_legacy_case_toml_still_lists(roots):
    d = roots / "cases" / "old"
    d.mkdir(parents=True)
    (d / "case.toml").write_text(
        'id = "old"\nname = "Old"\nevid_set = "old-anon"\ndid_project = "/tmp/did/old"\n'
    )
    (d / "sources.yml").write_text("evid:\n  global:\n    - laws\n  local: []\ndid: []\n")
    c = cases.get("old")
    assert c.name == "Old" and c.evid_set == "old-anon"
    assert c.did_project is not None and c.did_project.as_posix() == "/tmp/did/old"


def test_create_case_rejects(roots, monkeypatch):
    with pytest.raises(ValueError, match="bad case id"):
        cases.create("../x", "x")
    cases.create("a", "a")
    with pytest.raises(FileExistsError):
        cases.create("a", "a")
    monkeypatch.setenv("WORKBENCH_DID", str(roots / "cases" / "did"))
    with pytest.raises(ValueError, match="must not be under the cases root"):
        cases.create("b", "b")


def test_slug_folds_a_typed_id_to_ascii():
    assert cases.slug("manosfæren") == "manosfaeren"
    assert cases.slug("Ørsted-sagen") == "Oersted-sagen"
    assert cases.slug("2026-014 Tenancy") == "2026-014-Tenancy"
    assert cases.slug("Café") == "Cafe"
    assert cases.slug(" ../x ") == "x" and cases.slug("???") == ""
    with pytest.raises(ValueError, match="'manosfaeren'"):
        cases.create("manosfæren", "x")  # the error points at the id the UI would have sent


# ---------- supervisor ----------
FAKE = [sys.executable, "-c", "import time; print('x\\n  open http://127.0.0.1:1234/', flush=True); time.sleep(60)"]


def test_child_reads_url_and_stops():
    c = Child("fake", FAKE)
    c.start()
    assert c.wait_url(10) == "http://127.0.0.1:1234/"
    assert c.as_json()["alive"]
    c.stop()
    assert not c.as_json()["alive"]


def test_child_restarts_after_crash():
    crash = [sys.executable, "-c", "print('WRITE_URL http://127.0.0.1:9/', flush=True); raise SystemExit(3)"]
    c = Child("crash", crash)
    c.start()
    assert c.wait_url(10) == "http://127.0.0.1:9/"
    deadline = time.time() + 10
    while c.restarts == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert c.restarts >= 1 and "exited with code 3" in c.error
    c.stop()


def test_child_started_from_a_short_thread_survives_it():
    """PR_SET_PDEATHSIG is per forking thread: children must not die with a request thread."""
    sup = Supervisor()
    t = threading.Thread(target=lambda: sup.ensure("fake", lambda: Child("fake", FAKE)))
    t.start()
    t.join()
    child = sup.get("fake")
    assert child.wait_url(10)
    time.sleep(0.5)
    assert child.proc.poll() is None
    sup.stop()


def test_child_reuses_a_raised_instance():
    raised = [sys.executable, "-c", "print('Raised the running evid GUI (http://127.0.0.1:8790/).', flush=True)"]
    c = Child("evid", raised)
    c.start()
    assert c.wait_url(10) == "http://127.0.0.1:8790/" and c.external
    c.proc.wait(5)
    time.sleep(0.3)
    assert c.restarts == 0 and c.url == "http://127.0.0.1:8790/"


def test_unknown_binary_reports_error():
    c = Child("nope", ["/nonexistent/evid", "gui"])
    c.start()
    assert c.wait_url(5) is None and "cannot start" in c.error


# ---------- server ----------
@pytest.fixture
def shell(roots, monkeypatch):
    from workbench import server

    monkeypatch.setenv("WORKBENCH_EVID_BIN", "/nonexistent/evid")
    monkeypatch.setenv("WORKBENCH_DID_BIN", "/nonexistent/did")
    wb = server.Workbench()
    server.Handler.wb = wb
    server.Handler.on_quit = None
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    server.Handler.on_quit = None
    srv.shutdown()
    wb.stop()


def call(base, method, path, body=None, headers=None):
    h = {"X-Workbench": "1", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = Request(base + path, data=data, method=method, headers=h)
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()


def test_shell_page_and_static(shell):
    status, page = call(shell, "GET", "/")
    assert status == 200 and b"jura" in page and b"__TOKEN__" not in page
    assert b"Datasets" in page and b"Anonymization" in page
    assert b'data-tab="sources"' not in page and b"/api/quit" in page and b"workbench-quit" in page
    assert b'data-pane="preview"' not in page and b'data-pane="skills"' in page and b'data-pane="queue"' in page
    assert b'id="agents"' in page and b"workbench-panes" in page and b"workbench-agents" in page and b"workbench-agent" in page
    # a long history: the picker filters, and the id field says what it will fold
    assert b'id="casefind"' in page and b'id="casecount"' in page and b'id="nc-idhint"' in page and b'id="st-idhint"' in page
    # Focus lives in the Write editor bar now, not the shell
    assert b'id="b-focus"' not in page and b"workbench-focus" not in page
    # did hides its own agent pane; evid keeps its Agent pane, sandboxed through this window
    assert b"if (tab === 'did') u.searchParams.set('embed', '1')" in page
    assert b"['write', 'evid']" in page
    assert call(shell, "GET", "/static/fonts/jetbrains-mono-latin-400-normal.woff2")[0] == 200
    assert call(shell, "GET", "/static/fonts/source-sans-3-latin-300-normal.woff2")[0] == 200
    assert b'<option value="evening">' in page and b'<option value="legal">' in page
    assert call(shell, "GET", "/vendor/xterm.js")[0] == 200
    assert call(shell, "GET", "/static/../server.py")[0] == 404
    assert call(shell, "GET", "/static/fonts/.hidden")[0] == 404


def test_theme_reaches_evid_and_did(shell):
    """evid and did get the whole theme by message, and load the fonts from the shell's port."""
    page = call(shell, "GET", "/")[1]
    assert b"tokens:" in page and b"fonts:" in page and b"/static/fonts.css" in page
    assert b"<link rel=\"stylesheet\" href=\"/static/fonts.css\">" in page
    for path in ("/static/fonts.css", "/static/fonts/source-serif-4-latin-400-normal.woff2"):
        with urlopen(Request(shell + path), timeout=30) as r:
            assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == "*"
    css = call(shell, "GET", "/static/fonts.css")[1]
    assert b"url(fonts/source-serif-4-latin-400-normal.woff2)" in css and b"/static/" not in css
    with urlopen(Request(shell + "/vendor/xterm.js"), timeout=30) as r:
        assert r.headers["Access-Control-Allow-Origin"] is None  # only the fonts are shared


def test_guards(shell):
    assert call(shell, "POST", "/api/case/new", {"id": "x"}, {"X-Workbench": ""})[0] == 403
    assert call(shell, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403


def test_new_case_starts_write(shell):
    status, body = call(shell, "POST", "/api/case/new", {"id": "2026-014", "name": "Tenancy dispute"})
    state = json.loads(body)
    assert status == 200 and state["current"] == "2026-014"
    assert state["cases"][0]["evidSet"] == ""
    assert state["didRoot"].endswith("/did")
    status, body = call(shell, "GET", "/api/tab?tab=write&case=2026-014")
    tab = json.loads(body)
    assert tab["url"].startswith("http://127.0.0.1:")
    with urlopen(tab["url"] + "api/tree", timeout=10) as r:
        assert json.loads(r.read())["root"] == "2026-014"
    assert call(shell, "POST", "/api/case/new", {"id": "2026-014"})[0] == 400
    assert call(shell, "POST", "/api/case/select", {"id": "nope"})[0] == 404


def test_sandbox_prefix_is_the_open_cases_write_child(shell, roots):
    call(shell, "POST", "/api/case/new", {"id": "2026-014", "name": "Tenancy dispute"})
    status, body = call(shell, "GET", "/api/sandbox")
    out = json.loads(body)
    assert status == 200 and set(out) == {"argv", "sandboxed"}
    assert out["sandboxed"] == bool(out["argv"])
    if os.environ.get("WORKBENCH_SANDBOX", "1") != "0" and shutil.which("bwrap"):
        case_dir = (roots / "cases" / "2026-014").resolve()
        assert out["argv"][0] == "bwrap" and out["argv"][-1] == "--"
        assert str(case_dir) in out["argv"]  # bound read-write
        assert "--chdir" not in out["argv"]  # the caller's own folder stays (evid starts in the set)
    else:
        assert out["argv"] == []


def test_sandbox_prefix_needs_a_case(shell):
    status, body = call(shell, "GET", "/api/sandbox")
    assert status == 404 and "no case" in json.loads(body)["error"]


def test_sandbox_exec_runs_the_command_under_the_case_sandbox(shell, roots):
    if os.environ.get("WORKBENCH_SANDBOX", "1") == "0" or not shutil.which("bwrap"):
        pytest.skip("the sandbox is off here")
    call(shell, "POST", "/api/case/new", {"id": "2026-014", "name": "Tenancy dispute"})
    case_dir = (roots / "cases" / "2026-014").resolve()
    r = subprocess.run(
        [sys.executable, "-m", "workbench.sandbox_exec", "/bin/sh", "-c", "echo sandbox-ok; pwd"],
        cwd=str(case_dir),
        env={**os.environ, "WORKBENCH_SHELL_URL": shell},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "sandbox-ok" in r.stdout
    assert r.stdout.strip().splitlines()[-1] == str(case_dir)  # its own folder, as evid's set is


def test_new_case_opens_the_folded_id(shell, roots):
    """The picker folds a typed id before it posts ("manosfæren" → "manosfaeren"): the case that
    opens is the one in the list, in the workdir, and the display name keeps the æ."""
    status, body = call(shell, "POST", "/api/case/new", {"id": "manosfaeren", "name": "Manosfæren"})
    state = json.loads(body)
    assert status == 200 and state["current"] == "manosfaeren"
    assert [(c["id"], c["name"]) for c in state["cases"]] == [("manosfaeren", "Manosfæren")]
    assert Path(state["cases"][0]["workdir"]) == (roots / "cases" / "manosfaeren").resolve()
    # it is open: its Write tab serves that workdir, and a window that starts again lands on it
    tab = json.loads(call(shell, "GET", "/api/tab?tab=write&case=manosfaeren")[1])
    assert tab["url"].startswith("http://127.0.0.1:")
    with urlopen(tab["url"] + "api/tree", timeout=10) as r:
        assert json.loads(r.read())["root"] == "manosfaeren"
    assert json.loads(call(shell, "GET", "/api/state")[1])["current"] == "manosfaeren"
    # an un-folded id (an agent or a script) is refused with the id the window would have sent
    status, body = call(shell, "POST", "/api/case/new", {"id": "manosfæren"})
    assert status == 400 and "manosfaeren" in json.loads(body)["error"]


def test_missing_app_reports(shell):
    call(shell, "POST", "/api/case/new", {"id": "c1"})
    _, body = call(shell, "GET", "/api/tab?tab=evid&case=c1")
    tab = json.loads(body)
    assert tab["url"] is None and "cannot start" in tab["error"]


def test_evid_launch_replaces_a_running_gui(monkeypatch):
    from workbench import server

    monkeypatch.setattr(server, "tool", lambda _name: "/bin/evid")
    monkeypatch.setattr(server, "free_port", lambda _start: 8790)
    captured = {}

    class Fake(server.Child):
        def start(self):
            captured["argv"] = self.argv
            captured["env"] = self.env

    monkeypatch.setattr(server, "Child", Fake)
    wb = server.Workbench()
    wb.url = "http://127.0.0.1:8780/"
    wb.evid_child()
    assert captured["argv"] == ["/bin/evid", "gui", "--headless", "--replace", "-p", "8790"]
    # the Datasets tab's Agent pane asks this window for the open case's sandbox
    assert captured["env"]["EVID_TERM_WRAP"].endswith("-m workbench.sandbox_exec")
    assert captured["env"]["WORKBENCH_SHELL_URL"] == "http://127.0.0.1:8780/"


def test_evid_tab_without_a_server_leaves_the_workdir_alone(shell, roots):
    call(shell, "POST", "/api/case/new", {"id": "c1"})
    _, body = call(shell, "GET", "/api/tab?tab=evid&case=c1")
    tab = json.loads(body)
    assert tab["url"] is None
    assert not (roots / "cases" / "c1" / "evid").exists()


def test_connect_workdir_posts_the_case_evid_dir(roots):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from workbench.server import Workbench

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["body"] = self.rfile.read(n)
            seen["x"] = self.headers.get("X-Evid")
            raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wb = Workbench()
    try:
        child = Child("evid", ["true"])
        child.url = f"http://127.0.0.1:{srv.server_port}/"
        wb.sup.children["evid"] = child
        case = cases.create("c1", "C")
        wb.connect_workdir(case)
        assert seen["path"] == "/api/roots"
        assert json.loads(seen["body"])["path"] == str(case.workdir / "evid")
        assert seen["x"] == "1"
        assert (case.workdir / "evid").is_dir()
    finally:
        wb.stop()
        srv.shutdown()
        srv.server_close()


def test_select_and_the_evid_tab_ask_to_connect(roots, monkeypatch):
    from workbench.server import Handler, Workbench

    wb = Workbench()
    seen = []
    monkeypatch.setattr(wb, "connect_workdir", lambda case: seen.append(case.id))
    case = cases.create("c1", "C")
    wb.select(case.id)
    assert seen == ["c1"]

    class Up:
        url = "http://127.0.0.1:9/"
        error = ""

        def wait_url(self, timeout=60):
            return self.url

    monkeypatch.setattr(wb, "evid_child", lambda: Up())
    handler = Handler.__new__(Handler)
    handler.wb = wb
    assert Handler.tab_url(handler, "evid", case.id)["url"] == Up.url
    assert seen == ["c1", "c1"]

    class Down:
        url = None
        error = "cannot start"

        def wait_url(self, timeout=60):
            return None

    monkeypatch.setattr(wb, "evid_child", lambda: Down())
    assert Handler.tab_url(handler, "evid", case.id)["url"] is None
    assert seen == ["c1", "c1"]
    wb.stop()


def test_quit_route(shell):
    from workbench import server

    seen = []
    server.Handler.on_quit = lambda: seen.append(True)
    status, body = call(shell, "POST", "/api/quit")
    assert status == 200 and json.loads(body) == {"ok": True} and seen == [True]
    assert call(shell, "GET", "/api/sources/terms?case=c1")[0] == 404
    server.Handler.on_quit = None


def test_request_quit_closes_the_window_or_the_server():
    from workbench.server import Workbench

    class Httpd:
        def __init__(self):
            self.n = 0

        def shutdown(self):
            self.n += 1

    class Win:
        def __init__(self):
            self.done = False

        def poll(self):
            return None

        def terminate(self):
            self.done = True

    window = Workbench()
    window.window = Win()
    window.httpd = Httpd()
    window.request_quit()
    time.sleep(0.4)
    assert window.window.done and window.httpd.n == 0

    headless = Workbench()
    headless.httpd = Httpd()
    headless.request_quit()
    time.sleep(0.4)
    assert headless.httpd.n == 1


def test_cli_case_commands(roots, capsys):
    from workbench.cli import main

    main(["case", "new", "2026-001", "First"])
    assert "2026-001" in capsys.readouterr().out
    main(["case", "ls", "-f", "json"])
    assert json.loads(capsys.readouterr().out)[0]["name"] == "First"
    assert not os.path.isdir(roots / "did" / "2026-001")


def test_create_does_not_call_evid(roots, monkeypatch):
    fake = roots / "evid"
    log = roots / "evid.log"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("WORKBENCH_EVID_BIN", str(fake))
    cases.create("2026-020", "Data access")
    assert not log.exists()
    assert not (roots / "did" / "2026-020").exists()


def test_free_port_reuses_a_port_in_time_wait():
    """A workbench restarted right after quitting gets its port back, not the next one."""
    import socket

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # the server closes first: its side of the port goes to TIME_WAIT
    cli.close()
    srv.close()
    from workbench import server

    assert server.free_port(port) == port
