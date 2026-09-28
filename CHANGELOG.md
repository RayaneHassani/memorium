# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-09-28

First public release.

### Added

- Export every Claude Code session in `~/.claude/projects` to a static, offline HTML site: prompts, answers, tool calls and terminal output.
- Full-text search across all sessions, from an index built at export time.
- Highlights in five colours and margin comments, with a per-session recap.
- Logical organization: rename sessions and folders, move sessions between folders, without touching the source logs.
- Dashboard with weekly activity and the latest sessions of every project.
- `memorium serve` to serve the export on `http://localhost`, which unlocks renaming and moving sessions.
- `memorium archive` to gzip every raw session outside `~/.claude`, incrementally.
- `memorium restore` to put an archived session back so `claude --resume` works again.
- `memorium init` to raise the session retention and archive every session as it ends.
- `memorium --help` and `memorium --version`.
- Embedded fonts, so the export never touches the network.

[Unreleased]: https://github.com/RayaneHassani/memorium/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/RayaneHassani/memorium/releases/tag/v1.0.0
