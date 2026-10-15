"""The Write tab's Queue: the user's new edits and annotations since the last roll.

The page builds the queue from what the workspace already tracks (saves, unsaved drafts,
annotations, feedback). This keeps the one thing it cannot know: what the last roll sent —
when, to which agent, the annotations as they were, and the feedback ids — so the next queue
holds only what is new. Kept per workdir in the workbench state dir, outside the case, with the
case's identity (its first commit): a new case at the same path starts with an empty record.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from workbench import paths


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


def read(root: Path) -> dict:
    try:
        data = json.loads(state_file(root).read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or (data.get("case") and data["case"] != case_id(root)):
        data = {}  # another case at this path: its rolls are not ours
    return {
        "at": float(data.get("at") or 0),
        "target": str(data.get("target") or ""),
        "notes": data.get("notes") if isinstance(data.get("notes"), dict) else {},
        "feedback": [str(x) for x in data.get("feedback") or []],
        "history": data.get("history") if isinstance(data.get("history"), list) else [],
        "held": data.get("held") if isinstance(data.get("held"), list) else [],
    }


def rolled(root: Path, target: str, notes: dict, feedback: list, items: list, held: list | None = None) -> dict:
    """Record a roll: everything in ITEMS was sent to TARGET now. HELD are the items the user
    switched off: they stay in the queue for a later roll."""
    prev = read(root)
    now = time.time()
    seen = list(dict.fromkeys([*prev["feedback"], *(str(f) for f in feedback)]))
    entry = {"at": now, "target": target, "items": items[:200]}
    data = {
        "case": case_id(root),
        "at": now,
        "target": target,
        "notes": {**prev["notes"], **{str(k): str(v) for k, v in notes.items()}},
        "feedback": seen[-2000:],
        "history": [entry, *prev["history"]][:50],
        "held": [h for h in (held or []) if isinstance(h, dict)][:500],
    }
    f = state_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, f)
    return read(root)
