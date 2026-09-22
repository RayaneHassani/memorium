# Memorium

Turn your **Claude Code** sessions (`~/.claude/projects/*.jsonl`) into a clean, searchable HTML journal — read past conversations, full-text search across all of them, and highlight and annotate passages. Pure Python **standard library**, a single file, **zero dependencies**.

[![CI](https://github.com/RayaneHassani/memorium/actions/workflows/ci.yml/badge.svg)](https://github.com/RayaneHassani/memorium/actions/workflows/ci.yml)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Dependencies: none](https://img.shields.io/badge/dependencies-0-brightgreen)
![Pure stdlib](https://img.shields.io/badge/pure-stdlib-informational)
![Offline](https://img.shields.io/badge/offline-first-blueviolet)

## Why

Claude Code stores every session as raw JSONL under `~/.claude/projects`. That's great for tooling, useless for humans: you can't skim what you did last week, find that one command, or keep notes on a design decision. Memorium renders those logs into a static site you actually want to open — no server, no database, no build toolchain.

## Features

- **Readable transcripts** — prompts, responses, tool calls and terminal output, laid out for long-form reading.
- **Full-text search** across every session, prebuilt at export time (`searchindex.js`).
- **Highlight & annotate** — select any passage, highlight in five colors, attach a comment. Persisted in `localStorage`.
- **Logical organization** — rename sessions and folders, move sessions between folders. Stored in a derived metadata layer (`data/metadata.json`) that **never touches** the source JSONL.
- **Offline-first** — system font stack, no web fonts, no CDN, no network calls. Open it on a plane.

## Design principle: never mutate the source

`~/.claude/projects` is owned by Claude Code and indexed by its tooling. Renaming a folder or moving a `.jsonl` file would break that indexing. So every rename and move here is **logical only** — recorded in a separate `metadata.json` and resolved at display time. The source of truth stays untouched and every change is reversible. This is the core architectural trade-off, chosen deliberately over physically reorganizing files.

Writing that metadata requires the browser's **File System Access API**, which is disabled on `file://` pages. That's why write features run through `memorium serve`, which serves the export over `http://localhost` — a *secure context* — with a dumb static file server and no business logic on the server side.

## Install

With [pipx](https://pipx.pypa.io) (isolated global CLI):

```bash
pipx install .
# or straight from git
pipx install git+https://github.com/RayaneHassani/memorium
```

`memorium` is now a global command.

Without installing, via [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/RayaneHassani/memorium memorium
```

## Usage

```bash
memorium                  # read ~/.claude/projects, write ./export, open the browser
memorium "D:\output"      # custom output directory
memorium serve            # serve the export on http://localhost:8137 (unlocks write features)
```

Output: `export/index.html` + `export/sessions/*.js` + `export/searchindex.js`. Everything is static — copy the folder anywhere and open `index.html`.

Read-only browsing works from `file://` in any browser. **Annotations and organization** need `memorium serve` and a Chromium browser (Chrome/Edge/Brave) for the File System Access API.

## Updating

Upgrade the tool, then re-run it in the same output directory:

```bash
pipx upgrade memorium
memorium           # or: memorium serve
```

Re-running only rewrites the generated pages (`index.html`, `sessions/`, `searchindex.js`). Your `export/data/` — any renames or moves — is never touched. Highlights and comments live in the browser (`localStorage`), keyed to how you open the export; browse through `memorium serve` so they keep a stable location and survive updates.

## Local development

```bash
python export.py         # equivalent to the installed command
```

## License

MIT — see [LICENSE](LICENSE).
