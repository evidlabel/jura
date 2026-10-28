"""The harness catalogue: shipped names, and buttons only for programs on PATH."""

import shutil
from importlib import resources

from workbench import agents


def test_shipped_names():
    rows = agents.parse(agents.shipped_text())
    assert [r["name"] for r in rows] == ["OpenCode", "Hermes", "Codex", "Claude", "Grok", "shell"]
    grok = next(r for r in rows if r["name"] == "Grok")
    assert grok["command"] == "grok" and grok["title"] == "Grok Build"
    hermes = next(r for r in rows if r["name"] == "Hermes")
    assert hermes["command"] == "hermes chat" and hermes["bin"] == "hermes"
    assert (resources.files("workbench") / "agents.toml").is_file()


def test_available_keeps_installed_and_the_shell(monkeypatch, tmp_path):
    cat = tmp_path / "agents.toml"
    cat.write_text(
        '[[agent]]\nname = "Claude"\ncommand = "claude"\nbin = "claude"\n\n'
        '[[agent]]\nname = "Codex"\ncommand = "codex"\nbin = "codex"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)
    assert agents.available() == [
        {"name": "Claude", "command": "claude", "title": ""},
        {"name": "shell", "command": "", "title": ""},
    ]


def test_bad_toml_is_the_shell_only(monkeypatch, tmp_path):
    cat = tmp_path / "agents.toml"
    cat.write_text("[[agent]\n", encoding="utf-8")
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    assert agents.available() == [{"name": "shell", "command": "", "title": ""}]


def test_user_file_replaces_the_shipped_list(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKBENCH_AGENTS", raising=False)
    cfg = tmp_path / "config" / "workbench"
    cfg.mkdir(parents=True)
    (cfg / "agents.toml").write_text(
        '[[agent]]\nname = "Only"\ncommand = "only"\nbin = "only"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/only" if name == "only" else None)
    assert [r["name"] for r in agents.available()] == ["Only", "shell"]


def test_missing_override_uses_the_shipped_names(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKBENCH_AGENTS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent"))
    assert [r["name"] for r in agents.catalogue()] == ["OpenCode", "Hermes", "Codex", "Claude", "Grok", "shell"]


def test_env_config_is_folded_into_the_command(monkeypatch, tmp_path):
    """One harness, two configs. `env` reaches that command only, `~` expanded and quoted."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cat = tmp_path / "agents.toml"
    cat.write_text(
        '[[agent]]\nname = "OpenCode · local"\ncommand = "opencode"\nbin = "opencode"\n'
        'env = { OPENCODE_CONFIG = "~/p q/local.json" }\n\n'
        '[[agent]]\nname = "OpenCode · global"\ncommand = "opencode"\nbin = "opencode"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/opencode" if name == "opencode" else None)
    local, global_, shell = agents.available()
    assert local["command"] == f"OPENCODE_CONFIG='{tmp_path}/p q/local.json' opencode"
    assert global_["command"] == "opencode"  # no env: untouched
    assert shell["command"] == "" and "env" not in shell


def test_parse_reads_an_env_table():
    rows = agents.parse('[[agent]]\nname = "x"\ncommand = "x"\nenv = { A = "1", B = 2 }\n')
    assert rows[0]["env"] == {"A": "1", "B": "2"}
    assert agents.parse('[[agent]]\nname = "y"\ncommand = "y"\n')[0]["env"] == {}


def test_case_rows_come_first_and_replace_by_name(monkeypatch, tmp_path):
    cat = tmp_path / "agents.toml"
    cat.write_text(
        '[[agent]]\nname = "Claude"\ncommand = "claude"\nbin = "claude"\n'
        '[[agent]]\nname = "Grok"\ncommand = "grok"\nbin = "grok"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    extra = [
        {"name": "Claude", "command": "ocprofile launch local-qw38", "bin": "ocprofile"},
        {"name": "OpenCode · qw38", "command": "opencode", "bin": "opencode"},
    ]
    rows = agents.available(extra)
    assert [r["name"] for r in rows] == ["Claude", "OpenCode · qw38", "Grok", "shell"]
    assert rows[0]["command"] == "ocprofile launch local-qw38"  # the case's row won


def test_case_names_stay_greyed_when_missing(monkeypatch, tmp_path):
    """A new case's bare names take the catalogue rows; one not installed stays, marked missing."""
    cat = tmp_path / "agents.toml"
    cat.write_text(
        '[[agent]]\nname = "Claude"\ncommand = "claude"\n'
        '[[agent]]\nname = "Grok"\ncommand = "grok"\ntitle = "Grok Build"\n'
        '[[agent]]\nname = "Hermes"\ncommand = "hermes chat"\nbin = "hermes"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if name == "claude" else None)
    rows = agents.available([{"name": n, "command": n} for n in ("claude", "grok", "codex")])
    assert rows == [
        {"name": "Claude", "command": "claude", "title": ""},  # the catalogue's name, not doubled below
        {"name": "Grok", "command": "grok", "title": "Grok Build", "missing": True, "bin": "grok"},
        {"name": "codex", "command": "codex", "title": "", "missing": True, "bin": "codex"},  # not in the catalogue
        {"name": "shell", "command": "", "title": ""},
    ]  # Hermes: a catalogue row that is not installed is left out


def test_a_name_only_case_row_is_the_catalogue_row(monkeypatch, tmp_path):
    """`- name: Claude` in workbench.yml means the catalogue's Claude, not a shell labelled Claude."""
    cat = tmp_path / "agents.toml"
    cat.write_text(
        '[[agent]]\nname = "Claude"\ncommand = "claude"\n'
        '[[agent]]\nname = "OpenCode · local"\ncommand = "opencode"\nenv = { OPENCODE_CONFIG = "/p.json" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKBENCH_AGENTS", str(cat))
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    rows = agents.available([{"name": "Claude"}, {"name": "opencode · LOCAL"}])
    assert [(r["name"], r["command"]) for r in rows] == [
        ("Claude", "claude"),
        ("OpenCode · local", "OPENCODE_CONFIG=/p.json opencode"),
        ("shell", ""),
    ]
