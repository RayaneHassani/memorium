# 2. Ship a single file on the standard library only

- Status: Accepted
- Date: 2026-09-28 (recorded retroactively)

## Context

Memorium is a personal tool installed next to Claude Code, on machines where Python may be anything from 3.8 to the latest release. Every dependency is one more thing that can fail to install, conflict with another tool, or pull the network into an export that must work offline.

## Options considered

1. **A regular package with dependencies**: Click for the CLI, Jinja2 for the pages, a Markdown library for the answers. Idiomatic and easier to split, at the cost of a dependency tree to resolve and keep compatible across six Python versions.
2. **Several modules, standard library only.** Easier to navigate, but a multi-file layout brings packaging work for no user-facing gain at this size.
3. **One file, standard library only**: CLI, HTML generator, search index and the viewer template in `export.py`.

## Decision

Option 3. `export.py` runs as `python export.py` straight from a clone, or as `memorium` once installed with pipx. The CLI parses its own arguments, the Markdown renderer is hand-written for the subset Claude Code produces, and the fonts are embedded as base64 so the output never touches the network. CI byte-compiles and tests it on Python 3.8 to 3.13.

## Consequences

- Installation cannot fail on a dependency, and there is nothing to audit or update upstream.
- About 2,500 lines in one file, more than half of them the HTML, CSS and JavaScript of the viewer held in a Python string. Editors do not highlight or lint that template.
- The hand-written Markdown renderer covers what Claude Code emits, not the full CommonMark specification.
- The embedded fonts add most of the 243 KB app shell.

## Revisit when

- The viewer template grows enough that editing it inside a Python string slows changes down: move it to a package data file, still without dependencies.
- A feature needs a library the standard library cannot replace reasonably.
