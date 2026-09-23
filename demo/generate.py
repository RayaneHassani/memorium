"""Generate the synthetic demo corpus into demo/sessions.

Re-run after editing a scenario: python demo/generate.py
Timestamps are written into every line, so session dates survive a git clone.
"""
import datetime
import json
import os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
CWD = {"api": "/home/dev/acme-api", "webapp": "/home/dev/acme-webapp",
       "cli": "/home/dev/acme-cli", "deploy": "/home/dev/acme-deploy"}


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def write(project, sid, start, title, steps):
    """steps: ("u", prompt) | ("a", text, [(tool, input, result, is_error), ...])"""
    t = datetime.datetime.fromisoformat(start)
    lines = [{"type": "ai-title", "aiTitle": title}]
    n = 0

    def stamp(o, dt):
        nonlocal t
        t += datetime.timedelta(seconds=dt)
        o["timestamp"] = iso(t)
        o["cwd"] = CWD[project]
        o["sessionId"] = sid
        lines.append(o)

    for st in steps:
        if st[0] == "u":
            stamp({"type": "user", "message": {"content": st[1]}}, 95)
            continue
        _, text, tools = st
        content = [{"type": "text", "text": text}]
        ids = []
        for name, inp, _, _ in tools:
            n += 1
            ids.append("t%d" % n)
            content.append({"type": "tool_use", "id": ids[-1], "name": name, "input": inp})
        stamp({"type": "assistant", "message": {"content": content}}, 25)
        for tid, (_, _, result, err) in zip(ids, tools):
            stamp({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": err}]}}, 12)

    os.makedirs(os.path.join(ROOT, project), exist_ok=True)
    with open(os.path.join(ROOT, project, sid + ".jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n")


def backfill(project, sid, start):
    """Add timestamp + cwd to an existing hand-written session, content untouched."""
    path = os.path.join(ROOT, project, sid + ".jsonl")
    with open(path, encoding="utf-8") as f:
        objs = [json.loads(l) for l in f if l.strip()]
    t = datetime.datetime.fromisoformat(start)
    for o in objs:
        if o.get("type") == "ai-title":
            continue
        t += datetime.timedelta(seconds=40)
        o["timestamp"] = iso(t)
        o["cwd"] = CWD[project]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n")


def bash(cmd, desc):
    return {"command": cmd, "description": desc}


# ── Existing three sessions: dates only ─────────────────────────────────────
backfill("webapp", "a1c0ffee-1111-4a2b-8c3d-0000000000a1", "2026-08-10T09:12:00")
backfill("api", "b2d1e2f3-2222-4b3c-9d4e-0000000000b2", "2026-08-18T14:05:00")
backfill("cli", "c3e2f3a4-3333-4c4d-ae5f-0000000000c3", "2026-09-05T10:40:00")

# ── api ─────────────────────────────────────────────────────────────────────
write("api", "d4f3a4b5-4444-4d5e-8f60-0000000000d4", "2026-08-12T16:20:00",
      "Rate-limit /login after a brute-force spike", [
    ("u", "Logs show ~3,000 failed /login attempts from a handful of IPs overnight. Add rate limiting to /login without hurting normal users."),
    ("a", "First I want the real shape of the traffic, so the limit is set from data rather than a guess.", [
        ("Bash", bash("grep 'POST /login' logs/access.log | awk '{print $1}' | sort | uniq -c | sort -rn | head -5",
                      "Top source IPs on /login"),
         "   1412 203.0.113.42\n    988 203.0.113.77\n    603 198.51.100.9\n      6 192.0.2.15\n      4 192.0.2.88", False)]),
    ("a", "Three IPs account for 99.5% of the attempts; real users peak at about 6 tries. A limit of **5 attempts per minute per IP plus per username** stops the attack and leaves headroom for a mistyped password. Per-username matters: IP-only limits are trivially bypassed with a proxy pool.", [
        ("Read", {"file_path": "app/routes/auth.py"},
         "@router.post(\"/login\")\nasync def login(body: LoginIn, db=Depends(get_db)):\n    user = await users.by_email(db, body.email)\n    ...", False),
        ("Edit", {"file_path": "app/routes/auth.py"}, "The file app/routes/auth.py has been updated.", False)]),
    ("a", "Adding a test that proves the 6th attempt is rejected with 429 and a Retry-After header, and that a successful login resets the counter.", [
        ("Write", {"file_path": "tests/test_rate_limit.py"}, "File created successfully at: tests/test_rate_limit.py", False),
        ("Bash", bash("pytest tests/test_rate_limit.py -q", "Run the rate-limit tests"),
         "...\n3 passed in 0.41s", False)]),
    ("a", "Done. `/login` now answers **429 Too Many Requests** after 5 failed attempts per minute, keyed on both IP and username, with a `Retry-After` header. The counter lives in memory for now: fine for a single instance, but it must move to a shared store before we scale horizontally.", []),
])

write("api", "e5a4b5c6-5555-4e6f-9071-0000000000e5", "2026-08-14T10:02:00",
      "Choose Postgres over SQLite for the API", [
    ("u", "Before the profile work lands we need to pick the database for the API. SQLite or Postgres? Give me the trade-offs, measure if you can, then decide."),
    ("a", "The deciding factor is concurrent writes: SQLite serializes every write behind a single database lock. Let me check how many writers we actually have.", [
        ("Bash", bash("grep -E 'workers|concurrency' gunicorn.conf.py worker.toml", "Count concurrent writers"),
         "gunicorn.conf.py:workers = 4\nworker.toml:concurrency = 2", False)]),
    ("a", "Six processes can write at the same time: 4 web workers plus 2 background workers. Measuring both engines under that exact load instead of arguing in the abstract.", [
        ("Bash", bash("python scripts/write_bench.py --writers 6 --ops 300", "Benchmark concurrent writes on both engines"),
         "engine     ok    errors                      p95\nsqlite    212    88 (database is locked)    410ms\npostgres  300     0                          18ms", False)]),
    ("a", "**Decision: Postgres 16.**\n\n- **Why:** with 6 concurrent writers, SQLite failed 29% of writes with `database is locked` and hit a 410 ms p95. Postgres took the same load with zero errors and an 18 ms p95. MVCC lets readers and writers work without blocking each other.\n- **Options considered:** SQLite in WAL mode (better, but still one writer at a time), SQLite plus a write queue (moves the bottleneck and adds code), Postgres (one extra service to run).\n- **Trade-offs accepted:** one more container in `docker compose`, backups through `pg_dump` instead of copying a file, and tests need a Postgres service in CI.\n- **Revisit if:** the API ever becomes single-writer, for example a read-only mirror, where SQLite would be simpler.\n\nRecording this as an ADR so the reasoning outlives this conversation.", [
        ("Write", {"file_path": "docs/adr/0003-postgres-over-sqlite.md"},
         "File created successfully at: docs/adr/0003-postgres-over-sqlite.md", False)]),
    ("u", "Good. Wire it into the settings so dev still works with one command."),
    ("a", "Adding a `postgres` service to `docker-compose.yml` and reading `DATABASE_URL` from the environment, with a local default for dev.", [
        ("Edit", {"file_path": "docker-compose.yml"}, "The file docker-compose.yml has been updated.", False),
        ("Edit", {"file_path": "app/settings.py"}, "The file app/settings.py has been updated.", False),
        ("Bash", bash("docker compose up -d postgres && alembic upgrade head", "Start Postgres and run migrations"),
         "[+] Running 1/1\n ✔ Container acme-api-postgres-1  Started\nINFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema", False)]),
    ("a", "Postgres is running under compose and the schema migrates cleanly. `docker compose up` is still the only command a new developer needs. The decision and its numbers are in `docs/adr/0003-postgres-over-sqlite.md`.", []),
])

write("api", "f6b5c6d7-6666-4f70-a182-0000000000f6", "2026-09-02T15:31:00",
      "Index the session lookup: 1.8 s to 40 ms", [
    ("u", "GET /sessions/{token} got slow since the table passed a million rows. Find out why."),
    ("a", "Asking Postgres for the actual plan rather than guessing at the cause.", [
        ("Bash", bash("psql $DATABASE_URL -c \"EXPLAIN ANALYZE SELECT * FROM sessions WHERE token = 'x'\"", "Explain the slow query"),
         "Seq Scan on sessions  (cost=0.00..48211.00 rows=1 width=96) (actual time=1794.2..1794.2 rows=0 loops=1)\n  Filter: (token = 'x'::text)\n  Rows Removed by Filter: 1204331\nExecution Time: 1794.6 ms", False)]),
    ("a", "A sequential scan over 1.2M rows: `token` has no index, so every lookup reads the whole table. A unique B-tree index fits, since tokens are unique and we only match on equality. Creating it `CONCURRENTLY` so the table stays writable during the build.", [
        ("Write", {"file_path": "migrations/versions/0007_sessions_token_idx.py"},
         "File created successfully at: migrations/versions/0007_sessions_token_idx.py", False),
        ("Bash", bash("alembic upgrade head && psql $DATABASE_URL -c \"EXPLAIN ANALYZE SELECT * FROM sessions WHERE token = 'x'\"",
                      "Apply the migration and re-explain"),
         "Index Scan using sessions_token_idx on sessions  (actual time=0.031..0.032 rows=0 loops=1)\nExecution Time: 0.06 ms", False)]),
    ("a", "Fixed. The query now does an index scan: **1.8 s → 0.06 ms** in Postgres, and about **40 ms** end to end on the endpoint. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction, so the migration disables Alembic's transaction for that single step.", []),
])

# ── webapp ──────────────────────────────────────────────────────────────────
write("webapp", "a7c6d7e8-7777-4081-b293-000000000107", "2026-08-20T11:15:00",
      "Debounce the search input", [
    ("u", "Typing in the search box fires one API call per keystroke. Fix it."),
    ("a", "Confirming the behavior in the component first.", [
        ("Read", {"file_path": "src/components/SearchBox.tsx"},
         "onChange={(e) => fetchResults(e.target.value)}", False)]),
    ("a", "Every keystroke calls `fetchResults` directly. I'll debounce by 250 ms, which is short enough to feel instant and long enough to skip intermediate keystrokes. I'll also cancel the in-flight request with an `AbortController`: without it, a slow early response can overwrite a newer one, which is a classic race.", [
        ("Edit", {"file_path": "src/components/SearchBox.tsx"}, "The file src/components/SearchBox.tsx has been updated.", False),
        ("Bash", bash("npm test -- SearchBox", "Run the SearchBox tests"),
         "PASS src/components/SearchBox.test.tsx\n  ✓ debounces keystrokes (262 ms)\n  ✓ ignores stale responses (12 ms)", False)]),
    ("a", "Typing \"postgres\" now sends **1 request instead of 8**, and stale responses can no longer overwrite fresh ones.", []),
])

write("webapp", "b8d7e8f9-8888-4192-a3a4-000000000118", "2026-09-09T17:48:00",
      "Fix hydration mismatch on timestamps", [
    ("u", "React logs 'Text content does not match server-rendered HTML' on the activity feed, only for some users."),
    ("a", "\"Only some users\" points at the environment rather than the data. Checking how the feed formats dates.", [
        ("Bash", bash("grep -rn 'toLocale' src/feed", "Find locale-dependent date formatting"),
         "src/feed/Item.tsx:14:  {new Date(item.at).toLocaleString()}", False)]),
    ("a", "That's the cause. The server renders the date in UTC, then the browser re-renders it in the user's timezone, so the two HTML strings differ for anyone outside UTC. Fix: pass an explicit `timeZone` and locale to `Intl.DateTimeFormat`, and render relative times on the client only.", [
        ("Edit", {"file_path": "src/feed/Item.tsx"}, "The file src/feed/Item.tsx has been updated.", False),
        ("Bash", bash("TZ=Asia/Tokyo npm run test:e2e -- feed", "Reproduce with a non-UTC timezone"),
         "✓ feed renders without hydration warnings (1.9s)", False)]),
    ("a", "Fixed and covered: the e2e test now runs with `TZ=Asia/Tokyo`, which reproduces the mismatch before the fix and passes after it.", []),
])

# ── cli ─────────────────────────────────────────────────────────────────────
write("cli", "c9e8f9a0-9999-42a3-b4b5-000000000129", "2026-08-26T09:30:00",
      "Add a --json output flag", [
    ("u", "Scripts parse our table output with awk and break every time we tweak a column. Add a --json flag."),
    ("a", "Looking at how output is produced today.", [
        ("Read", {"file_path": "mycli/output.py"}, "def render(rows):\n    print(tabulate(rows, headers='keys'))", False)]),
    ("a", "Rendering is centralized, which makes this simple: `--json` switches `render` to `json.dumps`. Two contract decisions: the JSON goes to **stdout only**, with progress and warnings on stderr so `| jq` never breaks, and the keys are documented as stable, unlike the table layout.", [
        ("Edit", {"file_path": "mycli/output.py"}, "The file mycli/output.py has been updated.", False),
        ("Bash", bash("python -m mycli list --json | python -m json.tool | head -4", "Check the output is valid JSON"),
         "[\n    {\n        \"name\": \"nightly-backup\",\n        \"status\": \"ok\",", False)]),
    ("a", "Shipped: `mycli list --json` prints a stable, documented schema on stdout. Table output is unchanged for humans.", []),
])

write("cli", "d0f9a0b1-aaaa-43b4-85c6-00000000013a", "2026-09-16T18:05:00",
      "Release 2.1.0: changelog and version bump", [
    ("u", "Cut the 2.1.0 release: changelog, version bump, tag."),
    ("a", "Collecting what changed since 2.0.0 from the commit history.", [
        ("Bash", bash("git log v2.0.0..HEAD --oneline", "List commits since the last release"),
         "e41a9c2 feat: --json output flag\n7b20f13 fix: default timeout when the config key is missing\n1c9d0aa docs: document the JSON schema", False)]),
    ("a", "One feature and one fix, with no breaking change, so this is a **minor** bump under semver: 2.0.0 → 2.1.0.", [
        ("Edit", {"file_path": "CHANGELOG.md"}, "The file CHANGELOG.md has been updated.", False),
        ("Edit", {"file_path": "pyproject.toml"}, "The file pyproject.toml has been updated.", False),
        ("Bash", bash("git commit -am 'chore(release): 2.1.0' && git tag v2.1.0", "Commit and tag the release"),
         "[main 5f3e2d1] chore(release): 2.1.0\n 2 files changed, 9 insertions(+), 1 deletion(-)", False)]),
    ("a", "Tagged `v2.1.0`. Pushing the tag triggers the publish workflow, so I've left the push to you.", []),
])

# ── deploy ──────────────────────────────────────────────────────────────────
write("deploy", "e1a0b1c2-bbbb-44c5-96d7-00000000014b", "2026-08-28T13:22:00",
      "Multi-stage Docker image for the API: 1.1 GB to 140 MB", [
    ("u", "The API image is 1.1 GB and deploys are slow. Slim it down."),
    ("a", "Checking where the weight comes from before changing anything.", [
        ("Bash", bash("docker history acme-api:latest --format '{{.Size}}\\t{{.CreatedBy}}' | head -4", "Inspect image layers"),
         "612MB\tRUN apt-get install -y build-essential libpq-dev\n388MB\tFROM python:3.12\n74MB\tRUN pip install -r requirements.txt\n2MB\tCOPY . /app", False)]),
    ("a", "Most of the weight is build tooling that's only needed to compile `psycopg` against libpq, plus the full `python:3.12` base. A **multi-stage** build fixes both: compile wheels in a builder stage, then copy only the wheels and the runtime `libpq5` into `python:3.12-slim`. The Postgres client library stays; the compiler goes.", [
        ("Write", {"file_path": "Dockerfile"}, "File created successfully at: Dockerfile", False),
        ("Bash", bash("docker build -t acme-api:slim . && docker images acme-api", "Build and compare image sizes"),
         "REPOSITORY  TAG     SIZE\nacme-api    slim    141MB\nacme-api    latest  1.1GB", False),
        ("Bash", bash("docker compose run --rm api pytest -q", "Run the test suite in the new image against Postgres"),
         "........................................\n40 passed in 6.2s", False)]),
    ("a", "**1.1 GB → 141 MB** (−87%) and the tests pass inside the new image against the compose Postgres. Deploys pull about 8× less data.", []),
])

write("deploy", "f2b1c2d3-cccc-45d6-a7e8-00000000015c", "2026-09-18T08:50:00",
      "Cache pip in CI: 6 min to 1 min 40", [
    ("u", "CI takes 6 minutes per push and most of it looks like installing dependencies. Speed it up."),
    ("a", "Reading the step timings from the last run.", [
        ("Bash", bash("gh run view --log | grep -E '^(Install|Test|Start)' ", "Show step durations of the last CI run"),
         "Install dependencies   4m 12s\nStart postgres service  0m 21s\nTest                   1m 03s", False)]),
    ("a", "70% of the run is `pip install` compiling the same wheels every time. Caching pip, keyed on the hash of `requirements.txt`, turns it into a restore. The Postgres service container stays: tests hit a real database on purpose, since mocking it hid a migration bug last month.", [
        ("Edit", {"file_path": ".github/workflows/ci.yml"}, "The file .github/workflows/ci.yml has been updated.", False),
        ("Bash", bash("gh workflow run ci.yml && gh run watch", "Trigger CI and watch it"),
         "✓ Install dependencies   0m 14s (cache hit)\n✓ Start postgres service  0m 20s\n✓ Test                   1m 02s\nTotal 1m 41s", False)]),
    ("a", "CI went from **6 min to 1 min 41 s** on a cache hit. A cache miss (when requirements change) costs the old 4 minutes once, then it's cached again.", []),
])

print("demo corpus written to", ROOT)
