"""The tools check: evid and did have a minimum version; doctor and the window say what is wrong."""

import pytest

from workbench import cli, requires


def _stub(tmp_path, name: str, out: str) -> str:
    exe = tmp_path / name
    exe.write_text(f"#!/bin/sh\necho '{out}'\n")
    exe.chmod(0o755)
    return str(exe)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    def put(evid: str, did: str):
        monkeypatch.setenv("WORKBENCH_EVID_BIN", _stub(tmp_path, "evid", evid))
        monkeypatch.setenv("WORKBENCH_DID_BIN", _stub(tmp_path, "did", did))

    return put


def test_new_enough_is_quiet(tools):
    tools("0.8.0", "did 0.3.10")  # 0.3.10 > 0.3.2: compared as numbers
    assert requires.check("evid").problem == "" and requires.check("evid").version == "0.8.0"
    assert requires.check("did").problem == ""


def test_too_old_and_missing_are_named(tools, monkeypatch):
    tools("0.7.1", "garbage")
    assert requires.check("evid").problem == "evid 0.7.1 is older than this workbench needs (0.8.0 or newer)"
    assert "did not print a version" in requires.check("did").problem
    monkeypatch.setenv("WORKBENCH_EVID_BIN", "/nonexistent/evid")  # set but not a command: not the one on PATH
    assert requires.check("evid").problem == "evid 0.8.0 or newer is not installed (not on PATH)"
    assert requires.check("evid").problem in requires.problems()


def test_doctor_exits_1_on_a_problem(tools, capsys):
    tools("0.7.1", "0.3.2")
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor"])
    out = capsys.readouterr().out
    assert e.value.code == 1
    assert "!!  evid" in out and "evid 0.7.1 is older" in out and "ok  did" in out


def test_state_carries_the_problems(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_CASES", str(tmp_path / "cases"))
    monkeypatch.setenv("WORKBENCH_DID", str(tmp_path / "did"))
    from workbench.server import Workbench

    wb = Workbench()
    assert wb.state()["problems"] is None  # not checked yet: the page asks again
    wb.problems = ["evid 0.7.1 is older than this workbench needs (0.8.0 or newer)"]
    assert wb.state()["problems"] == wb.problems


def test_source_tools_are_optional_but_named(tools, tmp_path, monkeypatch, capsys):
    tools("0.8.0", "0.3.2")
    monkeypatch.setenv("WORKBENCH_HUDOC_BIN", _stub(tmp_path, "hudoc", "0.1.3"))
    broken = tmp_path / "domdb"  # a stale env: a traceback with a version-like path, exit 1
    broken.write_text("#!/bin/sh\necho 'site-packages/treeparse-0.3.4/x.py ImportError' >&2\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("WORKBENCH_DOMDB_BIN", str(broken))
    monkeypatch.setenv("WORKBENCH_LAWCITE_BIN", "/nonexistent/lawcite")

    got = requires.source_problems()
    assert got["hudoc"] == "hudoc 0.1.3 is older than this workbench needs (0.1.4 or newer)"
    assert "did not print a version" in got["domdb"]
    assert "not installed" in got["lawcite"]
    assert requires.problems() == []  # the window only blocks on required tools

    cli.main(["doctor"])  # optional: no exit 1
    out = capsys.readouterr().out
    assert "!!  hudoc" in out and "hudoc 0.1.3 is older" in out
    assert "!!  domdb" in out and "--  lawcite" in out and "no statute lookup" in out
