# 1. Organize sessions logically, never on disk

- Status: Accepted
- Date: 2026-09-28 (recorded retroactively)

## Context

Users want to rename sessions, rename folders and move sessions between folders. The sessions live in `~/.claude/projects/<encoded project>/<session id>.jsonl`, a directory owned and indexed by Claude Code. `claude --resume <id>` finds a session by its file name and its project directory: a file renamed or moved there is a session Claude Code can no longer resume.

## Options considered

1. **Rename and move the files on disk.** What the user sees matches the file system. But it breaks `claude --resume`, and every change is a mutation of data Memorium does not own.
2. **Copy the sessions into a Memorium-owned tree and reorganize the copies.** Resume keeps working on the originals, but two copies drift apart and the storage doubles.
3. **Record the organization in a separate overlay file and resolve it at display time.**

## Decision

Option 3. Every rename and move is written to `export/data/metadata.json`, shaped as `{"folders": {...}, "sessions": {"<id>": {"title": ..., "folder": ...}}}`. The page reads the raw sessions first, then applies the overlay on top. Moving a session back to its original folder deletes its override instead of storing a redundant one, so the file only holds actual differences.

## Consequences

- The source of truth is never mutated. `claude --resume` keeps working, and every change is reversible by deleting its entry.
- The organization exists only inside Memorium. The file system still shows the raw names.
- `metadata.json` becomes user data worth keeping. Re-running an export rewrites the generated pages and never touches `export/data/`.
- A session deleted from the source leaves a dead entry in the overlay, which is harmless and ignored at display time.

## Revisit when

- Claude Code offers its own way to rename or group sessions.
- Users need the organization outside Memorium, for example in the terminal session picker.
