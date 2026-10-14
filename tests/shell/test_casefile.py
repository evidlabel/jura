"""workbench.yml: the hand-rolled reader and writer agree on the shape they own."""

from workbench import casefile


def test_render_then_parse_roundtrips():
    cfg = casefile.CaseConfig(
        id="2026-014",
        name='Tenancy "dispute" \\ odd',
        evid_set="2026-014-anon",
        did_project="~/did/2026-014",
        evid={"global": ["laws", "2026-014-anon"], "local": ["notes"]},
        did=[{"set": "interview", "mode": "faker"}, {"set": "hearing", "mode": "pseudo"}],
        skills_off=["did", "memo"],
        skills_dirs=["~/.claude/skills", "/opt/extra skills"],
        agents=[
            {"name": "OpenCode · qw38", "command": "ocprofile launch local-qw38", "bin": "ocprofile", "title": "GB10"},
            {"name": "Claude", "command": "claude"},
        ],
    )
    back = casefile.parse(casefile.render(cfg))
    assert back.id == cfg.id
    assert back.name == cfg.name
    assert back.evid_set == cfg.evid_set
    assert back.did_project == cfg.did_project
    assert back.evid == cfg.evid
    assert back.did == cfg.did
    assert back.skills_off == cfg.skills_off
    assert back.skills_dirs == cfg.skills_dirs
    assert back.agents == cfg.agents


def test_empty_sections_roundtrip():
    back = casefile.parse(casefile.render(casefile.CaseConfig(id="a", name="a")))
    assert back.evid == {"global": [], "local": []}
    assert back.did == []
    assert back.skills_off == list(casefile.DEFAULT_SKILLS_OFF)  # absent section -> the default
    assert back.skills_dirs is None  # no dirs key -> the catalogue applies
    assert back.agents is None  # no agents key -> the shipped catalogue applies


def test_empty_agents_section_is_authoritative():
    back = casefile.parse(casefile.render(casefile.CaseConfig(id="a", name="a", agents=[])))
    assert back.agents == []  # an empty list, not "absent"


def test_bare_agent_name_is_the_command():
    back = casefile.parse('id: "a"\nname: "a"\n\nagents:\n  - grok\n  - name: "OpenCode · qw38"\n    command: "ocprofile launch local-qw38"\n')
    assert back.agents == [
        {"name": "grok", "command": "grok"},
        {"name": "OpenCode · qw38", "command": "ocprofile launch local-qw38"},
    ]
