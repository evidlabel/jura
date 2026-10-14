"""One settings file for a case workdir: ``workbench.yml``.

It holds the case id and name, the linked evid and did sets, the directories
the Skillset reads, and the skills that are off. ``refs/refs.yml`` is the gathered
citation file (:data:`workbench.paths.REFS`), and the files under ``evid/`` are evid's own store. Neither of
those belongs in this file.

``workbench.yml`` wins when it is present. Until the next write, an older case
still loads from ``case.toml``, ``sources.yml`` (or ``evid.yml``) and ``skills.yml``.
A write rewrites ``workbench.yml`` and removes those four.

Only the narrow shape written above is understood — top-level ``id``/``name``/
``evid_set``/``did_project`` scalars, ``evid.global``/``evid.local`` lists, ``did``
entries with a ``set`` and a ``mode``, ``skills.dirs``/``skills.off`` lists, and
``agents`` entries — a bare name (used as the command too, e.g. ``- grok``) or a
map with a ``name`` and an optional ``command``/``bin``/``title`` (the case's own
harness buttons; a case that names none uses the catalogue).
``render`` quotes scalars and directory entries; list entries written unquoted are
plain identifiers (a slug, a skill name) and so must not contain ``#`` or a line
break, which the reader strips. This is a hand-rolled subset, not a YAML engine:
evid and did are the tools that read the real documents.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "workbench.yml"
LEGACY_NAMES = ("case.toml", "sources.yml", "skills.yml", "evid.yml")
HEADER = "# workbench case\n"
MODES = ("placeholder", "faker", "anon", "pseudo")
DEFAULT_MODE = "placeholder"
DEFAULT_SKILLS_OFF = ["did"]
# Used when a new case is written. A case that omits ``skills.dirs`` still
# reads skills.toml (or ~/.config/workbench/skills.toml).
DEFAULT_SKILL_DIRS = [
    "~/.claude/skills",
    "~/.grok/skills",
    "~/.hermes/skills",
    "~/.codex/skills",
]


@dataclass
class CaseConfig:
    id: str
    name: str
    evid_set: str = ""
    did_project: str = ""
    evid: dict = field(default_factory=lambda: {"global": [], "local": []})
    did: list = field(default_factory=list)
    skills_off: list[str] | None = None  # None: the skills section was absent
    skills_dirs: list[str] | None = None  # None: no dirs key; the catalogue applies
    agents: list | None = None  # None: no agents key; the shipped catalogue applies


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        body = value[1:-1]
        if value[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    return value


def _yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _inline(rest: str) -> list[str] | None:
    rest = rest.strip()
    if rest.startswith("[") and rest.endswith("]"):
        return [_unquote(part) for part in rest[1:-1].split(",") if part.strip()]
    return None


def _code(raw: str) -> str:
    return raw.split("#", 1)[0].rstrip()


_AGENT_KEYS = ("name", "command", "bin", "title")


def _agent_field(row: dict, text: str) -> None:
    name, _, rest = text.partition(":")
    if name.strip() in _AGENT_KEYS:
        row[name.strip()] = _unquote(rest)


def empty() -> CaseConfig:
    return CaseConfig(id="", name="")


def parse(text: str) -> CaseConfig:
    """``workbench.yml``, or a ``sources.yml`` which is the same shape without identity."""
    cfg = empty()
    section = None
    pool = None
    current = None
    skill_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        line = _code(raw)
        if not line.strip():
            continue
        item = line.strip()
        if indent == 0 and not item.startswith("-"):
            name, _, rest = item.partition(":")
            key, rest = name.strip(), rest.strip()
            section, pool, current, skill_key = None, None, None, None
            if key in ("id", "name", "evid_set", "did_project"):
                setattr(cfg, key, _unquote(rest))
            elif key in ("evid", "did", "skills", "agents"):
                section = key
                if key == "agents" and cfg.agents is None:
                    cfg.agents = []
            continue
        if section == "evid" and not item.startswith("-"):
            name, _, rest = item.partition(":")
            pool = name.strip() if name.strip() in ("global", "local") else None
            if pool is None:
                continue
            listed = _inline(rest)
            if listed is None:
                continue
            for slug in listed:
                if slug and slug not in cfg.evid[pool]:
                    cfg.evid[pool].append(slug)
            continue
        if section == "evid" and pool and item.startswith("-"):
            slug = _unquote(item[1:])
            if slug and slug not in cfg.evid[pool]:
                cfg.evid[pool].append(slug)
            continue
        if section == "did" and item.startswith("-"):
            body = item[1:].strip()
            if body.startswith("set:"):
                slug = _unquote(body.split(":", 1)[1])
                if slug:
                    current = {"set": slug, "mode": DEFAULT_MODE}
                    cfg.did.append(current)
            continue
        if section == "did" and current is not None and item.startswith("mode:"):
            mode = _unquote(item.split(":", 1)[1])
            if mode in MODES:
                current["mode"] = mode
            continue
        if section == "agents":
            body = item[1:].strip() if item.startswith("-") else item
            if item.startswith("-"):
                current = {}
                cfg.agents.append(current)
            if current is not None and body:
                if ":" in body:
                    _agent_field(current, body)
                else:  # a bare name is the command too: `- grok`
                    current["name"] = current["command"] = _unquote(body)
            continue
        if section == "skills" and not item.startswith("-"):
            name, _, rest = item.partition(":")
            skill_key = name.strip()
            if skill_key == "off":
                if cfg.skills_off is None:
                    cfg.skills_off = []
                listed = _inline(rest)
                if listed is not None:
                    cfg.skills_off = [slug for slug in listed if slug]
            elif skill_key == "dirs":
                if cfg.skills_dirs is None:
                    cfg.skills_dirs = []
                listed = _inline(rest)
                if listed is not None:
                    cfg.skills_dirs = [path for path in listed if path]
            else:
                skill_key = None
            continue
        if section == "skills" and skill_key == "off" and item.startswith("-"):
            if cfg.skills_off is None:
                cfg.skills_off = []
            slug = _unquote(item[1:])
            if slug and slug not in cfg.skills_off:
                cfg.skills_off.append(slug)
            continue
        if section == "skills" and skill_key == "dirs" and item.startswith("-"):
            if cfg.skills_dirs is None:
                cfg.skills_dirs = []
            path = _unquote(item[1:])
            if path and path not in cfg.skills_dirs:
                cfg.skills_dirs.append(path)
    return cfg


def _read_evid_yml(workdir: Path) -> dict:
    """{"case": [...], "reference": [...]} from an old evid.yml. Missing file: both empty."""
    out: dict = {"case": [], "reference": []}
    path = workdir / "evid.yml"
    if not path.is_file():
        return out
    key = None
    for raw in path.read_text("utf-8").splitlines():
        line = _code(raw)
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            name, _, rest = line.partition(":")
            key = name.strip() if name.strip() in out else None
            listed = _inline(rest) if key else None
            if key and listed is not None:
                out[key] = [slug for slug in listed if slug]
            continue
        item = line.strip()
        if key and item.startswith("-"):
            slug = _unquote(item[1:])
            if slug and slug not in out[key]:
                out[key].append(slug)
    return out


def _read_skills_yml(path: Path) -> list[str]:
    """The ``off`` list. A file with no ``off`` key is an empty list, and that list is authoritative."""
    cfg = parse("skills:\n" + "".join(f"  {line}\n" for line in path.read_text("utf-8").splitlines()))
    return list(cfg.skills_off or [])


def _identity(cfg: CaseConfig, workdir: Path) -> None:
    if not cfg.id:
        cfg.id = workdir.name
    if not cfg.name:
        cfg.name = cfg.id


def read_config(workdir: Path) -> CaseConfig:
    """Prefer ``workbench.yml``. Otherwise assemble the older files. Does not write."""
    path = workdir / CONFIG_NAME
    if path.is_file():
        cfg = parse(path.read_text("utf-8"))
        _identity(cfg, workdir)
        return cfg
    cfg = empty()
    toml = workdir / "case.toml"
    if toml.is_file():
        data = tomllib.loads(toml.read_text("utf-8"))
        cfg.id = str(data.get("id") or workdir.name)
        cfg.name = str(data.get("name") or cfg.id)
        cfg.evid_set = str(data.get("evid_set") or "")
        cfg.did_project = str(data.get("did_project") or "")
    _identity(cfg, workdir)
    sources = workdir / "sources.yml"
    if sources.is_file():
        sel = parse(sources.read_text("utf-8"))
        cfg.evid = sel.evid
        cfg.did = sel.did
    else:
        old = _read_evid_yml(workdir)
        slugs = list(old["case"])
        for slug in old["reference"]:
            if slug not in slugs:
                slugs.append(slug)
        cfg.evid = {"global": slugs, "local": []}
    skills = workdir / "skills.yml"
    if skills.is_file():
        cfg.skills_off = _read_skills_yml(skills)
    return cfg


def _block(name: str, slugs: list[str], indent: str, *, quote: bool = False) -> str:
    if not slugs:
        return f"{indent}{name}: []\n"
    lines = "".join(f"{indent}  - {_yaml(slug) if quote else slug}\n" for slug in slugs)
    return f"{indent}{name}:\n{lines}"


def render(cfg: CaseConfig) -> str:
    parts = [HEADER, f"id: {_yaml(cfg.id)}\n", f"name: {_yaml(cfg.name)}\n"]
    if cfg.evid_set:
        parts.append(f"evid_set: {_yaml(cfg.evid_set)}\n")
    if cfg.did_project:
        parts.append(f"did_project: {_yaml(cfg.did_project)}\n")
    evid = cfg.evid or {}
    parts.append("\nevid:\n")
    parts.append(_block("global", list(evid.get("global") or []), "  "))
    parts.append(_block("local", list(evid.get("local") or []), "  "))
    did = cfg.did or []
    if not did:
        parts.append("did: []\n")
    else:
        parts.append("did:\n")
        for entry in did:
            mode = entry.get("mode") if entry.get("mode") in MODES else DEFAULT_MODE
            parts.append(f"  - set: {entry['set']}\n    mode: {mode}\n")
    off = list(DEFAULT_SKILLS_OFF) if cfg.skills_off is None else list(cfg.skills_off)
    parts.append("\nskills:\n")
    if cfg.skills_dirs is not None:
        parts.append(_block("dirs", list(cfg.skills_dirs), "  ", quote=True))
    parts.append(_block("off", off, "  "))
    parts.append(_agents_block(cfg))
    return "".join(parts)


def _agents_block(cfg: CaseConfig) -> str:
    """The case's own agent buttons. Omitted when the case names none (the catalogue applies)."""
    if cfg.agents is None:
        return ""
    if not cfg.agents:
        return "\nagents: []\n"
    parts = ["\nagents:\n"]
    for row in cfg.agents:
        parts.append(f"  - name: {_yaml(str(row.get('name') or ''))}\n")
        for key in ("command", "bin", "title"):
            value = str(row.get(key) or "")
            if value:
                parts.append(f"    {key}: {_yaml(value)}\n")
    return "".join(parts)


def write_config(workdir: Path, cfg: CaseConfig) -> None:
    """Write ``workbench.yml``, then remove the older settings files."""
    tmp = workdir / ".workbench.yml.tmp"
    tmp.write_text(render(cfg), "utf-8")
    os.replace(tmp, workdir / CONFIG_NAME)
    for name in LEGACY_NAMES:
        path = workdir / name
        if path.is_file():
            path.unlink()
