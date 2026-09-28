# 4. Archive raw sessions outside `~/.claude` and raise the retention

- Status: Accepted
- Date: 2026-09-28 (recorded retroactively)

## Context

Claude Code silently deletes any session file untouched for `cleanupPeriodDays`, 30 days by default. Every export re-reads `~/.claude/projects`, so a session purged there also disappears from the next export: without a copy of its own, Memorium inherits the amnesia it is meant to cure.

That directory belongs to Claude Code. Its retention policy, its layout and its cleanup are outside Memorium's control, and a copy kept inside it would live under the same rules.

## Options considered

1. **Raise `cleanupPeriodDays` only.** One setting, nothing to maintain. But the sessions stay in a single place, lost on a reinstall or a machine change, and one edit of the setting away from the purge.
2. **Copy the sessions inside `~/.claude`.** Still under Claude Code's cleanup and conventions, which can change without notice.
3. **A scheduled job** (cron, Task Scheduler). A different setup per operating system, and it runs whether or not a session ended.
4. **A separate archive, written when each session ends, plus a longer retention.**

## Decision

Option 4.

`memorium init` shows the exact changes, asks for consent (`--yes` skips the prompt), backs up `settings.json` to `settings.json.bak`, then:

- raises `cleanupPeriodDays` to 3650, and never lowers a longer value the user chose;
- adds a `SessionEnd` hook that runs `memorium archive`, calling the interpreter and the script by absolute path so the hook does not depend on `PATH`.

`memorium archive` writes each raw session to `~/.memorium/archive/<project>/<id>.jsonl.gz` (overridable with `MEMORIUM_ARCHIVE_DIR`). It is incremental: a session log only grows, so an unchanged size and modification time means nothing new to write. Each file is written to a `.part` file first and renamed, so an interruption never leaves a truncated archive.

`memorium restore <id>` decompresses a session back where Claude Code looks it up, so `claude --resume` works again.

## Consequences

- Sessions survive the purge, a reinstall and a machine change: copying the archive directory is enough.
- The archive is plain gzip, readable without Memorium. On a real history, 197 MB of raw logs take 62 MB archived.
- Memorium writes to `settings.json`, a file it does not own. Consent, the backup and the raise-never-lower rule limit that risk; they do not remove it.
- The archive only grows. Nothing prunes it.
- The hook stores absolute paths. If the installation moves (for example a pipx reinstall on another Python), the hook stops archiving until it is fixed, and `memorium init` detects an existing Memorium hook and leaves it as is.

## Revisit when

- Claude Code offers a native export, or a retention setting that makes the purge opt-in.
- Claude Code changes where or how it stores session logs.
- The archive grows large enough to need pruning or deduplication.
