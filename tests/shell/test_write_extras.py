"""What the workbench adds to the Write tab: sources, skills, queue, auto-git, sandbox, routes."""

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from workbench import sandbox
from workbench.write import autogit, queue, skills, sources


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("WORKBENCH_DID", str(tmp_path / "did_workdirs"))  # never list the real did sets
    monkeypatch.setenv("WORKBENCH_EVID_BIN", "/nonexistent/evid")  # never touch the real evid store
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def case(tmp_path):
    d = tmp_path / "cases" / "2026-014"
    d.mkdir(parents=True)
    (d / "case.toml").write_text('id = "2026-014"\nname = "Tenancy dispute"\nevid_set = "2026-014-anon"\n')
    (d / "memo.typ").write_text("= Memo\n\nText.\n")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "start"], check=True)
    return d


class FakeEvid:
    def __init__(self, sets, data_dir=None):
        self._sets, self._data = sets, data_dir

    def sets(self):
        if self._sets is None:
            raise ConnectionError("the Datasets app (evid) is not running")
        return self._sets

    def data_dir(self):
        return self._data

    def stale(self, slug):
        return 2 if slug == "2026-014-anon" else 0


class FakeDid:
    """The did pane's projects. ``None`` means the Anonymization app is down."""

    def __init__(self, projects):
        self._projects = projects

    def projects(self):
        if self._projects is None:
            raise ConnectionError("the Anonymization app (did) is not running")
        return self._projects


def _proj(folder, docs, name=None):
    docs = [Path(p) for p in docs]
    return {
        "slug": folder.name,
        "name": name or folder.name,
        "path": str(folder),
        "docs": len(docs),
        "documents": [str(p) for p in docs],
    }


# ---------- sources ----------
def test_old_evid_yml_is_global_links(case):
    assert sources.read_selection(case) == {"evid": {"global": [], "local": []}, "did": []}
    (case / "evid.yml").write_text("case:\n  - 2026-014-anon\nreference:\n  - laws\n")
    assert sources.read_selection(case) == {"evid": {"global": ["2026-014-anon", "laws"], "local": []}, "did": []}
    (case / "evid.yml").write_text("case: [a-anon]\nreference: []  # none\n")
    assert sources.read_selection(case)["evid"]["global"] == ["a-anon"]


def test_first_write_folds_the_older_files(case):
    (case / "sources.yml").write_text(
        "evid:\n  global:\n    - litc\n    - lokk\n  local: []\n"
        "did:\n  - set: interview\n    mode: placeholder\n"
    )
    (case / "skills.yml").write_text("off:\n  - citing\n  - notat\n")
    (case / "evid.yml").write_text("reference:\n  - gone\n")
    sources.choose(case, "local", "notes", True)
    text = (case / "workbench.yml").read_text()
    assert "    - litc\n" in text and "    - lokk\n" in text and "    - notes\n" in text
    assert "mode: placeholder" in text and "gone" not in text and "    - did\n" not in text
    assert "    - citing\n" in text and "    - notat\n" in text
    assert 'id: "2026-014"' in text and 'name: "Tenancy dispute"' in text
    assert 'evid_set: "2026-014-anon"' in text
    for name in ("case.toml", "sources.yml", "skills.yml", "evid.yml"):
        assert not (case / name).exists()


def test_workbench_yml_keeps_every_section(case):
    (case / "skills.yml").write_text("off:\n  - citing\n  - notat\n")
    (case / "evid.yml").write_text("case:\n  - gone\nreference: []\n")
    (case / "workbench.yml").write_text(
        '# workbench case\nid: "2026-014"\nname: "Tenancy dispute"\n'
        'evid_set: "2026-014-anon"\n\nevid:\n  global:\n    - laws\n  local: []\n'
        "did:\n  - set: interview\n    mode: anon\n\nskills:\n  off:\n    - memo\n"
    )
    assert sources.read_selection(case)["evid"]["global"] == ["laws"]
    assert skills.read_off(case) == ["memo"]
    skills.write_off(case, ["citing"])
    assert sources.read_selection(case)["did"] == [{"set": "interview", "mode": "anon"}]
    assert skills.read_off(case) == ["citing"]
    assert 'evid_set: "2026-014-anon"' in (case / "workbench.yml").read_text()
    assert not (case / "skills.yml").exists() and not (case / "evid.yml").exists()


def test_sources_roundtrip_and_default_mode(case):
    sources.write_selection(case, {"evid": {"global": ["laws"], "local": ["notes"]}, "did": [{"set": "interview", "mode": "placeholder"}]})
    text = (case / "workbench.yml").read_text()
    assert text.startswith("# workbench case") and "    - laws\n" in text and "mode: placeholder" in text
    assert 'id: "2026-014"' in text and 'evid_set: "2026-014-anon"' in text
    for name in ("case.toml", "sources.yml", "skills.yml", "evid.yml"):
        assert not (case / name).exists()
    assert sources.read_selection(case)["did"] == [{"set": "interview", "mode": "placeholder"}]
    sources.choose(case, "did", "other", True)
    assert sources.read_selection(case)["did"][-1] == {"set": "other", "mode": "placeholder"}
    sources.set_mode(case, "interview", "faker")
    modes = {d["set"]: d["mode"] for d in sources.read_selection(case)["did"]}
    assert modes == {"interview": "faker", "other": "placeholder"}


def test_did_pane_projects_are_the_sets():
    rows = sources.sets_from_state({"projects": [
        {
            "path": "/d/did_workdirs/interview/project.did-project.yaml",
            "name": "Interview",
            "label": "Interview",
            "documents": [{"path": "/secret/lease.pdf"}, {"path": "/secret/gone.pdf", "missing": True}],
        },
        {
            "path": "/d/did_workdirs/not a set/project.did-project.yaml",
            "name": "Odd",
            "label": "Odd one",
            "documents": [],
        },
        {
            "path": "/d/elsewhere/interview/project.did-project.yaml",
            "name": "Other",
            "label": "Other interview",
            "active": True,
            "documents": [{"path": "/secret/live.pdf"}],
        },
    ]})
    by = {r["slug"]: r for r in rows}
    assert by["interview"]["name"] == "Other interview"  # the open project wins a shared folder name
    assert by["interview"]["path"] == "/d/elsewhere/interview"
    assert by["interview"]["documents"] == ["/secret/live.pdf"]
    assert by["not-a-set"]["name"] == "Odd one"
    assert by["not-a-set"]["path"] == "/d/did_workdirs/not a set"


def test_panel_two_pools(case, tmp_path, monkeypatch):
    root = tmp_path / "did_workdirs"
    monkeypatch.setenv("WORKBENCH_DID", str(root))
    folder = root / "interview"
    folder.mkdir(parents=True)
    pdf = folder / "lease.pdf"
    pdf.write_bytes(b"%PDF")
    loose = root / "not-a-project"  # a directory is not a did set
    loose.mkdir()
    (loose / "x.pdf").write_bytes(b"%PDF")
    hearing = root / "hearing"
    hearing.mkdir()
    ev = FakeEvid([
        {"slug": "laws", "name": "Laws", "type": "normal", "docs": 3},
        {"slug": "2026-014-anon", "name": "Case", "type": "anon", "docs": 5},
    ])
    did = FakeDid([_proj(folder, [pdf], name="Interview"), _proj(hearing, [])])
    sources.choose(case, "global", "laws", True)
    sources.choose(case, "global", "2026-014-anon", True)
    sources.choose(case, "did", "interview", True)
    p = sources.panel(case, ev, did)
    evid = {r["slug"]: r for r in p["evid"]}
    pills = {r["slug"]: r for r in p["did"]}
    assert [r["slug"] for r in p["evid"] if r["on"]] == ["2026-014-anon", "laws"]
    assert evid["2026-014-anon"]["anon"] and evid["2026-014-anon"]["where"] == "global" and evid["2026-014-anon"]["stale"] == 2
    assert evid["laws"]["where"] == "global" and not evid["laws"]["anon"]
    assert pills["interview"]["on"] and pills["interview"]["name"] == "Interview"
    assert pills["interview"]["mode"] == "placeholder" and pills["interview"]["stale"] is True
    assert pills["interview"]["path"].endswith("/interview")
    assert "documents" not in pills["interview"]
    assert pills["hearing"]["on"] is False
    assert "not-a-project" not in pills
    sources.choose(case, "global", "laws", False)
    assert "laws" not in sources.read_selection(case)["evid"]["global"]
    down = sources.panel(case, FakeEvid([]), FakeDid(None))
    missing = {r["slug"]: r for r in down["did"]}
    assert missing["interview"]["on"] and missing["interview"]["missing"]
    assert "not running" in down["didError"]


def test_panel_without_evid_keeps_a_linked_global_set(case):
    (case / "evid.yml").write_text("case:\n  - 2026-014-anon\nreference: []\n")
    p = sources.panel(case, FakeEvid(None))
    assert "not running" in p["error"]
    assert p["evid"][0]["slug"] == "2026-014-anon" and p["evid"][0]["missing"] and p["did"] == []


def test_did_pill_goes_stale_without_copying_keys(case, tmp_path, monkeypatch):
    root = tmp_path / "did_workdirs"
    monkeypatch.setenv("WORKBENCH_DID", str(root))
    folder = root / "interview"
    (folder / "draft").mkdir(parents=True)
    pdf = folder / "lease.pdf"
    pdf.write_bytes(b"%PDF-1")
    (folder / "draft" / "entities.yaml").write_text("name: SECRET-NAME\n")
    sources.choose(case, "did", "interview", True)
    did = FakeDid([_proj(folder, [pdf])])
    snap = sources.set_snapshot(folder, [str(pdf)])
    sources.write_pull_state(case, {"interview": {**snap, "mode": "placeholder", "uuids": {}}})
    assert sources.panel(case, FakeEvid([]), did)["did"][0]["stale"] is False
    extra = folder / "extra.pdf"
    extra.write_bytes(b"%PDF-2")
    did._projects[0]["documents"].append(str(extra))
    did._projects[0]["docs"] = 2
    assert sources.panel(case, FakeEvid([]), did)["did"][0]["stale"] is True
    did._projects[0]["documents"].pop()
    did._projects[0]["docs"] = 1
    sources.write_pull_state(case, {"interview": {**sources.set_snapshot(folder, [str(pdf)]), "mode": "placeholder", "uuids": {}}})
    (folder / "draft" / "entities.yaml").write_text("name: SECRET-NAME\nother: 1\n")
    assert sources.panel(case, FakeEvid([]), did)["did"][0]["stale"] is True
    blob = "".join(p.read_text(errors="ignore") for p in case.rglob("*") if p.is_file())
    assert "SECRET-NAME" not in blob


def test_pull_writes_pdfs_only(case, tmp_path, monkeypatch):
    root = tmp_path / "did_workdirs"
    monkeypatch.setenv("WORKBENCH_DID", str(root))
    folder = root / "interview"
    (folder / "draft").mkdir(parents=True)
    (folder / "lease.pdf").write_bytes(b"%PDF-1")
    (folder / "draft" / "entities.yaml").write_text("person: SECRET-NAME\n")
    sources.choose(case, "did", "interview", True)
    sources.set_mode(case, "interview", "faker")
    did = FakeDid([_proj(folder, [folder / "lease.pdf"])])
    log = tmp_path / "calls.log"
    didbin = tmp_path / "didbin"
    evidbin = tmp_path / "evidbin"
    didbin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\nfrom pathlib import Path\n"
        f"args = sys.argv[1:]\nout = Path(args[args.index('-o') + 1])\n"
        "(out / 'pdf').mkdir(parents=True)\n(out / 'keys').mkdir()\n"
        "(out / 'keys' / 'config.yaml').write_text('person: SECRET-NAME\\n')\n"
        "(out / 'pdf' / 'lease.pdf').write_bytes(b'%PDF-pulled')\n"
        f"Path({str(log)!r}).open('a').write('DID ' + ' '.join(args) + '\\n')\n"
    )
    evidbin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, shutil\nfrom pathlib import Path\n"
        f"args = sys.argv[1:]\nPath({str(log)!r}).open('a').write('EVID ' + ' '.join(args) + '\\n')\n"
        "if args[args.index('--db') + 1] and 'add' in args:\n"
        "    db = Path(args[args.index('--db') + 1])\n"
        "    slug = args[args.index('-s') + 1]\n"
        "    src = Path(args[args.index('add') + 1])\n"
        "    dest = db / 'anon' / f'anon_set_{slug}' / 'docs' / 'u1'\n"
        "    dest.mkdir(parents=True)\n"
        "    shutil.copy(src, dest / 'original.pdf')\n"
        "    print(json.dumps({'uuid': 'u1'}))\n"
    )
    didbin.chmod(0o755)
    evidbin.chmod(0o755)
    monkeypatch.setenv("WORKBENCH_DID_BIN", str(didbin))
    monkeypatch.setenv("WORKBENCH_EVID_BIN", str(evidbin))
    result = sources.pull(case, "interview", did)
    assert result["added"] == 1 and result["mode"] == "faker"
    pdf = case / "evid" / "anon" / "anon_set_interview" / "docs" / "u1" / "original.pdf"
    assert pdf.read_bytes() == b"%PDF-pulled"
    blob = "".join(p.read_text(errors="ignore") for p in case.rglob("*") if p.is_file())
    assert "SECRET-NAME" not in blob
    assert not (case / "keys").exists()
    calls = log.read_text()
    assert "--render synthetic" in calls
    assert "--type anon" in calls
    assert sources.panel(case, FakeEvid([]), did)["did"][0]["stale"] is False
    with pytest.raises(sources.PullError, match="did pane"):
        sources.pull(case, "interview", FakeDid([]))
    assert sources._uuid_from("uuid: abc-1\n") == "abc-1"
    assert sources._uuid_from("title: x\nUUID: kept\n") == "kept"


def test_did_argv_uses_anon_checkout_when_path_did_has_none(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKBENCH_DID_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "did"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['-j']:\n"
        "    print(json.dumps({'name': 'did', 'commands': [{'name': 'gui', 'type': 'command'}], 'subgroups': []}))\n"
        "else:\n"
        "    sys.exit('no anon')\n"
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    src = tmp_path / "didproj"
    (src / "src" / "did" / "cli").mkdir(parents=True)
    (src / "src" / "did" / "cli" / "anon.py").write_text("# anon\n")
    monkeypatch.setenv("WORKBENCH_DID_SRC", str(src))
    argv = sources.did_argv()
    assert argv[0] != str(exe)
    assert argv[1:3] == ["-c", sources._DID_SOURCE_RUNNER]
    assert argv[3] == str(src / "src")
    monkeypatch.setenv("WORKBENCH_DID_BIN", str(exe))
    assert sources.did_argv() == [str(exe)]


def test_pull_keeps_pdf_flag_after_anon(case, tmp_path, monkeypatch):
    """A multi-word did invocation still receives `anon --pdf`, not `--pdf` as the program."""
    folder = tmp_path / "interview"
    (folder / "draft").mkdir(parents=True)
    (folder / "lease.pdf").write_bytes(b"%PDF-1")
    sources.choose(case, "did", "interview", True)
    log = tmp_path / "calls.log"
    runner = tmp_path / "py"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:1] != ['-c'] or args[3:6] != ['anon', '--pdf', '--render']:\n"
        "    sys.exit('bad argv ' + ' '.join(args))\n"
        "out = Path(args[args.index('-o') + 1])\n"
        "(out / 'pdf').mkdir(parents=True)\n"
        "(out / 'pdf' / 'lease.pdf').write_bytes(b'%PDF-pulled')\n"
        f"Path({str(log)!r}).write_text(' '.join(args))\n"
    )
    runner.chmod(0o755)
    evidbin = tmp_path / "evidbin"
    evidbin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "args = sys.argv[1:]\n"
        "if 'add' in args:\n"
        "    print(json.dumps({'uuid': 'u1'}))\n"
    )
    evidbin.chmod(0o755)
    monkeypatch.setenv("WORKBENCH_EVID_BIN", str(evidbin))
    monkeypatch.setattr(sources, "did_argv", lambda: [str(runner), "-c", "runner", str(tmp_path / "src")])
    did = FakeDid([_proj(folder, [folder / "lease.pdf"])])
    result = sources.pull(case, "interview", did)
    assert result["added"] == 1
    assert "anon --pdf --render placeholder" in log.read_text()


def test_an_unscanned_set_is_detected_not_passed_through(tmp_path):
    """did writes an empty draft/entities.yaml with a new set. Given as -c, did anon would
    take it as the whole review and replace nothing, so it is not given."""
    did_dir = tmp_path / "set"
    (did_dir / "draft").mkdir(parents=True)
    (did_dir / "draft" / "entities.yaml").write_text("")
    assert sources._config_yaml(did_dir) is None
    (did_dir / "draft" / "entities.yaml").write_text("# no review yet\n{}\n")
    assert sources._config_yaml(did_dir) is None
    (did_dir / "draft" / "entities.yaml").write_text("PERSON:\n- id: PERSON_1\n  variants: [A]\n")
    assert sources._config_yaml(did_dir) == did_dir / "draft" / "entities.yaml"


# ---------- skills ----------
@pytest.fixture
def skill_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    real = tmp_path / "repo" / "memo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text('---\nname: memo\ndescription: "Write a memo."\n---\n# memo\n')
    (real / "ref.md").write_text("x\n")
    (real / "helper.py").write_text("x\n")
    root.mkdir()
    (root / "memo").symlink_to(real)
    (root / "did").mkdir()
    (root / "did" / "SKILL.md").write_text("---\nname: did\ndescription: >\n  pii removal,\n  local only\n---\n")
    (root / "notaskill").mkdir()
    monkeypatch.setenv("WORKBENCH_SKILLS", str(root))
    return root


def test_skills_default_and_toggle(case, skill_root):
    s = {x["name"]: x for x in skills.list_skills(case)}
    assert set(s) == {"memo", "did"}
    assert s["memo"]["on"] and s["memo"]["link"] and s["memo"]["description"] == "Write a memo."
    assert not s["did"]["on"]  # Write agents never handle raw case files
    assert s["did"]["description"] == "pii removal, local only"  # a folded block scalar
    assert s["memo"]["files"] == ["SKILL.md", "ref.md"]  # markdown only; helper.py is not listed
    skills.set_on(case, "memo", False)
    assert skills.read_off(case) == ["did", "memo"]
    assert [n for n, _ in skills.enabled_dirs(case)] == []
    skills.set_on(case, "did", True)
    assert [n for n, _ in skills.enabled_dirs(case)] == ["did"]
    assert skills.read_file("memo", "SKILL.md")["text"].startswith("---")
    with pytest.raises(FileNotFoundError):
        skills.read_file("memo", "../../etc/passwd")


def _skill(d):
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: {d.name} skill\n---\n")


def test_skill_tree_and_clear(case, tmp_path, monkeypatch):
    root = tmp_path / "installed"
    root.mkdir()
    # The tree is the folders inside the skill directory. A symlink is one skill:
    # the repo it points at is not walked as more groups.
    one = tmp_path / "work" / "pack" / "a" / "x" / "one"
    _skill(one)
    (one / "helper.py").write_text("x\n")
    (one / "notes.md").write_text("n\n")
    (one / "src").mkdir()
    (one / "src" / "hidden.md").write_text("h")
    _skill(root / "authoring" / "quotation" / "labquote")
    (root / "one").symlink_to(one)
    (root / "zz").symlink_to(one)  # the same folder, listed once
    (root / "loose").mkdir()
    (root / "loose" / "SKILL.md").write_text("---\nname: loose\ndescription: here\n---\n")
    monkeypatch.setenv("WORKBENCH_SKILLS", str(root))

    s = {x["name"]: x for x in skills.list_skills(case)}
    assert s["labquote"]["group"] == ["authoring", "quotation"]
    assert s["one"]["group"] == [] and s["one"]["link"] and s["one"]["folder"] == ""
    assert "zz" not in s  # same resolved folder as one
    assert s["loose"]["group"] == []
    assert s["one"]["files"] == ["SKILL.md", "notes.md"]
    assert "helper.py" not in s["one"]["files"]
    assert "src/hidden.md" not in s["one"]["files"]


def test_shipped_skill_dirs():
    rows = skills.parse_dirs(skills.shipped_text())
    assert [r["name"] for r in rows] == ["claude", "grok", "hermes", "codex"]
    assert [r["path"] for r in rows] == [
        "~/.claude/skills",
        "~/.grok/skills",
        "~/.hermes/skills",
        "~/.codex/skills",
    ]


def test_config_dirs_are_the_skill_tree(case, tmp_path, monkeypatch):
    monkeypatch.delenv("WORKBENCH_SKILLS", raising=False)
    claude = tmp_path / "claude-skills"
    hermes = tmp_path / "hermes-skills"
    _skill(claude / "memo")
    _skill(hermes / "creative" / "plan")
    cfg = tmp_path / "config" / "workbench"
    cfg.mkdir(parents=True)
    (cfg / "skills.toml").write_text(
        f'[[dir]]\nname = "claude"\npath = "{claude}"\n[[dir]]\nname = "hermes"\npath = "{hermes}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    s = {x["name"]: x for x in skills.list_skills(case)}
    assert s["memo"]["group"] == ["claude"]
    assert s["plan"]["group"] == ["hermes", "creative"]
    label, paths = skills.roots_label()
    assert label == "claude · hermes"
    assert paths == [str(claude), str(hermes)]


def test_case_dirs_are_where_skills_are_read(case, tmp_path, monkeypatch):
    monkeypatch.delenv("WORKBENCH_SKILLS", raising=False)
    one = tmp_path / "claude" / "skills"
    two = tmp_path / "extra"
    _skill(one / "memo")
    _skill(two / "labquote")
    missing = tmp_path / "gone"
    cfg = tmp_path / "config" / "workbench"
    cfg.mkdir(parents=True)
    (cfg / "skills.toml").write_text(
        '[[dir]]\nname = "other"\npath = "/no/such/catalogue"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (case / "workbench.yml").write_text(
        'id: "2026-014"\nname: "Tenancy dispute"\n\nskills:\n'
        f'  dirs:\n    - "{one}"\n    - "{two}"\n    - "{missing}"\n'
        "  off:\n    - did\n",
        encoding="utf-8",
    )
    s = {x["name"]: x for x in skills.list_skills(case)}
    assert set(s) == {"memo", "labquote"}
    assert s["memo"]["group"] == ["claude"]
    assert s["labquote"]["group"] == ["extra"]
    label, paths = skills.roots_label(case)
    assert label == "claude · extra"
    assert paths == [str(one.resolve()), str(two.resolve())]
    skills.write_off(case, ["memo"])
    text = (case / "workbench.yml").read_text()
    assert f'"{one}"' in text and f'"{missing}"' in text and "    - memo\n" in text
    assert skills.read_off(case) == ["memo"]


def test_env_replaces_the_case_dirs(case, tmp_path, monkeypatch):
    root = tmp_path / "installed"
    _skill(root / "one")
    other = tmp_path / "other"
    _skill(other / "two")
    (case / "workbench.yml").write_text(
        f'id: "2026-014"\nname: "Tenancy dispute"\n\nskills:\n  dirs:\n    - "{other}"\n  off: []\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_SKILLS", str(root))
    assert [x["name"] for x in skills.list_skills(case)] == ["one"]


def test_skill_off_list_ignores_a_path(case, tmp_path, monkeypatch):
    root = tmp_path / "installed"
    root.mkdir()
    _skill(root / "one")
    _skill(root / "two")
    monkeypatch.setenv("WORKBENCH_SKILLS", str(root))
    skills.set_many(case, ["one", "two", "../no", ""], False)
    assert skills.read_off(case) == ["did", "one", "two"]
    skills.set_many(case, ["pack/a", "one"], True)  # a path is not a skill name; `one` comes back on
    assert "one" not in skills.read_off(case) and "two" in skills.read_off(case)
    names = sorted(x["name"] for x in skills.list_skills(case))
    skills.clear(case)
    assert skills.read_off(case) == names == ["one", "two"]
    assert skills.enabled_dirs(case) == []


# ---------- queue ----------
def test_queue_rolled(case):
    assert queue.read(case)["at"] == 0
    q = queue.rolled(case, "claude", {"memo.typ": "tighten"}, [3], [{"kind": "save", "path": "memo.typ"}])
    assert q["target"] == "claude" and q["feedback"] == ["3"] and q["notes"]["memo.typ"] == "tighten"
    q = queue.rolled(case, "opencode", {}, [4, 3], [])
    assert q["feedback"] == ["3", "4"] and len(q["history"]) == 2 and q["history"][0]["target"] == "opencode"


def test_queue_forgets_a_previous_case_at_the_same_path(case):
    queue.rolled(case, "claude", {}, [1, 2], [])
    assert queue.read(case)["feedback"] == ["1", "2"]
    shutil.rmtree(case / ".git")  # the case is made again at the same path: feedback ids start at 1 again
    subprocess.run(["git", "-C", str(case), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(case), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(case), "commit", "-qm", "start again"], check=True)
    q = queue.read(case)
    assert q["at"] == 0 and q["feedback"] == [] and q["history"] == []


# ---------- auto-git ----------
class WS:
    def __init__(self, root, changes=None):
        self.root, self.changes = root, changes or {}


def log(case):
    return subprocess.run(["git", "-C", str(case), "log", "--format=%s"], capture_output=True, text=True).stdout.split("\n")


def test_autogit_waits_for_quiet_then_attributes(case):
    ws = WS(case, {"memo.typ": {"by": "you", "at": 0}})
    g = autogit.AutoGit(ws, idle=5)
    (case / "memo.typ").write_text("= Memo\n\nMore text.\n")
    (case / "angle.typ").write_text("= Angle\n")
    (case / ".juranotes.json").write_text("{}\n")
    assert g.tick(now=100) is None  # first sight
    assert g.tick(now=103) is None  # not quiet long enough
    done = g.tick(now=106)
    assert done is not None
    msgs = log(case)
    assert "user: memo.typ" in msgs and "agent: angle.typ" in msgs and "notes: annotations and feedback" in msgs
    assert g.status() == []


def test_autogit_stands_back(case):
    g = autogit.AutoGit(WS(case))
    (case / "memo.typ").write_text("changed\n")
    subprocess.run(["git", "-C", str(case), "add", "memo.typ"], check=True)  # someone is committing
    g.tick(now=0)
    assert g.tick(now=100) is None
    subprocess.run(["git", "-C", str(case), "reset", "-q"], check=True)
    (case / ".git" / "MERGE_HEAD").write_text("x\n")
    g.tick(now=200)
    assert g.tick(now=300) is None and g.busy()
    (case / ".git" / "MERGE_HEAD").unlink()
    g.tick(now=400)
    assert g.tick(now=500)["who"] == "agent"


def test_autogit_keeps_evid_pdfs_out_of_git(case):
    """A case made before the rule tracked its stored PDFs. The migration untracks them (the files
    stay on disk), records the rule, and later handoffs leave only the labels and metadata in git."""
    pdf = case / "evid" / "plain" / "set" / "docs" / "abc" / "original.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    (pdf.parent / "info.yml").write_text("title: x\n")
    subprocess.run(["git", "-C", str(case), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(case), "commit", "-qm", "old behaviour"], check=True)

    g = autogit.AutoGit(WS(case))
    assert g.migrate() and g.migrated  # called from the autogit loop at the first idle tick
    tracked = subprocess.run(["git", "-C", str(case), "ls-files"], capture_output=True, text=True).stdout
    assert "original.pdf" not in tracked and "info.yml" in tracked
    assert pdf.is_file()  # the document itself stays
    assert autogit.EVID_PDF_IGNORE in (case / ".gitignore").read_text()
    assert autogit.EVID_RULE_COMMIT in log(case)
    assert g.status() == [] and g.migrated

    # a set handed off later: the PDF is ignored, the work beside it is not
    later = case / "evid" / "plain" / "set" / "docs" / "def"
    (later / "label").mkdir(parents=True)
    (later / "original.pdf").write_bytes(b"%PDF-1.4\n")
    (later / "label" / "labels.json").write_text("{}\n")
    assert g.status() == [("??", "evid/plain/set/docs/def/label/labels.json")]


def test_autogit_leaves_a_case_without_evid_alone(case):
    g = autogit.AutoGit(WS(case))
    assert g.migrate() and g.migrated
    ignore = case / ".gitignore"
    assert not ignore.exists() or autogit.EVID_PDF_IGNORE not in ignore.read_text()
    assert log(case)[0] == "start"


def test_tag_state(case):
    g = autogit.AutoGit(WS(case))
    (case / "brief.typ").write_text("= Brief\n")
    t = g.tag("Huslejenævn submission", "filed")
    assert t["name"].startswith("state/") and t["name"].endswith("-huslejenaevn-submission")
    assert "agent: brief.typ" in log(case)
    st = g.state()
    assert st["tags"][0]["name"] == t["name"] and st["pending"] == 0 and st["commits"] == 2
    assert st["tags"][0]["hash"] == st["last"]["hash"] == t["hash"]
    assert g.log()[0]["tags"] == [t["name"]]
    with pytest.raises(RuntimeError, match="already exists"):
        g.tag("Huslejenævn submission")


# ---------- sandbox ----------
def test_bwrap_argv(tmp_path, monkeypatch):
    """Everything is there as in any terminal, except what may hold a case's personal data."""
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-other-project").mkdir(parents=True)
    (home / ".claude" / "history.jsonl").write_text("{}\n")
    (home / "did" / "2026-014" / "draft").mkdir(parents=True)
    raw = home / "Downloads" / "2026-014"
    raw.mkdir(parents=True)
    (raw / "lease.pdf").write_text("x")
    loose = home / "letter.pdf"
    loose.write_text("x")
    (home / "did" / "2026-014" / "draft" / "sources.json").write_text(
        json.dumps({"sources": [{"path": str(raw / "lease.pdf")}, {"path": str(loose)}]})
    )
    data = tmp_path / "evid"
    (data / "anon" / "anon_set_a-anon").mkdir(parents=True)
    (data / "plain" / "plain_set_secret").mkdir(parents=True)
    (data / "sets" / "old-secret").mkdir(parents=True)
    skill = tmp_path / "repo" / "memo"
    skill.mkdir(parents=True)
    cases_root = tmp_path / "cases"
    case = cases_root / "c"
    case.mkdir(parents=True)
    (cases_root / "other").mkdir()
    local = case / "evid" / "anon" / "anon_set_pulled"
    local.mkdir(parents=True)
    outside = home / "other-did" / "interview"
    outside.mkdir(parents=True)
    note = home / "note.pdf"
    note.write_text("x")
    a = sandbox.bwrap_argv(case, data, ["a-anon", "../x", "missing"], home=home, skills=[("memo", skill)],
                           did_root=home / "did", cases_root=cases_root,
                           also=[outside, note, home / "did" / "already"])
    joined = " ".join(a)
    assert a[:6] == ["bwrap", "--die-with-parent", "--unshare-pid", "--dev-bind", "/", "/"] and a[-1] == "--"
    assert "--tmpfs /tmp" in " ".join(a[:12])
    assert f"--tmpfs {home / 'did'}" in joined  # did's folder
    assert f"--tmpfs {raw}" in joined  # the folder the raw files came from
    assert f"--ro-bind /dev/null {loose}" in joined  # a raw file straight in $HOME: just the file
    assert f"--tmpfs {home} " not in joined + " "  # never the whole home
    anon = (data / "anon").resolve()
    plain = (data / "plain").resolve()
    legacy = (data / "sets").resolve()
    assert f"--tmpfs {anon}" in joined and f"--tmpfs {plain}" in joined
    assert f"--tmpfs {legacy}" in joined
    assert f"--ro-bind {anon / 'anon_set_a-anon'}" in joined
    assert "secret" not in joined and "../x" not in joined
    assert f"--tmpfs {cases_root.resolve()}" in joined and f"--bind {case.resolve()} {case.resolve()}" in joined
    assert joined.index(f"--tmpfs {cases_root.resolve()}") < joined.index(f"--bind {case.resolve()}")
    assert f"{home / '.claude' / 'projects'}" in joined and f"{home / '.claude' / 'history.jsonl'}" in joined
    assert f"--ro-bind {skill} {home / '.claude' / 'skills' / 'memo'}" in joined
    assert "anon_set_pulled" not in joined  # the case's own evid dir is the workdir, not a hidden set
    assert f"--tmpfs {outside}" in joined  # a did-pane set that is not under ~/did_workdirs
    assert f"--ro-bind /dev/null {note}" in joined
    assert f"--tmpfs {home / 'did' / 'already'}" not in joined  # already inside the hidden did root


@pytest.mark.skipif(not sandbox.enabled(), reason="bubblewrap not available")
def test_sandbox_is_your_terminal_minus_the_case_data(tmp_path):
    """Live: home, tools and config are there; did, the raw folder and other cases are not."""
    home = Path.home()
    did_root = tmp_path / "did"
    (did_root / "p" / "draft").mkdir(parents=True)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.pdf").write_text("pii")
    (did_root / "p" / "draft" / "sources.json").write_text(json.dumps({"sources": [{"path": str(raw / "a.pdf")}]}))
    cases_root = tmp_path / "cases"
    case = cases_root / "c"
    case.mkdir(parents=True)
    (cases_root / "other").mkdir()
    a = sandbox.bwrap_argv(case, did_root=did_root, cases_root=cases_root)
    script = (
        f'ls "{did_root}" | wc -l; ls "{raw}" | wc -l; ls "{cases_root}"; '
        f'[ -d "{home}" ] && echo home; touch "{case}/w" && echo writable; command -v git >/dev/null && echo git'
    )
    r = subprocess.run([*a, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.stdout.split() == ["0", "0", "c", "home", "writable", "git"], r.stderr


# ---------- routes ----------
def test_case_agents_are_buttons(case, monkeypatch):
    """workbench.yml can list the case's own harness buttons; they win by name."""
    import shutil

    from workbench.write import core
    from workbench.write.extras import Extras

    (case / "workbench.yml").write_text(
        'id: "2026-014"\nname: "Tenancy dispute"\n\nagents:\n'
        '  - name: "OpenCode · qw38"\n    command: "ocprofile launch local-qw38"\n    bin: "ocprofile"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    ex = Extras(core._ws(str(case), "", [], False), {})
    try:
        assert ex.case["agents"][0]["name"] == "OpenCode · qw38"
        agents = ex.cfg()["agents"]
        assert agents[0]["name"] == "OpenCode · qw38" and agents[0]["command"] == "ocprofile launch local-qw38"
    finally:
        ex.stop()


@pytest.fixture
def write_server(case, skill_root, monkeypatch):
    from workbench.write import core

    monkeypatch.setenv("WORKBENCH_EVID_URL", "http://127.0.0.1:9/")  # nothing there
    ws = core._ws(str(case), "", [], False)
    core.Handler.ws = ws
    core.Handler.loopback = True
    from workbench.write.extras import Extras

    ex = core.Handler.extras = Extras(ws, {})
    srv = core.Server(("127.0.0.1", 0), core.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    ex.stop()
    core.Handler.extras = None


def call(base, method, path, body=None):
    h = {"X-Treedit": "1"}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    try:
        with urlopen(Request(base + path, data=data, method=method, headers=h), timeout=20) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_routes(write_server, case):
    s, wb = call(write_server, "GET", "/api/wb")
    assert s == 200 and wb["case"]["id"] == "2026-014" and wb["case"]["evidSet"] == "2026-014-anon"
    s, src = call(write_server, "GET", "/api/sources")
    assert s == 200 and "evid did not answer" in src["error"]
    s, sk = call(write_server, "GET", "/api/skills")
    assert {x["name"] for x in sk["skills"]} == {"memo", "did"}
    s, sk = call(write_server, "PUT", "/api/skills", {"name": "memo", "on": False})
    assert not {x["name"]: x for x in sk["skills"]}["memo"]["on"]
    s, f = call(write_server, "GET", "/api/skills/file?name=memo&path=ref.md")
    assert f["text"] == "x\n"
    s, st = call(write_server, "GET", "/api/git/state")
    assert st["repo"] and st["commits"] == 1 and st["pending"] == 2  # workbench.yml new, case.toml removed
    s, c = call(write_server, "POST", "/api/git/commit", {})
    assert c["commit"]["who"] == "agent"
    s, t = call(write_server, "POST", "/api/git/tag", {"label": "draft one"})
    assert s == 200 and t["name"].startswith("state/")
    s, rec = call(write_server, "GET", "/api/git/recent")
    assert rec["commits"][0]["tags"] == [t["name"]]
    s, pv = call(write_server, "GET", "/api/preview?path=../x.typ")
    assert s == 400 and "not a Typst file" in pv["error"]
    s, q = call(write_server, "POST", "/api/queue/rolled", {"target": "claude", "feedback": [1], "items": []})
    assert q["target"] == "claude"
    with urlopen(write_server + "/static/fonts/jetbrains-mono-latin-400-normal.woff2", timeout=5) as r:
        assert r.status == 200
    with urlopen(write_server + "/", timeout=5) as r:
        page = r.read().decode()
    assert '"case": {"id": "2026-014"' in page


def test_preview_runs(write_server, case):
    from workbench.write.preview import find_tinymist

    if not find_tinymist():
        pytest.skip("tinymist not installed")
    s, pv = call(write_server, "GET", "/api/preview?path=memo.typ")
    assert s == 200 and pv["data"].startswith("http://127.0.0.1:") and pv["control"].startswith("ws://")
    with urlopen(pv["data"], timeout=10) as r:
        assert b"<html" in r.read().lower()
    _, again = call(write_server, "GET", "/api/preview?path=memo.typ")
    assert again["data"] == pv["data"]  # reused, not restarted
    _, restarted = call(write_server, "GET", "/api/preview?path=memo.typ&restart=1")
    assert restarted["data"] != pv["data"] and restarted["control"] != pv["control"]
    with urlopen(restarted["data"], timeout=10) as r:
        assert b"<html" in r.read().lower()
    _close_control(restarted["control"])  # tinymist then drops the control listener
    fresh = restarted
    for _ in range(30):
        _, fresh = call(write_server, "GET", "/api/preview?path=memo.typ")
        if fresh.get("data") != restarted["data"]:
            break
        time.sleep(0.1)
    assert fresh["data"] != restarted["data"] and fresh["control"].startswith("ws://")
    with urlopen(fresh["data"], timeout=10) as r:
        assert b"<html" in r.read().lower()


def test_preview_collects_errors_per_compile(monkeypatch):
    """tinymist's error blocks: one compile's blocks together, a later compile replaces them."""
    from workbench.write import preview

    clock = [100.0]
    monkeypatch.setattr(preview.time, "monotonic", lambda: clock[0])
    p = preview.Preview("memo.typ", None)
    p.root = "/w/case/"
    block = ["error: unknown variable: x", "  ┌─ /w/case/memo.typ:4:1", "  │", "4 │ #x", ""]
    for line in ["[INFO] Data plane server listening on: 127.0.0.1:1", *block, *block]:
        assert p._diag(line) == (line not in ("[INFO] Data plane server listening on: 127.0.0.1:1",))
    assert p.errors == ["error: unknown variable: x\n  ┌─ memo.typ:4:1\n  │\n4 │ #x"]  # deduped, root stripped
    clock[0] += 0.2
    for line in ["error: expected expression", "  ┌─ /w/case/memo.typ:9:2", ""]:
        p._diag(line)
    assert len(p.errors) == 2  # the same compile
    clock[0] += 5
    for line in ["error: other", ""]:
        p._diag(line)
    assert p.errors == ["error: other"]  # a later compile starts over


def test_preview_errors_endpoint(write_server, case):
    from workbench.write.preview import find_tinymist

    if not find_tinymist():
        pytest.skip("tinymist not installed")
    (case / "bad.typ").write_text("= Bad\n\n#no-such-thing\n", encoding="utf-8")
    s, pv = call(write_server, "GET", "/api/preview?path=bad.typ")
    assert s == 200
    errors = []
    for _ in range(50):
        _, r = call(write_server, "GET", "/api/preview/errors?path=bad.typ")
        errors = r["errors"]
        if errors:
            break
        time.sleep(0.1)
    assert errors and errors[0].startswith("error: unknown variable") and "┌─ bad.typ:3:" in errors[0]
    assert call(write_server, "GET", "/api/preview/errors?path=memo.typ")[1] == {"errors": []}


def _readn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


def _close_control(url: str) -> None:
    """What the reload button used to do: a live control socket, then a Close frame.

    tinymist answers the handshake with a text message. Closing after that frame is what
    logs `unsupported close message` and drops the listener.
    """
    host, port = url.removeprefix("ws://").rstrip("/").split(":")
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, int(port)), 2)
    try:
        sock.sendall(
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1:9\r\n\r\n".encode()
        )
        sock.settimeout(2)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(256)
        # one server frame (it greets the editor) so the close is read as a websocket message
        hdr = _readn(sock, 2)
        ln = hdr[1] & 127
        if ln == 126:
            ln = int.from_bytes(_readn(sock, 2), "big")
        if ln:
            _readn(sock, ln)
        mask, payload = os.urandom(4), bytes([0x03, 0xE8])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes([0x88, 0x80 | len(payload)]) + mask + masked)
        time.sleep(0.2)  # let tinymist read the frame before the TCP close
    finally:
        sock.close()


def test_gather_writes_one_merged_store(case, monkeypatch):
    """One refs/refs.yml: each set gathered to a temp file, then evid refs merge."""
    calls = []

    def fake_run(argv, timeout=600):
        calls.append(argv)
        out = Path(argv[argv.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("laws:main:\n  title: x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sources, "evid_bin", lambda: "evid")
    monkeypatch.setattr(sources, "_run", fake_run)
    sources.choose(case, "global", "laws", True)
    sources.choose(case, "local", "notes", True)
    r = sources.gather(case)
    assert r == {"ref": "refs/refs.yml", "errors": [], "stray": []}
    assert (case / "refs" / "refs.yml").is_file()
    merge = next(c for c in calls if "merge" in c)
    assert merge[:3] == ["evid", "refs", "merge"] and "-o" in merge
    assert sum("set" in c and "gather" in c for c in calls) == 2  # one per linked set


def test_pdf_route_refuses_non_pdf(write_server):
    """The PDF viewer route serves only .pdf files in the workdir."""
    assert call(write_server, "GET", "/api/pdf?path=memo.typ")[0] == 415
    assert call(write_server, "GET", "/api/pdf/info?path=memo.typ")[0] == 415


def test_refs_has_one_place(case, tmp_path, monkeypatch, capsys):
    """refs/refs.yml is the case's citation file; any other refs.yml is listed as stray."""
    from workbench import cli, paths

    for rel in ("refs.yml", "memo/refs.yml", "refs/refs.yml", "evid/plain/x/refs.yml", ".cache/refs.yml", "refs/old.yml"):
        (case / rel).parent.mkdir(parents=True, exist_ok=True)
        (case / rel).write_text("k: {}\n", encoding="utf-8")
    assert paths.REFS == "refs/refs.yml"
    assert paths.stray_refs(case) == ["memo/refs.yml", "refs.yml"]

    def fake_run(argv, timeout=600):
        out = Path(argv[argv.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("laws:main:\n  title: x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sources, "evid_bin", lambda: "evid")
    monkeypatch.setattr(sources, "_run", fake_run)
    sources.choose(case, "global", "laws", True)
    assert sources.gather(case)["stray"] == ["memo/refs.yml", "refs.yml"]
    # the agent command finds the case root from any folder inside it
    cli.main(["gather", "-C", str(case / "memo")])
    out, err = capsys.readouterr()
    assert out.strip() == str(case / "refs" / "refs.yml")
    assert "stray  memo/refs.yml" in err and "stray  refs.yml" in err
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(SystemExit, match="not inside a case"):
        cli.main(["gather", "-C", str(elsewhere)])


def test_case_instructions_name_the_one_refs_file():
    """A new case's AGENTS.md and main.typ say where refs go, so no agent has to look."""
    from workbench import cases

    agents = cases.AGENTS_MD.format(id="x", name="y")
    assert "exactly one citation file, `refs/refs.yml`" in agents
    assert 'yaml("/refs/refs.yml")' in agents and "jura gather" in agents and "not read" in agents
    main = cases.main_source("x", "y")
    assert "refs/*.yml" not in main and 'yaml("/refs/refs.yml")' in main
