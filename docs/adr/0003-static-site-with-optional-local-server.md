# 3. A static site, with an optional local server for writing

- Status: Accepted
- Date: 2026-09-28 (recorded retroactively)

## Context

Reading sessions needs nothing but HTML files. Organizing them needs somewhere to write the `metadata.json` overlay (see ADR 1), next to the export so it can be kept and copied with it. A page opened from `file://` cannot write to disk, and the browser API that can, the File System Access API, is only available in a secure context, which `file://` is not and `http://localhost` is.

## Options considered

1. **A local web application**: a Python server with an API that stores the user's changes. It works in every browser, but it is a backend to write, secure and keep running for every read.
2. **A desktop application** (Electron, Tauri). Full file access, at the cost of a heavy runtime and a build per operating system.
3. **A static site, read-only everywhere, plus an optional static file server** that turns the page into a secure context so the browser itself writes to the export folder.

## Decision

Option 3. The export opens from `file://` in any browser for reading. `memorium serve` starts the standard library's `http.server` on `http://localhost:8137`, with no application logic. In the page, the user grants access to the export folder once through `showDirectoryPicker`; the handle is kept in IndexedDB, and writes go to `export/data/`.

Highlights and comments take a lighter path: they are saved in the browser's `localStorage`, which works from `file://` and in every browser.

## Consequences

- No backend: the server only serves files, and the export stays a folder that can be copied, zipped or opened on a plane.
- Organizing requires a Chromium browser (Chrome, Edge, Brave). Firefox and Safari do not ship the File System Access API; there, sessions cannot be renamed or moved.
- Highlights and comments live in one browser profile only. Clearing site data erases them, and `file://` and `http://localhost` are separate origins, so highlights made in one do not show in the other.
- The browser asks for the folder permission, and may ask again in a later visit.
- The port is fixed at 8137. If it is already taken, `serve` fails instead of picking another one.

## Revisit when

- Firefox or Safari ship the File System Access API, or a comparable writable storage becomes available on `file://`.
- Users need annotations synchronized across machines, which would justify a backend.
