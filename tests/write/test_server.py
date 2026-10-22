# Derived from wr1/treedit at 94e6806 (MIT, Copyright (c) 2026 wr1); see src/workbench/write/LICENSE-treedit.txt.
import base64
import json
import os
import socket
import threading
import time
from urllib.error import HTTPError as URLHTTPError
from urllib.request import Request, urlopen

import pytest

from workbench.write.core import Handler, Server, _ws, main
from workbench.write.term import Client, Sessions, Terminal, ws_frame


@pytest.fixture
def server(tree, monkeypatch):
    monkeypatch.setattr(Handler, "ws", _ws(str(tree), "", [], False), raising=False)
    monkeypatch.setattr(Handler, "terms", None)
    monkeypatch.setattr(Handler, "window", None)
    monkeypatch.setattr(Handler, "context", {})
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def call(base, method, path, body=None, headers=None):
    h = {"X-Treedit": "1", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = Request(base + path, data=data, method=method, headers=h)
    try:
        with urlopen(req, timeout=5) as r:
            raw, ctype, status = r.read(), r.headers["Content-Type"], r.status
    except URLHTTPError as e:
        raw, ctype, status = e.read(), e.headers["Content-Type"], e.code
    return status, (json.loads(raw) if ctype.startswith("application/json") else raw.decode())


def test_page_and_vendor(server):
    status, page = call(server, "GET", "/")
    assert status == 200 and '"token"' in page and "/*TREEDIT_CONFIG*/" not in page
    assert 'id="agentbar"' not in page and 'id="agents"' in page and 'id="tagents"' not in page and 'id="t-add"' not in page
    assert "workbench-agents" in page and "workbench-agent" in page
    assert 'html[data-theme="evening"]' in page and 'html[data-theme="legal"]' in page
    assert 'id="t-collapse"' in page and 'id="termrail"' in page  # collapse strip + the right-edge rail back
    assert 'id="lc-collapse"' in page  # the tree collapses to a strip on the left
    assert 'id="b-focus"' in page and 'id="focusveil"' in page
    assert "if (CFG.term && !T.open) openTerm(true)" not in page
    assert call(server, "GET", "/vendor/xterm.js")[0] == 200
    assert call(server, "GET", "/logo.svg")[1].startswith("<svg")
    assert call(server, "GET", "/vendor/nope.js")[0] == 404


def test_raw_plot(server, tree):
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0P\x0f\x00\x04\x85\x01\x80\x84\xa9\x8c!\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (tree / "fig.png").write_bytes(png)
    (tree / "fig.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    with urlopen(server + "/api/raw?path=fig.png", timeout=5) as r:
        assert r.status == 200 and r.headers["Content-Type"] == "image/png" and r.read() == png
        assert "sandbox" in r.headers["Content-Security-Policy"]
    status, svg = call(server, "GET", "/api/file?path=" + "fig.svg")
    assert status == 200 and svg["kind"] == "plot" and svg["content"].startswith("<svg")
    assert call(server, "GET", "/api/raw?path=README.md")[0] == 415
    assert call(server, "GET", "/api/raw?path=../x")[0] == 400


def test_tree_and_files(server, tree):
    status, t = call(server, "GET", "/api/tree")
    assert status == 200 and t["root"] == "proj" and t["feedback"] == []
    assert call(server, "GET", "/api/version")[1]["version"] == t["version"]
    f = call(server, "GET", "/api/file?path=pkg/b.py")[1]
    status, out = call(server, "PUT", "/api/file", {"path": "pkg/b.py", "content": "x = 2\n", "base_hash": f["hash"]})
    assert status == 200 and (tree / "pkg" / "b.py").read_text() == "x = 2\n"
    status, out = call(server, "PUT", "/api/file", {"path": "pkg/b.py", "content": "x", "base_hash": f["hash"]})
    assert status == 409 and out["disk"]["content"] == "x = 2\n"
    assert call(server, "GET", "/api/file?path=nope")[0] == 404
    assert call(server, "GET", "/api/file?path=../x")[0] == 400
    assert call(server, "GET", "/api/changes")[1]["root"] == str(tree)


def test_history_endpoint(server, tree):
    assert call(server, "GET", "/api/tree")[0] == 200
    p = tree / "pkg" / "b.py"
    p.write_text("x = 1\ny = 2\n")
    os.utime(p, None)
    assert call(server, "GET", "/api/tree")[0] == 200
    status, body = call(server, "GET", "/api/history")
    assert status == 200
    ev = [e for e in body["events"] if e["path"] == "pkg/b.py" and e["ranges"]]
    assert ev[-1]["ranges"] == [[2, 2, "add"]] and ev[-1]["by"] == "agent"


def test_mutations(server, tree):
    assert call(server, "POST", "/api/new", {"path": "d/n.txt"})[1] == {"ok": True, "git": False}
    assert call(server, "POST", "/api/new", {"path": "d2", "kind": "dir"})[0] == 200
    assert call(server, "PUT", "/api/note", {"path": "d", "note": "folder d"})[0] == 200
    assert call(server, "POST", "/api/note-move", {"from": "d", "to": "e"})[0] == 200
    assert call(server, "POST", "/api/rename", {"from": "d", "to": "e"})[0] == 200
    assert (tree / "e" / "n.txt").exists()
    status, fb = call(server, "POST", "/api/feedback", {"paths": ["e"], "text": "why?"})
    assert status == 200 and fb["id"] == 1
    assert call(server, "PUT", "/api/feedback", {"id": 1, "reply": "because", "status": "done"})[1]["status"] == "done"
    assert call(server, "POST", "/api/feedback-delete", {"id": 1})[0] == 200
    assert call(server, "PUT", "/api/draft", {"path": "e/n.txt", "base_hash": "x", "diff": "+y"})[0] == 200
    assert "e/n.txt" in call(server, "GET", "/api/tree")[1]["drafts"]
    assert call(server, "POST", "/api/draft-drop", {"path": "e/n.txt"})[0] == 200
    assert call(server, "POST", "/api/delete", {"path": "e"})[0] == 200
    assert not (tree / "e").exists()
    assert call(server, "POST", "/api/log", {"msg": "page error"})[0] == 200
    assert call(server, "POST", "/api/nope", {})[0] == 404


def test_guards(server):
    req = Request(server + "/api/note", data=b"{}", method="PUT")
    with pytest.raises(URLHTTPError) as e:
        urlopen(req, timeout=5)
    assert e.value.code == 403
    assert call(server, "GET", "/api/tree", headers={"Host": "evil.example"})[0] == 403
    req = Request(server + "/api/note", data=b"{bad", method="PUT", headers={"X-Treedit": "1"})
    with pytest.raises(URLHTTPError) as e:
        urlopen(req, timeout=5)
    assert e.value.code == 400
    assert call(server, "GET", "/api/term")[0] == 404


def test_grep_endpoint(server):
    from urllib.parse import quote

    status, out = call(server, "GET", "/api/grep?q=" + quote("return 1"))
    assert status == 200 and out["hits"] == [{"path": "pkg/a.py", "line": 2, "text": "return 1"}]
    assert out["truncated"] is False
    assert call(server, "GET", "/api/grep?q=")[0] == 400
    assert call(server, "GET", "/api/grep?q=" + quote("RETURN 1") + "&case=1")[1]["hits"] == []


def test_git_endpoints(server, repo):
    status, log = call(server, "GET", "/api/git/log?whole=1")
    assert status == 200 and log["commits"][0]["subject"] == "init"
    status, diff = call(server, "GET", f"/api/git/show?whole=1&commit={log['commits'][0]['short']}")
    assert status == 200 and "init" in diff


def test_context_roundtrip(server, capsys):
    call(
        server,
        "POST",
        "/api/context",
        {"paths": ["pkg/a.py", "README.md"], "lines": [1, 2], "quote": "def a():\n    return 1", "focus": "pkg/a.py"},
    )
    main(["context", "--url", server])
    assert capsys.readouterr().out == "pkg/a.py L1-2\nREADME.md\n    | def a():\n    |     return 1\n"
    call(server, "POST", "/api/context", {})
    main(["context", "--url", server])
    assert capsys.readouterr().out == "nothing selected\n"


def test_edits_with_window(server, tree, capsys):
    ws = Handler.ws
    ws.scan()
    ws.write_file("README.md", "new\n", ws.read_file("README.md")["hash"], False)
    p = tree / "README.md"
    os.utime(p, (time.time() + 5, time.time() + 5))
    ws.scan()
    main(["edits", "-C", str(tree), "--url", server])
    assert "saved in the editor (1):\n  README.md  (just now)" in capsys.readouterr().out
    main(["edits", "-C", str(tree / "pkg"), "--url", server])
    assert "the window at" in capsys.readouterr().out


def test_terminal(server, monkeypatch, tree):
    terms = Sessions(str(tree), {"SHELL": "/bin/sh", "PATH": os.environ.get("PATH", ""), "PS1": "$ "}, "echo hi-there")
    term = terms.get("1")
    monkeypatch.setattr(Handler, "terms", terms)
    host, port = server.removeprefix("http://").split(":")
    token = Handler.token

    def handshake(origin, tok=token):
        s = socket.create_connection((host, int(port)), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(
            (
                f"GET /api/term?token={tok} HTTP/1.1\r\nHost: {host}:{port}\r\nOrigin: {origin}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n\r\n"
            ).encode()
        )
        return s

    s = handshake("http://evil.example")
    assert b"403" in s.recv(1024)
    s.close()
    s = handshake(f"http://{host}:{port}", tok="wrong")
    assert b"403" in s.recv(1024)
    s.close()
    s = handshake(f"http://{host}:{port}")
    data = b""
    deadline = time.time() + 10
    while b"hi-there" not in data.split(b"echo hi-there")[-1] and time.time() < deadline:
        data += s.recv(65536)
    assert b"101 Switching Protocols" in data and b'"hello"' in data
    assert b"hi-there" in data.split(b"echo hi-there")[-1]

    def send(op, payload):
        mask = b"\0\0\0\0"
        frame = ws_frame(op, payload)
        s.sendall(
            frame[:1] + bytes([frame[1] | 0x80]) + frame[2 : 2 + (len(frame) - 2 - len(payload))] + mask + payload
        )

    send(1, json.dumps({"t": "resize", "rows": 30, "cols": 100}).encode())
    send(1, b"not json")
    send(1, json.dumps({"t": "in", "d": "echo again\r"}).encode())
    send(9, b"ping")
    deadline, data = time.time() + 10, b""
    while (b"\x8a\x04ping" not in data or b"again" not in data) and time.time() < deadline:
        data += s.recv(65536)
    assert b"\x8a\x04ping" in data and b"again" in data
    assert term.size == (30, 100)
    send(8, b"")
    s.close()
    term.stop()
    assert not term.alive


def test_a_stalled_page_does_not_stall_the_terminal(tmp_path, monkeypatch):
    """The pty must never wait for a page. A webview that stops reading (busy, reloading) would
    otherwise fill its socket and block the pty reader, and the agent's own writes with it."""
    from workbench.write import term as term_mod

    monkeypatch.setattr(term_mod, "QUEUE_MAX", 128 * 1024)
    slow_srv, slow_cli = socket.socketpair()
    good_srv, good_cli = socket.socketpair()
    slow_cli.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # this page never reads
    got = bytearray()
    lock = threading.Lock()

    def drain():
        while (chunk := good_cli.recv(65536)):
            with lock:
                got.extend(chunk)

    threading.Thread(target=drain, daemon=True).start()
    t = Terminal(str(tmp_path), {"SHELL": "/bin/sh", "PATH": os.environ.get("PATH", "")})
    t.start()
    slow, good = Client(slow_srv), Client(good_srv)
    t.clients.update({slow, good})
    try:
        started = time.monotonic()
        for _ in range(12):  # 768 KB of agent output, six times what the stalled page may queue
            t._broadcast(b"x" * 65536)
            time.sleep(0.01)
        assert time.monotonic() - started < 2.0, "the pty waited for the stalled page"
        deadline = time.time() + 10
        while len(got) < 512 * 1024 and time.time() < deadline:
            time.sleep(0.02)
        assert len(got) >= 512 * 1024, f"the reading page got {len(got)} bytes of the burst"
        assert not slow.send(1, b"still there"), "the stalled page was queued forever, not dropped"
    finally:
        t.stop()
        slow_cli.close()
        good_cli.close()


def test_terminal_sessions(server, monkeypatch, tree):
    terms = Sessions(str(tree), {"SHELL": "/bin/sh", "PATH": os.environ.get("PATH", ""), "PS1": "$ "}, None)
    monkeypatch.setattr(Handler, "terms", terms)
    assert call(server, "GET", "/api/terms")[1]["terms"] == []
    status, out = call(server, "POST", "/api/term/new", {"agent": "echo second-pane"})
    assert status == 200 and out["id"] == "1" and [t["label"] for t in out["terms"]] == ["echo second-pane"]
    host, port = server.removeprefix("http://").split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(
        (
            f"GET /api/term?token={Handler.token}&id=1 HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Origin: http://{host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        ).encode()
    )
    data, deadline = b"", time.time() + 10
    while b"second-pane" not in data.split(b"echo second-pane")[-1] and time.time() < deadline:
        data += s.recv(65536)
    assert b'"label": "echo second-pane"' in data and terms.get("1").alive
    assert [t["id"] for t in call(server, "POST", "/api/term/close", {"id": "1"})[1]["terms"]] == []
    # From the agent menu the pane is titled with the row's name, not its command (which may
    # carry an env assignment with a home path).
    out = call(server, "POST", "/api/term/new", {"agent": "OPENCODE_CONFIG=/x/p.json opencode", "name": "OpenCode · local"})[1]
    assert [t["label"] for t in out["terms"]] == ["OpenCode · local"]
    call(server, "POST", "/api/term/close", {"id": out["id"]})
    data, deadline = b"", time.time() + 5
    while b'"closed"' not in data and time.time() < deadline:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    assert b'"closed"' in data
    s.close()
    assert call(server, "POST", "/api/term/close", {"id": "2"})[0] == 404
    assert call(server, "GET", "/api/term?id=9&token=" + Handler.token)[0] == 404
    terms.stop()


FAKE_SSH = r'''#!PYTHON
"""Stands in for ssh: `-N -L l:lport:h:rport` relays a local port, else runs the last argument locally."""
import socket, subprocess, sys, threading
args = sys.argv[1:]
if "-N" in args:
    _, lport, _, rport = args[args.index("-L") + 1].split(":")
    srv = socket.create_server(("127.0.0.1", int(lport)))
    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            b.close()
    while True:
        c, _ = srv.accept()
        r = socket.create_connection(("127.0.0.1", int(rport)))
        threading.Thread(target=pipe, args=(c, r), daemon=True).start()
        threading.Thread(target=pipe, args=(r, c), daemon=True).start()
sys.exit(subprocess.call(["sh", "-c", args[-1]]))
'''



def test_serve_contract(tree):
    """The workbench supervisor starts `serve`, reads `WRITE_URL <url>` and stops it with SIGTERM."""
    import signal
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-m", "workbench.write", "serve", str(tree), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("WRITE_URL http://127.0.0.1:"), line
        url = line.split(" ", 1)[1]
        with urlopen(url + "api/tree", timeout=5) as r:
            assert json.loads(r.read())["root"] == "proj"
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0


def test_restart_starts_a_program_that_ignores_hangup(tmp_path):
    """An agent that outlives SIGHUP is killed, and the restart starts a new session."""
    from workbench.write.term import Terminal

    t = Terminal(str(tmp_path), {"SHELL": "/bin/sh", "PATH": os.environ.get("PATH", ""), "PS1": "$ "},
                 "trap '' HUP; exec sleep 60")
    t.start()
    try:
        time.sleep(0.5)
        first = t.proc.pid
        t.restart()
        assert t.alive and t.proc.pid != first
    finally:
        t.stop()
