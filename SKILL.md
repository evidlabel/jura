---
name: workbench
description: Use when working as an agent inside a jura workbench case (a ~/cases/<id> workdir with workbench.yml, or $WORKBENCH_CASE set) - reading the case's linked evid sets, writing Typst with labquote keys from refs/*.yml, answering the user's notes and feedback, and committing work so auto-git attributes it.
---

# workbench

You run in the Write tab's sandbox: your usual terminal, minus the case's private data. The case workdir is yours. The global evid sets linked in `workbench.yml` are readable. Local sets, including PDFs handed off from a did set, are `evid/` in the workdir (`evid --db evid`). The did sets live in the Anonymization pane, outside this case. `~/did_workdirs`, the raw case files and the other cases are hidden on purpose, so don't look for them.

Discover: `workbench -h`, `evid -h`.

## Read

- `evid set list`: the linked global sets. `evid --db evid set list`: the local sets, including a did set handed off into `evid/anon/anon_set_<name>/`. Placeholders (`[PERSON 1]`) are identities: keep them verbatim, and never guess who they are.
- `evid search text -s SET "…"`, `evid label ls -s SET -u UUID`. Citations for the linked sets are one file, `refs/refs.yml` at the workdir root: each set is gathered on its own, then `evid refs merge`. `jura gather` rewrites it, from any folder in the case, merging hand-added `refs/extra/*.yml` (citations from outside evid, such as a lawcite statute) after the sets. labquote prints only the entries a document cites.

## Write

- Typst. `main.typ` is the case map (the question, the line, what is open). Memos and briefs are other files. There can be any number of them. Quote only through labquote keys (`#blockq(<key>)`, `#inlineq`, `#cite-ref`) against `refs/refs.yml`, loaded from any folder as `#setup(yaml("/refs/refs.yml"))`. Quote text always comes from evid at compile time, never from you.
- `refs/refs.yml` is the only citation file. Any other `refs.yml` in the case is in the wrong place and is not read: never write one (not even with `evid set gather -o`), and never search for where refs go, in the case or in the workbench code. When keys are missing or stale, run `jura gather`. It also lists stray `refs.yml` files.
- Where every file goes is fixed: the case's `AGENTS.md` has the layout, and `jura layout` prints it and lists anything in the wrong place, with the `git mv` / `git rm` that fixes it. In short: documents are `docs/<topic>/<name>.typ`, PDFs are only ever `out/<same path>.pdf` (not in git), citations are `refs/refs.yml` plus hand-added `refs/extra/*.yml`. Never derive it from anywhere else.
- `jura compile docs/<topic>/<name>.typ` to check your work: it writes `out/docs/<topic>/<name>.pdf`.

## Answer the user

- `jura write fb ls` lists open feedback (files, folders, line spans). `jura write edits` shows the user's saved and unsaved edits, and `jura write context` shows their current selection.
- A **roll** from the Queue lists exactly what to act on: do those items, in order.
- The user's saves are already committed (`user: …`). Commit your own work per item as `agent: <what changed>`, then reply with `jura write fb reply ID "what changed (hash)" --done`.
- Anything you leave uncommitted is committed for you as `agent: …` once the tree is quiet.
