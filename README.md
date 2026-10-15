[![CI](https://github.com/evidlabel/jura/actions/workflows/ci.yml/badge.svg)](https://github.com/evidlabel/jura/actions/workflows/ci.yml)[![Version](https://img.shields.io/github/v/tag/evidlabel/jura?include_prereleases&sort=semver&label=version)](https://github.com/evidlabel/jura/tags)![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)![Platform](https://img.shields.io/badge/platform-Linux-lightgrey.svg)[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)[![Website](https://img.shields.io/badge/web-jura.sh-1f6feb.svg)](https://jura.sh/workbench/)

# jura workbench

A legal engineering workbench. One window for a legal case: write it with agents, next
to its datasets and its anonymization.

**Alpha.** This is a prototype. The header shows the running release (`0.1.0a2`). Features,
file formats and behaviour will change. The **?** button in the header says the same.

```
jura.sh 0.1.0a2 / Tenancy dispute 2026-014 ▾                         theme ▾  ?
Write agent · Datasets app · Anonymization app
```

| Tab | What it is |
|---|---|
| **Write** `agent` | The case workdir as a tree you edit and annotate, a wrapped Typst editor with a tinymist preview beside it, and a stack of agents and terminals. The agents pane stays closed until you open a harness. Every change is committed in the background. |
| **Datasets** `app` | evid: documents, labels, search, over the global evid store. |
| **Anonymization** `app` | did: create sets and add documents. The files stay outside the case, under `~/did_workdirs`. Write connects to those sets. |

Ctrl+PageDown and Ctrl+PageUp cycle the tabs, from anywhere in the window. Ctrl+W and Ctrl+Q quit the window. In a terminal, Ctrl+W stays delete-word.

## The flow

1. `jura case new 2026-014 "Tenancy dispute"` creates `~/cases/2026-014/`, the git-tracked workdir (`workbench.yml`, `AGENTS.md`, `main.typ`, and empty `docs/`, `refs/extra/` and `out/`). Its agents are Claude, Codex, Grok and OpenCode. One that is not installed is greyed out in the Agent menu. It does not create a did folder and it does not create a global evid set.
2. In **Anonymization**, create a did set and add the case files. The set lives under `~/did_workdirs/<set>/`, outside the case. That same set shows up in Write.
3. In **Write**, the Sources pane is two pools of pills. Evid pills (one color) link a global set or a local set under `evid/plain/`. Did pills (the other color) are the sets from Anonymization: connect one, and choose how the PDF is labelled (`placeholder` by default, or `faker`, `anon`, `pseudo`). Connecting a set writes those PDFs into this case's evid directory, `evid/anon/anon_set_<set>/`. **Hand off** writes them again. A connected did pill is marked stale when a document is added or the extracted keys change. **Gather** (or `jura gather`, for agents) writes one merged `refs/refs.yml` for the linked sets. It is the case's only citation file: any `.typ` file, in any folder, loads it as `yaml("/refs/refs.yml")`, and labquote prints only the entries it cites. Any other `refs.yml` in the case is not read, and Gather lists it as stray. Citations from outside evid (a lawcite statute, …) go in `refs/extra/*.yml`, which Gather merges in.
4. Annotate files, folders and lines. In the **Queue**, choose which of your new edits and notes go to which agent, then **Roll**.
5. **Tag state…** marks the current version (`state/<date>-<name>`).

## Where things go

A new case comes with one opinionated layout, written into its `AGENTS.md` so an agent never has to work it out: documents in `docs/<topic>/<name>.typ`, the case map in `main.typ`, citations in `refs/refs.yml` (plus hand-added `refs/extra/*.yml`), evid's store in `evid/`, and every PDF in `out/`, mirroring its source (`docs/risk/notat.typ` → `out/docs/risk/notat.pdf`). `out/` is not in git. `jura compile <file.typ>` builds into it, and so does **Export PDF**. `jura layout` prints the layout and lists every file in the wrong place, with the `git mv` or `git rm` that fixes it.

## Why Typst, and git

Every document in a case is Typst: plain text with light markup, compiled to PDF. Git
tracks plain text line by line, so the case's history is something to read rather than a
list of saves. Each commit (yours as `user:`, an agent's as `agent:`) shows the sentences
it changed, a diff between two tagged states is a redline, and `git blame` says who wrote a
line. A `.docx` is a zipped binary to git: its history could only say that the file
changed. Plain text is also what agents edit reliably, and the same source always gives
the same PDF. Nobody has to learn Typst syntax to work in a case: nested plain-text bullet
lists (`- point`, indented for sub-points) are already valid Typst, and the rest comes from
annotating a file, a folder or a few lines and rolling the note to an agent. Quotes stay
out of the text: they are keys into `refs/refs.yml`, drawn from the source at compile time.

## Isolation

Raw case files carry personal data. They stay in the did set, outside the case, and only the handed-off PDFs reach the case. Every terminal in Write runs in a bubblewrap sandbox. Inside, it is your usual terminal, as in treedit: your shell and its config, history, and every agent harness (claude, hermes, grok, opencode, codex, …) with its login and settings. Hidden from it:
- did's folder (`~/did_workdirs`, or `$WORKBENCH_DID`), and the folders the raw files did has read came from (from each set's `draft/sources.json`). A raw file sitting directly in `$HOME` is hidden on its own.
- The global evid sets the case did not link in `workbench.yml`. Local evid sets live in the workdir, so the sandbox shows them with the case.
- The other cases.
- Other projects' Claude Code transcripts, prompt history, file history and pastes. Each case keeps its own in `~/.local/state/workbench/claude/`.
- Other programs' scratch files in `/tmp`. Each terminal gets its own `/tmp`.
- Anything in `$WORKBENCH_SANDBOX_HIDE`.

## Install (alpha, Linux)

```sh
curl -LsSf https://jura.sh/install.sh | sh
```

That installs uv if it is missing, then evid, did and the workbench as uv tools, then runs
`jura setup`. Setup installs what is still missing and asks before each step: `typst` and
`tinymist` (live preview) into `~/.local/bin`, `git` and `bwrap` (bubblewrap) with your
package manager (sudo, asked separately), optionally poppler (`pdftotext`, did's fallback
for broken PDF text), and did's language models. Agent harnesses
(`claude`, `codex`, `grok`, `opencode`, …) for the Write terminals are your own install.

By hand:

```sh
uv tool install "evid @ git+https://github.com/evidlabel/evid"                 # 0.8.0 or newer
uv tool install --python 3.12 "did[gui,models] @ git+https://github.com/evidlabel/did"   # 0.3.2 or newer
uv tool install "jura-workbench @ git+https://github.com/evidlabel/jura"
jura setup           # or: jura doctor, which only lists what is missing or too old
jura open            # the browser; for the app window, build it from a clone (below)
```

The empty window offers a case to start from: a blank map, or a plain question about a rent
increase, a contact arrangement or a tax reassessment (`jura case new ID --template rent`).
evid and did change together with the workbench: an older one shows an **update** pill in
the header, and `jura doctor` says which.

## Run from a clone

```sh
make sync        # workbench's own .venv (never the shared jura env)
make deps        # tinymist for the Typst preview (or set $TINYMIST)
make app-build   # the workbench-app window (Tauri; `make app-deps` for the libraries)
make gui         # open it; `make web` for the browser
```

Needs `evid` and `did` on PATH (`$WORKBENCH_EVID_BIN` / `$WORKBENCH_DID_BIN` override them), plus `typst`, `git` and `bwrap`.

| Variable | Default | |
|---|---|---|
| `WORKBENCH_CASES` | `~/cases` | case workdirs |
| `WORKBENCH_DID` | `~/did_workdirs` | did sets, one directory each. Not inside a case. |
| `WORKBENCH_EVID_DB` | evid's own | a separate evid data dir (a deliverable, a test) |
| `WORKBENCH_AGENT` | (unset) | command typed into the first Write terminal, e.g. `claude`. Unset, the pane stays closed and no shell starts. |
| `WORKBENCH_AGENTS` | shipped `agents.toml` | harness buttons. A button shows when its program is on PATH. `~/.config/workbench/agents.toml` replaces the shipped list when that file exists. A row may set `env` for one config (e.g. an opencode profile), and one harness may be listed several times. A case can list its own rows under `agents:` in `workbench.yml`; they come first and replace the catalogue by name. |
| `WORKBENCH_SKILLS` | case `skills.dirs`, else shipped `skills.toml` | one directory that replaces the skill directories. A case lists its own directories under `skills.dirs` in `workbench.yml`. With no `dirs` key, `~/.config/workbench/skills.toml` replaces the shipped list (claude, grok, hermes, codex) when that file exists. |
| `WORKBENCH_SANDBOX` | `1` | `0` turns the sandbox off (each terminal shows it) |

## Discover

```sh
workbench -h          # open · case new|ls · layout · compile · gather · doctor · write …
jura write -h         # the Write tab's own CLI: notes, feedback, the user's edits
```

## Credits

The Write tab is a fork of [wr1/treedit](https://github.com/wr1/treedit) at `94e6806` (MIT, © 2026 wr1). See `src/workbench/write/LICENSE-treedit.txt`; every forked file says so in its header. The window shell is adapted from evid-app. It bundles xterm.js (MIT), Source Sans 3, Source Serif 4 and JetBrains Mono (OFL).

## License

MIT, see [LICENSE](LICENSE).
