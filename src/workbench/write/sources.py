"""The Write tab's Sources panel: two pools, evid and did.

The choice lives in the case workdir's ``workbench.yml``::

    evid:
      global:
        - laws-lejeloven
      local:
        - notes
    did:
      - set: interview
        mode: placeholder

Until that file exists, ``sources.yml`` is read, and an older ``evid.yml``
(``case:`` / ``reference:``) still loads: both lists become global evid links.
A write rewrites ``workbench.yml``.

A did set is a project in the Anonymization pane (did's own project list). Connecting
it renders PDFs inside that project's directory and ingests only those PDFs into the
case's local evid db, ``<workdir>/evid`` (``anon/anon_set_<slug>/``). Keys stay in the
did project. ``.pull-state.json`` records digests and evid uuids, not names and not
the key file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from workbench import cases, paths
from workbench.layout import REFS_EXTRA
from workbench.paths import REFS, stray_refs
from workbench.casefile import DEFAULT_MODE, MODES, read_config, write_config

STATE_NAME = ".pull-state.json"
# did's PDF --render values. `pseudo` is token Typst compiled locally, not a --render.
RENDER = {"placeholder": "placeholder", "faker": "synthetic", "anon": "redacted"}


class PullError(RuntimeError):
    pass


def _ok_slug(slug: str) -> bool:
    return bool(slug) and bool(cases.ID_RE.match(slug))


def read_selection(workdir: Path) -> dict:
    """The two pools from ``workbench.yml``, or from the older files when it is absent."""
    cfg = read_config(workdir)
    evid = cfg.evid or {}
    return {
        "evid": {"global": list(evid.get("global") or []), "local": list(evid.get("local") or [])},
        "did": [dict(entry) for entry in cfg.did],
    }


def global_slugs(sel: dict) -> list[str]:
    """Evid slugs the sandbox may show from the global store. Local sets live in the workdir."""
    return list(sel["evid"]["global"])


def write_selection(workdir: Path, sel: dict) -> None:
    """Replace the linked sets. Identity and the skill off-list stay as they were."""
    cfg = read_config(workdir)
    evid = sel.get("evid") or {}
    cfg.evid = {"global": list(evid.get("global") or []), "local": list(evid.get("local") or [])}
    did = []
    for entry in sel.get("did") or []:
        mode = entry.get("mode") if entry.get("mode") in MODES else DEFAULT_MODE
        did.append({"set": entry["set"], "mode": mode})
    cfg.did = did
    write_config(workdir, cfg)


def choose(workdir: Path, pool: str, slug: str, on: bool, mode: str | None = None) -> dict:
    """Link or unlink one pill. ``pool`` is ``global``, ``local`` or ``did``."""
    if pool not in ("global", "local", "did"):
        raise ValueError(f"unknown pool {pool!r}")
    if not _ok_slug(slug):
        raise ValueError(f"bad set name {slug!r}")
    sel = read_selection(workdir)
    if pool == "did":
        sel["did"] = [d for d in sel["did"] if d["set"] != slug]
        if on:
            use = mode if mode in MODES else DEFAULT_MODE
            sel["did"].append({"set": slug, "mode": use})
    else:
        for key in ("global", "local"):
            sel["evid"][key] = [s for s in sel["evid"][key] if s != slug]
        if on:
            sel["evid"][pool].append(slug)
    write_selection(workdir, sel)
    return sel


def set_mode(workdir: Path, slug: str, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown pull mode {mode!r}")
    sel = read_selection(workdir)
    found = False
    for entry in sel["did"]:
        if entry["set"] == slug:
            entry["mode"] = mode
            found = True
    if not found:
        raise ValueError(f"{slug} is not a linked did set")
    write_selection(workdir, sel)
    return sel


def is_anon(s: dict) -> bool:
    return s.get("type") == "anon" or str(s.get("slug", "")).endswith("-anon")


def _tab_url(shell_url: str, tab: str) -> str:
    """The child server's URL for TAB, asked of the workbench shell. "" when it is not up."""
    if not shell_url:
        return ""
    try:
        with urlopen(shell_url + "/api/state", timeout=3) as r:
            data = (json.loads(r.read()).get("tabs") or {}).get(tab) or {}
    except (OSError, ValueError):
        return ""
    return (data.get("url") or "").rstrip("/")


class Evid:
    """The evid GUI server the workbench runs (Datasets tab), found through the workbench shell."""

    def __init__(self, shell_url: str = "", evid_url: str = ""):
        self.shell_url = shell_url.rstrip("/")
        self.fixed = evid_url.rstrip("/")
        self.lock = threading.Lock()
        self.cache: dict = {}

    def url(self) -> str:
        return self.fixed or _tab_url(self.shell_url, "evid")

    def _get(self, path: str, ttl: float = 5.0):
        with self.lock:
            hit = self.cache.get(path)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        base = self.url()
        if not base:
            raise ConnectionError("the Datasets app (evid) is not running")
        req = Request(base + path, headers={"X-Evid": "1"})
        try:
            with urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
        except (URLError, OSError, ValueError) as e:
            raise ConnectionError(f"evid did not answer: {e}") from e
        with self.lock:
            self.cache[path] = (time.monotonic(), data)
        return data

    def sets(self) -> list[dict]:
        return self._get("/api/sets")

    def post(self, path: str, body: dict | None = None):
        base = self.url()
        if not base:
            raise ConnectionError("the Datasets app (evid) is not running")
        data = json.dumps(body or {}).encode()
        req = Request(base + path, data=data, method="POST", headers={"X-Evid": "1", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except (URLError, OSError, ValueError) as e:
            raise ConnectionError(f"evid did not answer: {e}") from e

    def stale(self, slug: str) -> int:
        try:
            return int(self._get(f"/api/sets/{slug}/stale", ttl=10.0).get("count") or 0)
        except (ConnectionError, AttributeError, ValueError):
            return 0

    def data_dir(self) -> Path | None:
        try:
            return Path(self._get("/api/config", ttl=60.0)["data_dir"])
        except (ConnectionError, KeyError, TypeError):
            return None


class Did:
    """The Anonymization app. ``projects()`` is the set list its pane is showing."""

    def __init__(self, shell_url: str = "", did_url: str = ""):
        self.shell_url = shell_url.rstrip("/")
        self.fixed = did_url.rstrip("/")

    def url(self) -> str:
        return self.fixed or _tab_url(self.shell_url, "did")

    def projects(self) -> list[dict]:
        base = self.url()
        if not base:
            raise ConnectionError("the Anonymization app (did) is not running")
        req = Request(base + "/api/state", headers={"X-Did": "1"})
        try:
            with urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
        except (URLError, OSError, ValueError) as e:
            raise ConnectionError(f"Anonymization did not answer: {e}") from e
        if not isinstance(data, dict):
            raise ConnectionError("Anonymization did not answer")
        return sets_from_state(data)


def _workdir_of(project_path: str) -> Path:
    path = Path(project_path)
    if path.name.endswith((".yaml", ".yml")):
        return path.parent
    return path


def _slugify(name: str) -> str:
    if _ok_slug(name):
        return name
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)[:64].strip("-")
    return cleaned if _ok_slug(cleaned) else ""


def sets_from_state(data: dict) -> list[dict]:
    """Projects from did's ``/api/state``, as sets Write can connect to.

    The slug is the project folder (an evid set name). The name is the label the
    did pane shows. ``documents`` are the source paths that set hands off; the
    browser payload leaves them out.
    """
    rows: list[dict] = []
    at: dict[str, int] = {}
    for node in data.get("projects") or []:
        if not isinstance(node, dict):
            continue
        raw = str(node.get("path") or "").strip()
        if not raw:
            continue
        workdir = _workdir_of(raw)
        slug = _slugify(workdir.name)
        if not slug:
            continue
        documents: list[str] = []
        for doc in node.get("documents") or []:
            if isinstance(doc, dict) and doc.get("path"):
                documents.append(str(doc["path"]))
            elif isinstance(doc, str) and doc:
                documents.append(doc)
        row = {
            "slug": slug,
            "name": str(node.get("label") or node.get("name") or slug),
            "path": str(workdir),
            "docs": len(documents),
            "documents": documents,
        }
        slot = at.get(slug)
        if slot is None:
            at[slug] = len(rows)
            rows.append(row)
        elif node.get("active"):
            rows[slot] = row
    return rows


def set_snapshot(did_dir: Path, documents: list[str]) -> dict:
    """Digests of the set's documents and of its key YAML. Paths are hashed; names are not kept."""
    docs: dict[str, str] = {}
    for raw in documents:
        path = Path(raw)
        try:
            if not path.is_file():
                continue
            blob = path.read_bytes()
            key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
        except OSError:
            continue
        docs[key] = hashlib.sha256(blob).hexdigest()[:16]
    keys = hashlib.sha256()
    saw = False
    for rel in ("draft/entities.yaml", "draft/entities.yml", "keys/config.yaml", "config.yaml"):
        path = did_dir / rel
        try:
            if path.is_file():
                keys.update(path.read_bytes())
                saw = True
                break
        except OSError:
            continue
    return {"docs": docs, "keys": keys.hexdigest()[:16] if saw else ""}


def _changed(mode: str, record: dict | None, snap: dict) -> bool:
    if not isinstance(record, dict) or not record:
        return True
    return record.get("docs") != snap["docs"] or record.get("keys") != snap["keys"] or record.get("mode") != mode


def _has_entries(path: Path) -> bool:
    """Whether a did review file lists anything. did writes an empty one with a new set."""
    try:
        lines = [ln.split("#", 1)[0].strip() for ln in path.read_text("utf-8").splitlines()]
    except (OSError, UnicodeDecodeError):
        return False
    body = "".join(lines)
    return bool(body) and body not in ("{}", "[]", "null", "~", "---")


def _config_yaml(did_dir: Path) -> Path | None:
    """The set's review, for ``did anon -c``; None when there is none yet.

    did anon applies a given config as the whole review and does not detect. An empty
    one (a set nobody has scanned) would hand every name through, so it is skipped and
    did anon detects on its own.
    """
    for rel in ("draft/entities.yaml", "draft/entities.yml", "keys/config.yaml", "config.yaml"):
        path = did_dir / rel
        if path.is_file() and _has_entries(path):
            return path
    return None


def read_pull_state(workdir: Path) -> dict:
    try:
        data = json.loads((workdir / STATE_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ignore_pull_state(workdir: Path) -> None:
    gi = workdir / ".gitignore"
    text = gi.read_text("utf-8") if gi.is_file() else ""
    if STATE_NAME in text.splitlines():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    gi.write_text(text + STATE_NAME + "\n", "utf-8")


def write_pull_state(workdir: Path, state: dict) -> None:
    _ignore_pull_state(workdir)
    dest = workdir / STATE_NAME
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", "utf-8")
    os.replace(tmp, dest)


def local_sets(workdir: Path) -> list[dict]:
    """Sets already on disk under ``<workdir>/evid`` (evid's anon/plain/sets layout)."""
    root = workdir / "evid"
    out = []
    seen: set[str] = set()
    for bucket, prefix, anon in paths.EVID_LAYOUT:
        folder = root / bucket
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if prefix and not child.name.startswith(prefix):
                continue
            slug = child.name[len(prefix):]
            if not _ok_slug(slug) or slug in seen:
                continue
            seen.add(slug)
            docs_dir = child / "docs"
            docs = sum(1 for p in docs_dir.iterdir() if p.is_dir()) if docs_dir.is_dir() else 0
            out.append({"slug": slug, "anon": anon or slug.endswith("-anon"), "path": str(child), "docs": docs})
    return out


def _global_path(data_dir: Path | None, slug: str, anon: bool) -> str:
    if data_dir is None:
        return ""
    bucket, prefix = ("anon", "anon_set_") if anon else ("plain", "plain_set_")
    return str(data_dir / bucket / f"{prefix}{slug}")


def panel(workdir: Path, evid: Evid, did: Did | None = None) -> dict:
    """What the Sources pane shows: evid pills, and the did pane's sets as pills."""
    sel = read_selection(workdir)
    chosen_g = set(sel["evid"]["global"])
    chosen_l = set(sel["evid"]["local"])
    did_on = {d["set"]: d.get("mode") or DEFAULT_MODE for d in sel["did"]}
    try:
        sets, error = evid.sets(), ""
    except ConnectionError as e:
        sets, error = [], str(e)
    data_dir = None if error else evid.data_dir()
    known = {s["slug"] for s in sets}
    evid_rows = []
    for s in sets:
        anon = is_anon(s)
        slug = s["slug"]
        evid_rows.append(
            {
                "slug": slug,
                "name": s.get("name") or slug,
                "docs": s.get("docs", 0),
                "anon": anon,
                "on": slug in chosen_g,
                "where": "global",
                "path": _global_path(data_dir, slug, anon),
                "stale": evid.stale(slug) if slug in chosen_g and anon else 0,
            }
        )
    for slug in sorted(chosen_g - known):
        evid_rows.append(
            {"slug": slug, "name": slug, "docs": 0, "anon": slug.endswith("-anon"), "on": True,
             "where": "global", "path": "", "missing": True, "stale": 0}
        )
    local_known = set()
    for s in local_sets(workdir):
        local_known.add(s["slug"])
        evid_rows.append(
            {
                "slug": s["slug"],
                "name": s["slug"],
                "docs": s["docs"],
                "anon": s["anon"],
                "on": s["slug"] in chosen_l,
                "where": "local",
                "path": s["path"],
                "stale": 0,
            }
        )
    for slug in sorted(chosen_l - local_known):
        evid_rows.append(
            {"slug": slug, "name": slug, "docs": 0, "anon": slug.endswith("-anon"), "on": True,
             "where": "local", "path": str(workdir / "evid"), "missing": True, "stale": 0}
        )
    evid_rows.sort(key=lambda r: (not r["on"], not r["anon"], r["where"] != "local", r["slug"]))
    did_rows, did_error = _did_rows(did_on, read_pull_state(workdir), did)
    return {"evid": evid_rows, "did": did_rows, "error": error, "didError": did_error}


def _did_rows(chosen: dict, state: dict, did: Did | None) -> tuple[list[dict], str]:
    """Pills for the sets the did pane has. A connected set that left the pane stays, marked missing."""
    projects: list[dict] = []
    error = ""
    if did is not None:
        try:
            projects = did.projects()
        except ConnectionError as e:
            error = str(e)
    rows = []
    seen: set[str] = set()
    for proj in projects:
        slug = proj["slug"]
        seen.add(slug)
        linked = slug in chosen
        mode = chosen.get(slug, DEFAULT_MODE) if linked else DEFAULT_MODE
        if mode not in MODES:
            mode = DEFAULT_MODE
        if linked and not error:
            snap = set_snapshot(Path(proj["path"]), list(proj.get("documents") or []))
            record = state.get(slug) if isinstance(state.get(slug), dict) else None
            stale = _changed(mode, record, snap)
        else:
            stale = linked
        rows.append(
            {
                "slug": slug,
                "name": proj.get("name") or slug,
                "on": linked,
                "mode": mode,
                "path": proj.get("path") or "",
                "docs": int(proj.get("docs") or 0),
                "stale": bool(stale),
            }
        )
    for slug, mode in sorted(chosen.items()):
        if slug in seen:
            continue
        rows.append(
            {
                "slug": slug,
                "name": slug,
                "on": True,
                "mode": mode if mode in MODES else DEFAULT_MODE,
                "path": "",
                "docs": 0,
                "missing": True,
                "stale": True,
            }
        )
    rows.sort(key=lambda r: (not r["on"], r["slug"]))
    return rows, error


def did_bin() -> str:
    return os.environ.get("WORKBENCH_DID_BIN") or shutil.which("did") or "did"


# Runs the checkout that contains `did anon` on the interpreter that already has did's
# libraries. sys.argv arrives as [-c, <src>, anon, ...]; the source dir is popped so
# `anon` is the command treeparse sees.
_DID_SOURCE_RUNNER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv.pop(1))\n"
    "sys.argv[0] = 'did'\n"
    "from did.cli import main\n"
    "main()\n"
)


def _schema_commands(node: object) -> set[str]:
    names: set[str] = set()
    if not isinstance(node, dict):
        return names
    if node.get("type") == "command" and node.get("name"):
        names.add(str(node["name"]))
    for key in ("commands", "subgroups"):
        for child in node.get(key) or []:
            names |= _schema_commands(child)
    return names


def _did_has_anon(exe: str) -> bool:
    try:
        proc = subprocess.run([exe, "-j"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return False
    return "anon" in _schema_commands(data)


def _python_of(exe: str) -> str:
    """The interpreter a console script names in its shebang. A binary falls back to this process."""
    try:
        first = Path(exe).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return sys.executable
    if not first.startswith("#!"):
        return sys.executable
    parts = first[2:].strip().split()
    if not parts:
        return sys.executable
    if Path(parts[0]).name == "env" and len(parts) > 1:
        return shutil.which(parts[1]) or parts[1]
    return parts[0]


def _anon_source() -> Path | None:
    """A did checkout whose CLI has ``anon``. ``$WORKBENCH_DID_SRC``, else the sibling of this repo."""
    env = os.environ.get("WORKBENCH_DID_SRC", "").strip()
    candidates = [Path(env).expanduser()] if env else []
    candidates.append(Path(__file__).resolve().parents[3].parent / "did")
    for cand in candidates:
        if (cand / "src" / "did" / "cli" / "anon.py").is_file():
            return cand
    return None


def did_argv() -> list[str]:
    """How to invoke did so ``anon`` exists.

    ``$WORKBENCH_DID_BIN`` wins. The ``did`` on PATH is often the review GUI, which has
    no ``anon`` command; the PDF handoff then runs the checkout that does, on that
    install's own Python.
    """
    exe = did_bin()
    if os.environ.get("WORKBENCH_DID_BIN", "").strip() or _did_has_anon(exe):
        return [exe]
    src = _anon_source()
    if src is None:
        raise PullError(
            "did has no anon command, so the handoff cannot render PDFs. "
            "Set WORKBENCH_DID_BIN to a did that provides anon, or WORKBENCH_DID_SRC to that checkout."
        )
    return [_python_of(exe), "-c", _DID_SOURCE_RUNNER, str(src / "src")]


def evid_bin() -> str:
    return os.environ.get("WORKBENCH_EVID_BIN") or shutil.which("evid") or "evid"


def _run(argv: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        raise PullError(str(e)) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise PullError(detail or f"{Path(argv[0]).name} exited {proc.returncode}")
    return proc


def _uuid_from(stdout: str) -> str:
    """The uuid `evid doc add` printed. JSON, a `uuid …` line, or the info.yml line `uuid: …`."""
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("uuid"):
        return str(data["uuid"])
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("uuid:"):
            return line.split(":", 1)[1].strip().strip("'\"")
        if low.startswith("uuid "):
            return line.split(None, 1)[1].strip().strip("'\"")
    return ""


def _render_pdfs(did_dir: Path, mode: str, docs: list[Path]) -> list[Path]:
    if not docs:
        raise PullError(f"no documents in {did_dir.name}")
    out = did_dir / "handoff" / "workbench"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    config = _config_yaml(did_dir)
    argv = [*did_argv(), "anon", "-o", str(out)]
    if mode != "pseudo":
        # After the subcommand. did_argv() may be more than the binary
        # (`python -c <runner> <src>`), and a fixed index would put --pdf in front of that.
        at = argv.index("anon") + 1
        argv[at:at] = ["--pdf", "--render", RENDER[mode]]
    if config is not None:
        argv += ["-c", str(config)]
    argv += [str(p) for p in docs]
    _run(argv)
    pdf_dir = out / "pdf"
    if mode == "pseudo":
        typst = os.environ.get("TYPST") or shutil.which("typst")
        if not typst:
            raise PullError("typst not found; a pseudo pull compiles the token Typst")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        typs = sorted((out / "anon").glob("*.typ"))
        if not typs:
            raise PullError("did wrote no token Typst to compile")
        for typ in typs:
            dest = pdf_dir / f"{typ.stem}.pdf"
            _run([typst, "compile", str(typ), str(dest)])
    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file()) if pdf_dir.is_dir() else []
    if not pdfs:
        raise PullError("did produced no PDF")
    return pdfs


def _ensure_anon_set(db: Path, slug: str) -> None:
    db.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [evid_bin(), "--db", str(db), "set", "create", "-s", slug, "--type", "anon"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 and "already exists" not in (proc.stdout + proc.stderr):
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise PullError(detail or f"evid set create exited {proc.returncode}")


def _add_doc(db: Path, slug: str, pdf: Path) -> str:
    proc = _run([evid_bin(), "--db", str(db), "doc", "add", str(pdf), "-s", slug, "--no-index"])
    return _uuid_from(proc.stdout)


def _replace_doc(db: Path, did_dir: Path, slug: str, uuid: str, pdf: Path) -> None:
    """Pass evid an empty label map. Identity variants are PII and are not stored in the case."""
    folder = did_dir / "handoff"
    folder.mkdir(parents=True, exist_ok=True)
    mapping = folder / ".label-map.json"
    try:
        mapping.write_text("{}\n", "utf-8")
        mapping.chmod(0o600)
        _run([
            evid_bin(), "--db", str(db), "doc", "replace", "-s", slug, "-u", uuid,
            "--source", str(pdf), "--map", str(mapping),
        ])
    finally:
        mapping.unlink(missing_ok=True)


def _connected(did: Did, slug: str) -> dict:
    try:
        projects = did.projects()
    except ConnectionError as e:
        raise PullError(str(e)) from e
    found = next((p for p in projects if p["slug"] == slug), None)
    if found is None:
        raise PullError(f"{slug} is not a set in the did pane")
    return found


def _overlaps(a: Path, b: Path) -> bool:
    try:
        left, right = a.resolve(), b.resolve()
        return left == right or left.is_relative_to(right) or right.is_relative_to(left)
    except (OSError, ValueError):
        return False


def pull(workdir: Path, slug: str, did: Did) -> dict:
    """Hand one connected did set off as PDFs into ``<workdir>/evid``."""
    if not _ok_slug(slug):
        raise ValueError(f"bad set name {slug!r}")
    sel = read_selection(workdir)
    entry = next((d for d in sel["did"] if d["set"] == slug), None)
    if entry is None:
        raise PullError(f"{slug} is not a linked did set")
    mode = entry.get("mode") if entry.get("mode") in MODES else DEFAULT_MODE
    proj = _connected(did, slug)
    did_dir = Path(proj["path"])
    if not did_dir.is_dir():
        raise PullError(f"no did set at {did_dir}")
    if _overlaps(did_dir, workdir):
        raise PullError("the did set overlaps this case")
    docs = sorted(
        (Path(p) for p in proj.get("documents") or [] if Path(p).is_file()),
        key=lambda p: (p.name, str(p)),
    )
    pdfs = _render_pdfs(did_dir, mode, docs)
    db = workdir / "evid"
    _ensure_anon_set(db, slug)
    state = read_pull_state(workdir)
    prev = state.get(slug) if isinstance(state.get(slug), dict) else {}
    prev_uuids = prev.get("uuids") or {}
    uuids: dict[str, str] = {}
    added = replaced = 0
    pairs = list(zip(docs, pdfs, strict=False))
    if len(docs) != len(pdfs):
        pairs = [(None, pdf) for pdf in pdfs]
    for index, (src, pdf) in enumerate(pairs):
        if src is not None:
            key = hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:16]
        else:
            key = f"pdf-{index}"
        uuid = str(prev_uuids.get(key) or "")
        if uuid:
            _replace_doc(db, did_dir, slug, uuid, pdf)
            uuids[key] = uuid
            replaced += 1
        else:
            uuids[key] = _add_doc(db, slug, pdf)
            added += 1
    snap = set_snapshot(did_dir, [str(p) for p in docs])
    state[slug] = {"mode": mode, "docs": snap["docs"], "keys": snap["keys"], "uuids": uuids}
    write_pull_state(workdir, state)
    return {"set": slug, "mode": mode, "added": added, "replaced": replaced, "pdfs": [p.name for p in pdfs]}


def pull_linked(workdir: Path, did: Did) -> dict:
    sel = read_selection(workdir)
    if not sel["did"]:
        raise PullError("no linked did sets")
    return {"pulls": [pull(workdir, d["set"], did) for d in sel["did"]]}


def _evid_db_args(db: Path | None) -> list[str]:
    return ["--db", str(db)] if db is not None else []


def list_docs(workdir: Path, slug: str, where: str) -> list[dict]:
    if not _ok_slug(slug):
        raise ValueError(f"bad set name {slug!r}")
    db = workdir / "evid" if where == "local" else None
    proc = _run([evid_bin(), *_evid_db_args(db), "doc", "list", "-s", slug, "-f", "json"])
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError as e:
        raise PullError("evid doc list did not return JSON") from e
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("uuid"):
            continue
        out.append({"uuid": str(row["uuid"]), "title": str(row.get("title") or row.get("name") or row["uuid"])})
    return out


def quote(workdir: Path, slug: str, uuid: str, where: str) -> dict:
    """Start evid's quote pass on one document of a linked set."""
    if not _ok_slug(slug) or not uuid or "/" in uuid:
        raise ValueError("say which document to pass")
    db = workdir / "evid" if where == "local" else None
    proc = _run([evid_bin(), *_evid_db_args(db), "doc", "quote", "-s", slug, "-u", uuid])
    return {"ok": True, "output": (proc.stdout or "")[-2000:]}


def gather(workdir: Path) -> dict:
    """Write the case's one citation file, ``refs/refs.yml`` (:data:`REFS`), for every linked set
    and every hand-added ``refs/extra/*.yml`` (citations from outside evid).

    Each set is gathered on its own, then merged by key, so a document that sits in
    more than one set is written once. labquote prints only the entries a document
    cites, so one store serves every document in the workdir. ``stray`` lists the
    other ``refs.yml`` files in the workdir: they are in the wrong place and not read.
    """
    sel = read_selection(workdir)
    out = workdir / REFS
    out.parent.mkdir(parents=True, exist_ok=True)
    stray = stray_refs(workdir)
    jobs: list[tuple[str, Path | None]] = [(slug, None) for slug in sel["evid"]["global"]]
    local_db = workdir / "evid"
    for slug in sel["evid"]["local"]:
        jobs.append((slug, local_db))
    for entry in sel["did"]:
        slug = entry["set"]
        if any(s == slug and db is not None for s, db in jobs):
            continue
        jobs.append((slug, local_db))
    errors: list[str] = []
    parts: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="workbench-refs-") as tmp:
        for i, (slug, db) in enumerate(jobs):
            dest = Path(tmp) / f"{i:02d}-{slug}.yml"
            try:
                _run([evid_bin(), *_evid_db_args(db), "set", "gather", slug, "-o", str(dest)])
            except PullError as e:
                errors.append(f"{slug}: {e}")
                continue
            parts.append(dest)
        extra = sorted((workdir / REFS_EXTRA).glob("*.yml")) if (workdir / REFS_EXTRA).is_dir() else []
        parts += extra  # hand-added citations from outside evid; the sets come first
        if not parts:
            return {"ref": "", "errors": errors, "stray": stray}
        try:
            _run([evid_bin(), "refs", "merge", "-o", str(out), *[str(p) for p in parts]])
        except PullError as e:
            errors.append(f"merge: {e}")
            return {"ref": "", "errors": errors, "stray": stray}
    return {"ref": REFS, "errors": errors, "stray": stray}
