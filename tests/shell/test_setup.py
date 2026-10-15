"""``jura setup``'s plan, and the case templates. Nothing here installs or downloads."""

import shutil
import subprocess

import pytest

from workbench import cases, setup, templates


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CASES", str(tmp_path / "cases"))
    monkeypatch.setenv("WORKBENCH_DID", str(tmp_path / "did"))
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "t")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@t")
    return tmp_path


# ---------- setup ----------
def test_plan_nothing_missing():
    assert setup.plan({}, uv=True, manager="apt-get", arch="x86_64", tinymist=False) == []


def test_plan_everything_missing_on_apt():
    missing = {n: f"{n} is not installed" for n in ("evid", "did", "typst", "git", "bwrap")}
    steps = setup.plan(missing, uv=True, manager="apt-get", arch="x86_64", languages=("da", "en"))
    assert [s.tool for s in steps] == ["evid", "did", "git bubblewrap", "typst", "tinymist", "did models"]
    evid, did, system, typst, tinymist, models = steps
    assert evid.argv[:4] == ("uv", "tool", "install", "--upgrade") and "evidlabel/evid" in evid.argv[-1]
    assert "3.12" in did.argv and did.argv[-1].startswith("did[gui,models] @ git+")
    assert system.sudo and system.argv == ("sudo", "apt-get", "install", "-y", "git", "bubblewrap")
    assert not any(s.sudo for s in (evid, did, typst, tinymist, models))
    assert typst.url.endswith(f"v{setup.TYPST}/typst-x86_64-unknown-linux-musl.tar.xz")
    assert tinymist.url.endswith("tinymist-linux-x64")
    assert models.argv == ("did", "models", "da", "en")


def test_plan_offers_pdftotext_with_sudo():
    steps = setup.plan({}, uv=True, manager="pacman", arch="x86_64", tinymist=False, pdftotext=True)
    assert [(s.tool, s.sudo, s.argv[-1]) for s in steps] == [("poppler", True, "poppler")]
    assert setup.plan({}, uv=True, manager="", arch="x86_64", tinymist=False, pdftotext=True) == []


@pytest.mark.parametrize("manager", ["dnf", "pacman", "zypper"])
def test_plan_other_managers_use_sudo(manager):
    (step,) = setup.plan({"bwrap": "x"}, uv=True, manager=manager, arch="x86_64", tinymist=False)
    assert step.sudo and step.argv[:2] == ("sudo", manager) and step.argv[-1] == "bubblewrap"


def test_plan_without_uv_manager_or_release_only_advises():
    steps = setup.plan({"evid": "x", "bwrap": "x", "typst": "x"}, uv=False, manager="", arch="riscv64")
    assert [s.tool for s in steps] == ["evid", "bubblewrap", "typst"]
    assert not any(s.runnable for s in steps)
    assert "uv" in steps[0].what and "riscv64" in steps[2].what


def test_plan_arm():
    typst, tinymist = setup.plan({"typst": "x"}, uv=True, manager="", arch="aarch64")
    assert "typst-aarch64-unknown-linux-musl" in typst.url and tinymist.url.endswith("linux-arm64")


def test_setup_yes_never_runs_sudo(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(setup, "_missing", lambda: {"bwrap": "bwrap is not installed"})
    monkeypatch.setattr(setup, "package_manager", lambda: "apt-get")
    monkeypatch.setattr(setup.requires, "check", lambda n: type("T", (), {"problem": "x"})())  # did missing: no models step
    monkeypatch.setattr(setup, "_do", ran.append)
    monkeypatch.setattr("workbench.write.preview.find_tinymist", lambda: "/x/tinymist")
    setup.run(yes=True)
    assert ran == [] and "needs sudo: not run under --yes" in capsys.readouterr().out


# ---------- templates ----------
def test_blank_template_is_the_default_map():
    assert cases.main_source("a", "A") == cases.main_source("a", "A", "blank")
    assert "_Replace this paragraph" in cases.main_source("a", "A") and "To collect" not in cases.main_source("a", "A")


@pytest.mark.parametrize("key", [k for k in templates.TEMPLATES if k != "blank"])
def test_template_case(key):
    c = cases.create("2026-001", template=key)
    t = templates.TEMPLATES[key]
    assert c.name == t.name
    src = (c.workdir / "main.typ").read_text("utf-8")
    assert t.question in src and "To collect:" in src and all(x in src for x in t.collect + t.open)
    assert "__" not in src
    log = subprocess.run(["git", "-C", str(c.workdir), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.strip() == f"workbench: new case 2026-001 ({key})"


@pytest.mark.skipif(not shutil.which("typst"), reason="typst is not installed")
@pytest.mark.parametrize("key", list(templates.TEMPLATES))
def test_template_compiles(key, tmp_path):
    c = cases.create("t-" + key, template=key)
    r = subprocess.run(["typst", "compile", "--root", str(c.workdir), str(c.workdir / "main.typ"), str(tmp_path / "m.pdf")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unknown_template_is_rejected():
    with pytest.raises(ValueError, match="unknown template"):
        cases.create("x", template="probate")
    assert not (cases.cases_root() / "x").exists()


def test_name_falls_back_to_id_for_blank():
    assert cases.create("2026-002").name == "2026-002"
    assert cases.create("2026-003", "Mine", "rent").name == "Mine"
