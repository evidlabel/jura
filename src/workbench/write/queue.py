"""The Write tab's Queue: the user's new edits and annotations since the last roll.

The page builds the queue from what the workspace already tracks (saves, unsaved drafts,
annotations, feedback). This keeps the one thing it cannot know: what the last roll sent —
when, to which agent, the annotations as they were, and the feedback ids — so the next queue
holds only what is new. Kept per workdir in the workbench state dir, outside the case, with the
case's identity (its first commit): a new case at the same path starts with an empty record.

It also keeps the roll's **plan**: the tasks the top-level agent split the roll into, each with
the effort it needs and the subagent that took it. The agent records the split (``jura write
queue plan``) and closes each task with ``done`` or ``drop``; the Queue shows it beneath the
items, so the human can see what the harness is doing in parallel. A new roll clears the plan:
it described the batch before it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from workbench import paths

EFFORTS = ("cheap", "standard", "deep")
STATUSES = ("open", "running", "done", "dropped")
PLAN_MAX = 50  # tasks one roll may be split into
ITEM_MAX = 200


def state_file(root: Path) -> Path:
    return paths.state_dir() / f"{hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]}-queue.json"


def case_id(root: Path) -> str:
    """The case's first commit, or "" when it has none (or no git)."""
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-list", "--max-parents=0", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.split()[-1] if r.returncode == 0 and r.stdout.split() else ""


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _keys(value) -> list[str]:
    """The queue item keys one task covers (a list, or one string)."""
    keys = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(k for k in (_text(v, ITEM_MAX) for v in keys) if k))


def _task(row, i: int) -> dict | None:
    """One task from an agent's plan, normalised. Unknown effort is ``standard``, so a plan is
    never thrown away over a typo; an unknown status is ``open``."""
    if not isinstance(row, dict):
        return None
    items = _keys(row.get("items"))
    title = _text(row.get("title") or row.get("task"), 200)
    if not items and not title:
        return None
    effort = _text(row.get("effort"), 20).lower()
    status = _text(row.get("status"), 20).lower()
    return {
        "id": _text(row.get("id"), 40) or f"t{i + 1}",
        "title": title or items[0],
        "items": items,
        "effort": effort if effort in EFFORTS else "standard",
        "agent": _text(row.get("agent"), 80),
        "status": status if status in STATUSES else "open",
        "commit": _text(row.get("commit"), 40),
        "note": _text(row.get("note"), 500),
    }


def _tasks(value) -> list[dict]:
    """A plan's tasks: at most PLAN_MAX, ids unique (a repeat gets a suffix)."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(value if isinstance(value, list) else []):
        t = _task(row, i)
        if t is None:
            continue
        base, n = t["id"], 2
        while t["id"] in seen:
            t["id"], n = f"{base}-{n}", n + 1
        seen.add(t["id"])
        out.append(t)
        if len(out) >= PLAN_MAX:
            break
    return out


def _raw(root: Path) -> dict:
    """The state file as it is on disk (no case check), for the updates that rewrite it."""
    try:
        data = json.loads(state_file(root).read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _write(root: Path, data: dict) -> dict:
    f = state_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, f)
    return read(root)


def read(root: Path) -> dict:
    data = _raw(root)
    if data.get("case") and data["case"] != case_id(root):
        data = {}  # another case at this path: its rolls are not ours
    history = data.get("history") if isinstance(data.get("history"), list) else []
    last = history[0] if history and isinstance(history[0], dict) else {}
    return {
        "at": float(data.get("at") or 0),
        "target": str(data.get("target") or ""),
        "notes": data.get("notes") if isinstance(data.get("notes"), dict) else {},
        "feedback": [str(x) for x in data.get("feedback") or []],
        "history": history,
        "held": data.get("held") if isinstance(data.get("held"), list) else [],
        "items": [i for i in last.get("items") or [] if isinstance(i, dict)],  # what the last roll sent
        "tasks": _tasks(data.get("tasks")),
        "plan_at": float(data.get("plan_at") or 0),
        "plan_by": str(data.get("plan_by") or ""),
    }


def rolled(root: Path, target: str, notes: dict, feedback: list, items: list, held: list | None = None) -> dict:
    """Record a roll: everything in ITEMS was sent to TARGET now. HELD are the items the user
    switched off: they stay in the queue for a later roll. The previous roll's plan goes with it."""
    prev = read(root)
    now = time.time()
    seen = list(dict.fromkeys([*prev["feedback"], *(str(f) for f in feedback)]))
    entry = {"at": now, "target": target, "items": [i for i in items if isinstance(i, dict)][:200]}
    return _write(
        root,
        {
            "case": case_id(root),
            "at": now,
            "target": target,
            "notes": {**prev["notes"], **{str(k): str(v) for k, v in notes.items()}},
            "feedback": seen[-2000:],
            "history": [entry, *prev["history"]][:50],
            "held": [h for h in (held or []) if isinstance(h, dict)][:500],
            "tasks": [],
            "plan_at": 0,
            "plan_by": "",
        },
    )


def plan(root: Path, tasks: list, by: str = "") -> dict:
    """Record the top-level agent's split of the last roll: one task per independent piece."""
    data = _raw(root)
    data.update(case=case_id(root), tasks=_tasks(tasks), plan_at=time.time(), plan_by=_text(by, 80))
    return _write(root, data)


def task(root: Path, tid: str, status: str | None = None, commit: str | None = None,
         note: str | None = None) -> dict | None:
    """Update one planned task. None when the plan has no such id."""
    rec = read(root)
    found = None
    for t in rec["tasks"]:
        if t["id"] != _text(tid, 40):
            continue
        if status is not None:
            s = _text(status, 20).lower()
            if s not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            t["status"] = s
        if commit is not None:
            t["commit"] = _text(commit, 40)
        if note is not None:
            t["note"] = _text(note, 500)
        found = t
    if found is None:
        return None
    data = _raw(root)
    data.update(case=case_id(root), tasks=rec["tasks"], plan_at=rec["plan_at"], plan_by=rec["plan_by"])
    return _write(root, data)
