# Demo sessions

Synthetic Claude Code sessions used for the live demo and tests — **not real logs**. They cover three everyday scenarios so you can try the viewer without exporting your own history:

- **webapp** — a unit test that breaks after a refactor, then goes green again
- **api** — adding an authenticated `GET /profile` endpoint (401 on missing token)
- **cli** — fixing a `KeyError` crash and defaulting a missing config value

Build the demo export from them by pointing the source at this folder:

```bash
MEMORIUM_PROJECTS_DIR=demo/sessions memorium
```
