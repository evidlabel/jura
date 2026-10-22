"""What the workbench adds to the Write tab's server, behind one hook in the forked handler.

Routes (all under the handler's guards: loopback Host, X-Treedit on writes):

    GET  /api/wb                  case, sandbox, preview and the harness buttons
    GET  /api/sources             evid pills and did pills (workbench.yml)
    PUT  /api/sources             {pool, slug, on} or {slug, mode} — link a pill, or set a pull mode
    POST /api/sources/pull        {slug?} — hand off one connected did set, or every connected one, as PDFs
    POST /api/sources/gather      write one merged refs/refs.yml for the linked sets
    POST /api/sources/quote       {slug, uuid, where} — evid doc quote on one document
    GET  /api/sources/docs        ?slug=&where= — documents in one linked set
    POST /api/sources/repair      {slug} — re-anchor a linked global anon set (evid's repair job)
    GET  /api/skills              the configured skill directories, as a tree of skills
    PUT  /api/skills              {name, on} or {names, on} or {clear: true}
    GET  /api/skills/file         ?name=&path= — one skill file, read-only
    GET  /api/preview             ?path= — start / reuse tinymist for a .typ file (?restart=1 replaces it)
    GET  /api/preview/errors      ?path= — the error blocks tinymist printed for its latest failed compile
    GET  /api/git/state           auto-git: last commit, pending, state tags
    GET  /api/git/recent          the last commits, with their state tags
    POST /api/git/tag             {label, message} — tag HEAD
    POST /api/git/commit          commit what is pending now
    POST /api/compile             {path} — typst compile a .typ to out/<path>.pdf
    GET  /api/queue               what the last roll sent
    POST /api/queue/rolled        {target, notes, feedback, items}
    GET  /static/fonts/<file>     the workbench fonts
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from workbench import agents, casefile, layout, sandbox

from . import queue, skills, sources
from .autogit import AutoGit
from .preview import Previews, find_tinymist

FONTS = resources.files("workbench") / "static" / "fonts"
PDF_MAX = 16_000_000  # a rendered page above this is refused


def pdf_pages(path: Path) -> int:
    """Page count from pdfinfo, or 0 when poppler is not installed."""
    exe = shutil.which("pdfinfo")
    if not exe:
        return 0
    try:
        r = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            first = line.split(":", 1)[1].strip().split(" ")[0]
            return int(first) if first.isdigit() else 0
    return 0


def pdf_page_png(path: Path, page: int, dpi: int) -> tuple[bytes, str]:
    """One page as a PNG, via pdftoppm.

    The app window is a system webview (WebKitGTK / WKWebView) with no built-in PDF
    viewer, so a PDF cannot be an iframe there. Rendering the page to an image does
    not depend on the webview.
    """
    exe = shutil.which("pdftoppm")
    if not exe:
        return b"", "pdftoppm (poppler-utils) is not installed"
    with tempfile.TemporaryDirectory(prefix="workbench-pdf-") as tmp:
        out = Path(tmp) / "page"
        try:
            r = subprocess.run(
                [exe, "-png", "-singlefile", "-f", str(page), "-l", str(page), "-r", str(dpi), str(path), str(out)],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return b"", f"pdftoppm failed: {e}"
        png = out.with_suffix(".png")
        if r.returncode != 0 or not png.is_file():
            return b"", ((r.stderr or r.stdout).strip() or "pdftoppm produced no image")[:400]
        data = png.read_bytes()
    if len(data) > PDF_MAX:
        return b"", "that page raster is too large"
    return data, ""


class Extras:
    def __init__(self, ws, env: dict):
        self.ws = ws
        self.root: Path = ws.root
        self.base_env = env
        self.case = self._case()
        self.evid = sources.Evid(os.environ.get("WORKBENCH_SHELL_URL", ""), os.environ.get("WORKBENCH_EVID_URL", ""))
        self.did = sources.Did(os.environ.get("WORKBENCH_SHELL_URL", ""), os.environ.get("WORKBENCH_DID_URL", ""))
        self.previews = Previews(self.root)
        self.git = AutoGit(ws)
        self.sandboxed = sandbox.enabled()

    def _case(self) -> dict:
        try:
            cfg = casefile.read_config(self.root)
        except (OSError, ValueError):
            cid = self.root.name
            return {"id": cid, "name": cid, "evidSet": "", "agents": []}
        return {"id": cfg.id, "name": cfg.name, "evidSet": cfg.evid_set, "agents": cfg.agents or []}

    def start(self) -> None:
        self.git.start()

    def stop(self) -> None:
        self.git.stop()
        self.previews.stop()

    # ---------- the sandbox, read at every terminal start ----------
    def wrap(self):
        return self._wrap(chdir=True)

    def sandbox_argv(self) -> dict:
        """The prefix a terminal of another app starts under: same walls, its own cwd."""
        prefix, _env = self._wrap(chdir=False)
        return {"argv": prefix, "sandboxed": bool(prefix)}

    def _wrap(self, chdir: bool):
        env = {**self.base_env}
        if not self.sandboxed:
            return [], env
        sel = sources.read_selection(self.root)
        evid_data = self.evid.data_dir()
        also: list[Path] = []
        try:
            for proj in self.did.projects():
                also.append(Path(proj["path"]))
                also.extend(Path(p) for p in proj.get("documents") or [])
        except ConnectionError:
            pass
        prefix = sandbox.bwrap_argv(
            self.root,
            evid_data,
            sources.global_slugs(sel),
            skills=skills.enabled_dirs(self.root),
            also=also,
            chdir=chdir,
        )
        return prefix, sandbox.sandbox_env(env, evid_data)

    def cfg(self) -> dict:
        return {
            "case": self.case,
            "sandbox": self.sandboxed,
            "tinymist": bool(find_tinymist()),
            "agents": agents.available(self.case.get("agents") or []),
        }

    def compile(self, rel: str) -> dict:
        return layout.compile_pdf(self.root, rel)

    # ---------- routes ----------
    def route(self, h, method: str, path: str, q: dict) -> bool:
        root = self.root
        if method == "GET" and path.startswith("/static/fonts/"):
            name = path.rsplit("/", 1)[-1]
            f = FONTS / name
            if "/" in path[len("/static/fonts/") :] or name.startswith(".") or not f.is_file():
                h.send_json(404, {"error": "no such font"})
            else:
                h.send(200, f.read_bytes(), "font/woff2", {"Cache-Control": "max-age=86400"})
            return True
        if method == "GET" and path == "/api/wb":
            h.send_json(200, self.cfg())
            return True
        if method == "GET" and path == "/api/sandbox":
            # What a terminal of this case starts under, for the Datasets tab's Agent pane
            # (evid asks the shell, the shell asks the open case's Write child). No --chdir:
            # evid's terminal keeps its own folder (the set).
            h.send_json(200, self.sandbox_argv())
            return True
        if method == "GET" and path in ("/api/pdf", "/api/pdf/info"):
            # Render a PDF page to an image: the app webview has no PDF viewer.
            t = self.ws.target_path(self.ws.norm(q.get("path", "")))
            if t.suffix.lower() != ".pdf" or not t.is_file():
                h.send_json(415, {"error": "not a PDF in the workdir"})
                return True
            if path == "/api/pdf/info":
                h.send_json(200, {"pages": pdf_pages(t)})
                return True
            try:
                page = max(1, int(q.get("page") or 1))
                dpi = min(220, max(60, int(q.get("dpi") or 130)))
            except ValueError:
                h.send_json(400, {"error": "bad page or dpi"})
                return True
            data, err = pdf_page_png(t, page, dpi)
            if err:
                h.send_json(400, {"error": err})
            else:
                h.send(200, data, "image/png", {"Cache-Control": "no-store"})
            return True
        if method == "GET" and path == "/api/sources":
            h.send_json(200, sources.panel(root, self.evid, self.did))
            return True
        if method == "PUT" and path == "/api/sources":
            b = h.body()
            pulled = None
            pull_error = ""
            try:
                if b.get("mode") and not b.get("pool"):
                    sources.set_mode(root, str(b.get("slug", "")), str(b.get("mode")))
                else:
                    pool = str(b.get("pool", ""))
                    slug = str(b.get("slug", ""))
                    on = bool(b.get("on"))
                    sources.choose(root, pool, slug, on, b.get("mode"))
                    # Connecting a did set is the handoff: the anon PDFs land in this case.
                    if pool == "did" and on:
                        try:
                            pulled = sources.pull(root, slug, self.did)
                        except sources.PullError as e:
                            pull_error = str(e)
            except ValueError as e:
                h.send_json(400, {"error": str(e)})
                return True
            panel = sources.panel(root, self.evid, self.did)
            if pulled is not None:
                panel["pull"] = pulled
            if pull_error:
                panel["pullError"] = pull_error
            h.send_json(200, panel)
            return True
        if method == "POST" and path == "/api/sources/pull":
            b = h.body()
            try:
                if b.get("slug"):
                    result = sources.pull(root, str(b["slug"]), self.did)
                else:
                    result = sources.pull_linked(root, self.did)
            except (sources.PullError, ValueError) as e:
                h.send_json(400, {"error": str(e)})
                return True
            h.send_json(200, {**sources.panel(root, self.evid, self.did), "pull": result})
            return True
        if method == "POST" and path == "/api/sources/gather":
            h.send_json(200, sources.gather(root))
            return True
        if method == "POST" and path == "/api/sources/quote":
            b = h.body()
            try:
                h.send_json(200, sources.quote(
                    root, str(b.get("slug", "")), str(b.get("uuid", "")), str(b.get("where") or "global")))
            except (sources.PullError, ValueError) as e:
                h.send_json(400, {"error": str(e)})
            return True
        if method == "GET" and path == "/api/sources/docs":
            try:
                rows = sources.list_docs(root, q.get("slug", ""), q.get("where") or "global")
            except (sources.PullError, ValueError) as e:
                h.send_json(400, {"error": str(e)})
                return True
            h.send_json(200, {"docs": rows})
            return True
        if method == "POST" and path == "/api/sources/repair":
            slug = str(h.body().get("slug") or "")
            linked = sources.read_selection(root)["evid"]["global"]
            if slug not in linked:
                h.send_json(400, {"error": "repair runs on a linked global evid set"})
                return True
            try:
                r = self.evid.post(f"/api/sets/{slug}/repair")
            except ConnectionError as e:
                h.send_json(400, {"error": str(e)})
                return True
            h.send_json(200, r)
            return True
        if method == "GET" and path == "/api/skills":
            label, paths = skills.roots_label(root)
            h.send_json(200, {"skills": skills.list_skills(root), "root": label, "roots": paths})
            return True
        if method == "PUT" and path == "/api/skills":
            b = h.body()
            if b.get("clear"):
                skills.clear(root)
            elif isinstance(b.get("names"), list):
                skills.set_many(root, b["names"], bool(b.get("on")))
            else:
                skills.set_on(root, str(b.get("name", "")), bool(b.get("on")))
            h.send_json(200, {"skills": skills.list_skills(root)})
            return True
        if method == "GET" and path == "/api/skills/file":
            h.send_json(200, skills.read_file(q.get("name", ""), q.get("path", "SKILL.md"), root))
            return True
        if method == "GET" and path == "/api/preview/errors":
            h.send_json(200, {"errors": self.previews.errors(q.get("path", ""))})
            return True
        if method == "GET" and path == "/api/preview":
            r = self.previews.open(q.get("path", ""), restart=q.get("restart") == "1")
            h.send_json(200 if "data" in r else 400, r)
            return True
        if method == "GET" and path == "/api/git/state":
            h.send_json(200, self.git.state())
            return True
        if method == "GET" and path == "/api/git/recent":
            h.send_json(200, {"commits": self.git.log(int(q.get("n", "30")))})
            return True
        if method == "POST" and path == "/api/git/tag":
            b = h.body()
            try:
                h.send_json(200, self.git.tag(str(b.get("label", "")).strip() or "version", str(b.get("message", ""))))
            except RuntimeError as e:
                h.send_json(400, {"error": str(e)})
            return True
        if method == "POST" and path == "/api/git/commit":
            try:
                h.send_json(200, {"commit": self.git.commit_pending()})
            except RuntimeError as e:
                h.send_json(400, {"error": str(e)})
            return True
        if method == "POST" and path == "/api/compile":
            r = self.compile(str(h.body().get("path", "")))
            h.send_json(200 if "pdf" in r else 400, r)
            return True
        if method == "GET" and path == "/api/queue":
            h.send_json(200, queue.read(root))
            return True
        if method == "POST" and path == "/api/queue/rolled":
            b = h.body()
            h.send_json(
                200,
                queue.rolled(root, str(b.get("target", "")), b.get("notes") or {}, b.get("feedback") or [],
                             b.get("items") or [], b.get("held") or []),
            )
            return True
        return False
