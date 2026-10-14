"""Where things go in a case: the layout a new case gets, the check, the out/ build and refs/extra."""

import shutil
import subprocess
from pathlib import Path

import pytest

from workbench import cases, cli, layout
from workbench.write import sources


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CASES", str(tmp_path / "cases"))
    monkeypatch.setenv("WORKBENCH_DID", str(tmp_path / "did"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    monkeypatch.setenv("WORKBENCH_EVID_BIN", "/nonexistent/evid")
    return tmp_path


def _put(root: Path, rel: str, text: str = "x\n") -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def test_new_case_carries_the_layout():
    c = cases.create("2026-014", "Tenancy dispute").workdir
    agents = (c / "AGENTS.md").read_text()
    assert "## Where things go" in agents and "jura layout" in agents and "jura compile" in agents
    for place in ("docs/<topic>/<name>.typ", "refs/refs.yml", "refs/extra/<name>.yml", "out/<path>.pdf", "evid/"):
        assert place in agents
    ignored = (c / ".gitignore").read_text().splitlines()
    assert ignored[0] == "out/" and {".labels.lock", "vecdb/"} <= set(ignored)  # evid's lock and index
    assert layout.misplaced(c) == []  # a fresh case is in order


def test_misplaced_names_the_fix():
    c = cases.create("t", "t").workdir
    for rel in ("docs/risk/notat.typ", "docs/risk/fig.png", "refs/refs.yml", "refs/extra/fal.yml", "evid/plain/a.pdf",
                "memo.typ", "docs/risk/notat.pdf", "notat.pdf", "docs/risk/refs.yml", "refs/old.yml", "out/docs/x.pdf"):
        _put(c, rel)
    subprocess.run(["git", "-C", str(c), "add", "-f", "docs/risk/notat.pdf"], check=True)
    wrong = {m.path: m for m in layout.misplaced(c)}
    assert set(wrong) == {"memo.typ", "docs/risk/notat.pdf", "notat.pdf", "docs/risk/refs.yml", "refs/old.yml"}
    assert wrong["memo.typ"].fix == "git mv -- memo.typ docs/memo/memo.typ"
    assert wrong["docs/risk/notat.pdf"].fix == "git rm -q -- docs/risk/notat.pdf"  # tracked
    assert wrong["notat.pdf"].fix == "rm -- notat.pdf"  # untracked
    assert "refs/extra/" in wrong["docs/risk/refs.yml"].fix
    assert wrong["refs/old.yml"].fix == "git mv -- refs/old.yml refs/extra/old.yml"


def test_layout_command(capsys):
    c = cases.create("t", "t").workdir
    cli.main(["layout", "-C", str(c)])
    out = capsys.readouterr().out
    assert "## Where things go" in out and "Everything is in its place." in out
    _put(c, "docs/a/a.pdf")
    with pytest.raises(SystemExit) as e:
        cli.main(["layout", "-C", str(c / "docs")])
    assert e.value.code == 1 and "docs/a/a.pdf  (a PDF outside out/)" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("typst"), reason="typst is not installed")
def test_compile_writes_out_and_ignores_it(capsys):
    c = cases.create("t", "t").workdir
    _put(c, "docs/risk/notat.typ", "= Notat\n")
    (c / ".gitignore").write_text("build/\n")  # a case made before out/
    cli.main(["compile", str(c / "docs/risk/notat.typ")])
    assert capsys.readouterr().out.strip() == str(c / "out/docs/risk/notat.pdf")
    assert (c / "out/docs/risk/notat.pdf").is_file()
    assert "out/" in (c / ".gitignore").read_text().splitlines()
    assert layout.misplaced(c) == []
    _put(c, "docs/bad.typ", "#nope\n")
    with pytest.raises(SystemExit, match="unknown variable"):
        cli.main(["compile", str(c / "docs/bad.typ")])


def test_gather_merges_refs_extra(monkeypatch):
    c = cases.create("t", "t").workdir
    _put(c, "refs/extra/fal.yml", "fal:main:\n  title: x\n")
    merged = []

    def fake_run(argv, timeout=600):
        if "merge" in argv:
            merged.append(argv)
        out = Path(argv[argv.index("-o") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("k:\n  title: x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sources, "evid_bin", lambda: "evid")
    monkeypatch.setattr(sources, "_run", fake_run)
    assert sources.gather(c)["ref"] == "refs/refs.yml"  # no linked set: the extras alone
    assert merged[-1][-1] == str(c / "refs/extra/fal.yml")
    sources.choose(c, "global", "laws", True)
    sources.gather(c)
    inputs = merged[-1][merged[-1].index("-o") + 2 :]
    assert inputs[0].endswith("-laws.yml") and inputs[-1] == str(c / "refs/extra/fal.yml")  # the sets first
