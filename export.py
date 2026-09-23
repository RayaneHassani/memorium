#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memorium — the memory of your Claude Code sessions, as a standalone static HTML site.

- Reads the JSONL files in ~/.claude/projects (override with MEMORIUM_PROJECTS_DIR).
- Writes index.html (dashboard, animated welcome, full-text search) + sessions/*.js + searchindex.js.
- Highlights, annotations; logical folder and session organisation
  through data/metadata.json — the JSONL source is NEVER modified.
- Zero dependencies: stdlib only. Offline-first output, no network call.

Usage:
    memorium                # export ~/.claude/projects → ./export, open the browser
    memorium <directory>    # custom output directory
    memorium serve          # serve ./export at http://localhost:8137 (unlocks writing)
    memorium archive        # incremental gzip backup of the JSONL files outside ~/.claude
    memorium restore <id>   # put an archived session back where Claude Code looks for it
    memorium init           # raise retention + install the archiving hook (with consent)
    python export.py        # same thing without installing
"""

import sys, os, json, re, html, glob, gzip, shutil, webbrowser, datetime

# Session source: ~/.claude/projects by default, overridable (demo, tests, CI)
PROJECTS_DIR = os.environ.get("MEMORIUM_PROJECTS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Raw JSONL archive: outside ~/.claude, which Claude Code prunes past cleanupPeriodDays.
ARCHIVE_DIR = os.environ.get("MEMORIUM_ARCHIVE_DIR") or os.path.join(os.path.expanduser("~"), ".memorium", "archive")

# ─────────────────────────── Session discovery ───────────────────────────

def scan_meta(path):
    """Pull (title, prompt count) from a transcript using a cheap substring prefilter."""
    title, prompts = None, 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"ai-title"' in line:
                    try:
                        o = json.loads(line)
                        if o.get("type") == "ai-title" and o.get("aiTitle"):
                            title = o["aiTitle"]
                    except Exception:
                        pass
                elif '"type":"user"' in line or '"type": "user"' in line:
                    try:
                        o = json.loads(line)
                        if o.get("type") == "user" and isinstance(
                            o.get("message", {}).get("content"), str
                        ):
                            prompts += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return title, prompts


def clean_input(s):
    """Strip whitespace and BOM/zero-width characters (pipe artefacts) at both ends."""
    return re.sub(r'^[\s﻿​]+|[\s﻿​]+$', '', s)


def discover_sessions(deep=50):
    """List sessions sorted by date; deep-scan (title + prompt count) the `deep` most recent ones."""
    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    paths.sort(key=os.path.getmtime, reverse=True)  # cheap sort first
    sessions = []
    for i, path in enumerate(paths):
        proj = os.path.basename(os.path.dirname(path))
        sid = os.path.splitext(os.path.basename(path))[0]
        if i < deep:
            title, prompts = scan_meta(path)
        else:
            title, prompts = None, None
        sessions.append({
            "path": path,
            "project": pretty_project(proj),
            "sid": sid,
            "title": title or "(untitled)",
            "prompts": prompts,
            "mtime": os.path.getmtime(path),
        })
    return sessions


def pretty_project(encoded):
    """C--Users-me-Desktop-...-portfolio -> portfolio (last segment)."""
    parts = encoded.replace("C--", "").split("-")
    parts = [p for p in parts if p]
    return parts[-1] if parts else encoded


def choose_session(sessions):
    if not sessions:
        print("No session found in", PROJECTS_DIR)
        sys.exit(1)
    print("\n  Available conversations:\n")
    show = sessions[:40]
    for i, s in enumerate(show, 1):
        d = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%d %b %H:%M")
        p = f"{s['prompts']:>3}p" if s["prompts"] is not None else "  ·"
        print(f"  [{i:>2}] {s['project']:<14} {d:<13} {p}  {s['title'][:50]}")
    print()
    while True:
        raw = clean_input(input("  Number to export (q to quit): "))
        if raw.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(show):
            return show[int(raw) - 1]
        print("  Invalid choice.")


def resolve_arg(arg):
    """Argument is either a .jsonl path or a session id (looked up in the projects)."""
    if os.path.isfile(arg):
        return arg
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{arg}*.jsonl"))
    if matches:
        return matches[0]
    print(f"Session not found: {arg}")
    sys.exit(1)

# ─────────────────────────── Transcript parsing ───────────────────────────

TAG_STRIP = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>"
    r"|<local-command-stderr>.*?</local-command-stderr>"
    r"|<local-command-caveat>.*?</local-command-caveat>",
    re.DOTALL,
)

CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def clean_prompt(text):
    # Command invocation: the prompt is nothing but tags, the typed command is the only real content.
    m = CMD_NAME.search(text)
    if m:
        cmd = m.group(1).strip()
        a = CMD_ARGS.search(text)
        args = a.group(1).strip() if a else ""
        if cmd:
            return (cmd + " " + args).strip()
    return TAG_STRIP.sub("", text).strip()


def prompt_title(cleaned):
    """Section title: first meaningful line of the prompt, cleaned and truncated."""
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^#{1,6}\s+", "", s)          # titre markdown
        s = re.sub(r"^[-*+]\s+", "", s)            # puce
        s = re.sub(r"[`*_>]", "", s)               # ponctuation md
        s = s.strip()
        if not s:
            continue
        return (s[:72] + "…") if len(s) > 72 else s
    return "Prompt"


def result_text(block):
    """Extract the text of a tool_result (string content or list of text blocks)."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for x in c:
            if isinstance(x, dict) and x.get("type") == "text":
                out.append(x.get("text", ""))
        return "\n".join(out)
    return ""


def parse(path):
    raw = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except Exception:
                pass

    # Page title = ai-title (stable across the session; it does NOT title the sections).
    page_title = None
    for o in raw:
        if o.get("type") == "ai-title" and o.get("aiTitle"):
            page_title = o["aiTitle"]

    # Session date = last activity recorded in the JSONL: the file mtime is reset
    # to "now" by a clone, a copy or a restore.
    stamps = [t for t in (parse_ts(o.get("timestamp")) for o in raw) if t is not None]

    # Session working directory: `claude --resume` is scoped to the cwd, the id alone is not enough.
    # The encoded project folder name is lossy (ambiguous dashes); only this field gives the real path.
    cwd = ""
    for o in raw:
        if o.get("cwd"):
            cwd = o["cwd"]
            break

    # map tool_use_id -> result text
    results = {}
    for o in raw:
        if o.get("type") != "user":
            continue
        c = o.get("message", {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    rid = b.get("tool_use_id")
                    if rid:
                        results[rid] = {
                            "text": result_text(b),
                            "is_error": bool(b.get("is_error")),
                        }

    sections = []      # {title, prompt, blocks:[...]}
    cur = None

    def ensure_section(title, prompt):
        nonlocal cur
        cur = {"title": title, "prompt": prompt, "blocks": []}
        sections.append(cur)

    for i, o in enumerate(raw):
        t = o.get("type")
        if t == "user":
            c = o.get("message", {}).get("content")
            if isinstance(c, str):
                cleaned = clean_prompt(c)
                if not cleaned:
                    continue
                if cleaned.startswith("[Request interrupted"):
                    if cur:
                        cur["blocks"].append({"kind": "interrupt", "text": cleaned})
                    continue
                ensure_section("", cleaned)
            # user-list entries are tool_result blocks, already consumed through `results`
        elif t == "assistant":
            if cur is None:
                # assistant reply before any prompt (rare) — opening section
                ensure_section("", "")
            c = o.get("message", {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text", "").strip()
                    if txt:
                        cur["blocks"].append({"kind": "text", "text": txt})
                # thinking blocks: deliberately excluded
                elif bt == "tool_use":
                    cur["blocks"].append(render_tool(b, results))

    return {
        "title": page_title or "Claude Code conversation",
        "cwd": cwd,
        "ts": max(stamps) if stamps else None,
        "sections": [s for s in sections if s["blocks"] or s["prompt"]],
    }


def parse_ts(s):
    """ISO 8601 -> epoch seconds, None if absent or malformed."""
    try:
        # fromisoformat only accepts the "Z" suffix from Python 3.11 on
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None

# ─────────────────────────── Tool formatting ───────────────────────────

NOISE_TOOLS = {"TaskCreate", "TaskUpdate", "TodoWrite", "TaskList", "TaskGet",
               "TaskOutput", "TaskStop", "ToolSearch"}

def basename(p):
    return os.path.basename(p) if p else "?"

def count_lines(s):
    return len([l for l in s.splitlines()]) if s else 0


def render_tool(b, results):
    name = b.get("name", "")
    inp = b.get("input") or {}
    rid = b.get("id")
    res = results.get(rid, {})
    rtext = res.get("text", "")
    err = res.get("is_error")

    if name in NOISE_TOOLS:
        return {"kind": "skip"}

    # Code edits: HEADER ONLY, never the content
    if name in ("Edit", "MultiEdit"):
        return {"kind": "tool", "icon": "✎", "label": f"edited {basename(inp.get('file_path'))}"}
    if name == "Write":
        return {"kind": "tool", "icon": "✚", "label": f"created {basename(inp.get('file_path'))}"}
    if name == "NotebookEdit":
        return {"kind": "tool", "icon": "✎", "label": f"edited notebook {basename(inp.get('notebook_path'))}"}

    # Reads and searches: header only (they return code)
    if name == "Read":
        return {"kind": "tool", "icon": "▤", "label": f"read {basename(inp.get('file_path'))}"}
    if name == "Grep":
        n = count_lines(rtext)
        pat = inp.get("pattern", "")
        return {"kind": "tool", "icon": "⌕", "label": f"searched “{pat}” — {n} result{'s' if n != 1 else ''}"}
    if name == "Glob":
        return {"kind": "tool", "icon": "⌕", "label": f"listed {inp.get('pattern','')}"}

    # Bash: command + output (collapsed when long)
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        desc = (inp.get("description") or "").strip()
        return {"kind": "bash", "cmd": cmd, "desc": desc, "out": rtext, "err": err}

    # Agent / delegation
    if name in ("Agent", "Task"):
        sub = inp.get("subagent_type", "agent")
        desc = inp.get("description", "")
        return {"kind": "agent", "label": f"delegated to the {sub} agent: {desc}", "out": rtext}

    # Plan
    if name == "ExitPlanMode":
        return {"kind": "plan", "text": inp.get("plan", "")}

    # Questions asked to the user
    if name == "AskUserQuestion":
        qs = inp.get("questions", [])
        return {"kind": "ask", "questions": qs, "answer": rtext}

    # Doc / web / MCP: generic header
    if name.startswith("mcp__") or name in ("WebFetch", "WebSearch"):
        q = inp.get("query") or inp.get("url") or inp.get("libraryName") or ""
        short = name.split("__")[-1]
        return {"kind": "tool", "icon": "◷", "label": f"{short} {q}".strip()}

    # Generic fallback
    return {"kind": "tool", "icon": "•", "label": f"used {name}"}

# ─────────────────────────── Markdown -> HTML (mini) ───────────────────────────

def esc(s):
    return html.escape(s, quote=False)

def inline_md(s):
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank">\1</a>', s)
    return s

def md_to_html(text):
    """Minimal but sturdy Markdown converter (headings, lists, code, quotes)."""
    lines = text.split("\n")
    out = []
    i = 0
    in_ul = in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul: out.append("</ul>"); in_ul = False
        if in_ol: out.append("</ol>"); in_ol = False

    while i < len(lines):
        ln = lines[i]
        # fenced code block
        if ln.lstrip().startswith("```"):
            close_lists()
            lang = ln.lstrip()[3:].strip()
            buf = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            cls = f' class="lang-{esc(lang)}"' if lang else ""
            out.append(f'<pre class="code"><code{cls}>{esc(chr(10).join(buf))}</code></pre>')
            continue
        # GFM table: a pipe row followed by a separator row (|---|---|)
        if "|" in ln and i + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", lines[i + 1]
        ):
            close_lists()
            def cells(row):
                row = row.strip()
                if row.startswith("|"): row = row[1:]
                if row.endswith("|"): row = row[:-1]
                return [c.strip() for c in row.split("|")]
            aligns = []
            for spec in cells(lines[i + 1]):
                l, r = spec.startswith(":"), spec.endswith(":")
                aligns.append(' style="text-align:center"' if l and r
                              else ' style="text-align:right"' if r
                              else ' style="text-align:left"' if l else "")
            head = cells(ln)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                body.append(cells(lines[i])); i += 1
            def row_html(tag, vals):
                return "".join(
                    f"<{tag}{aligns[j] if j < len(aligns) else ''}>{inline_md(v)}</{tag}>"
                    for j, v in enumerate(vals))
            thead = f"<thead><tr>{row_html('th', head)}</tr></thead>"
            tbody = "".join(f"<tr>{row_html('td', r)}</tr>" for r in body)
            out.append(f'<div class="tbl-wrap"><table>{thead}<tbody>{tbody}</tbody></table></div>')
            continue
        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            close_lists()
            lvl = min(len(m.group(1)) + 2, 6)  # shift down: # becomes h3 (h1/h2 belong to the page)
            out.append(f"<h{lvl}>{inline_md(m.group(2).strip())}</h{lvl}>")
            i += 1; continue
        # blockquote
        if ln.startswith(">"):
            close_lists()
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i][1:].lstrip()); i += 1
            out.append(f"<blockquote>{inline_md(chr(10).join(buf))}</blockquote>")
            continue
        # ordered list
        m = re.match(r"^\s*\d+\.\s+(.*)$", ln)
        if m:
            if not in_ol: close_lists(); out.append("<ol>"); in_ol = True
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1; continue
        # bullet list
        m = re.match(r"^\s*[-*+]\s+(.*)$", ln)
        if m:
            if not in_ul: close_lists(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1; continue
        # horizontal rule
        if re.match(r"^\s*---+\s*$", ln):
            close_lists(); out.append("<hr>"); i += 1; continue
        # blank line
        if not ln.strip():
            close_lists(); i += 1; continue
        # paragraph (merges consecutive lines)
        close_lists()
        buf = [ln]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|>|\s*[-*+]\s|\s*\d+\.\s|```|\s*---+\s*$)", lines[i]
        ):
            buf.append(lines[i]); i += 1
        out.append(f"<p>{inline_md(' '.join(buf))}</p>")
    close_lists()
    return "\n".join(out)

# ─────────────────────────── HTML rendering ───────────────────────────

def slug(i):
    return f"prompt-{i}"

def render_blocks(blocks, sidx):
    """Render the blocks of a section. Text containers get a data-bid
    (annotatable: client-side highlights and comments)."""
    parts = []
    bc = [0]
    def bid():
        bc[0] += 1
        return f"s{sidx}-b{bc[0]}"

    for b in blocks:
        k = b["kind"]
        if k == "skip":
            continue
        elif k == "text":
            parts.append(f'<div class="prose anno" data-bid="{bid()}">{md_to_html(b["text"])}</div>')
        elif k == "tool":
            parts.append(
                f'<div class="tool"><span class="tool-icon">{esc(b["icon"])}</span>'
                f'<span class="tool-label">{esc(b["label"])}</span></div>'
            )
        elif k == "interrupt":
            parts.append(f'<div class="interrupt">⛌ {esc(b["text"])}</div>')
        elif k == "bash":
            head = esc(b["cmd"]) if b["cmd"] else esc(b.get("desc", ""))
            out = b.get("out", "")
            err = " err" if b.get("err") else ""
            if out:
                n = count_lines(out)
                parts.append(
                    f'<div class="bash{err}"><details><summary class="term-cmd"><span class="ps">$</span>'
                    f'<span class="ctext">{head}</span><span class="lc">{n} line{"" if n == 1 else "s"}</span></summary>'
                    f'<div class="term-out anno" data-bid="{bid()}">{esc(out)}</div></details></div>'
                )
            else:
                parts.append(
                    f'<div class="bash{err}"><div class="term-cmd"><span class="ps">$</span>'
                    f'<span class="ctext">{head}</span></div></div>'
                )
        elif k == "agent":
            inner = md_to_html(b["out"]) if b.get("out") else ""
            parts.append(
                f'<details class="agent"><summary>{esc(b["label"])}</summary>'
                f'<div class="prose anno" data-bid="{bid()}">{inner}</div></details>'
            )
        elif k == "plan":
            parts.append(
                f'<div class="plan"><div class="plan-head">Proposed plan</div>'
                f'<div class="prose anno" data-bid="{bid()}">{md_to_html(b["text"])}</div></div>'
            )
        elif k == "ask":
            qhtml = []
            for q in b.get("questions", []):
                opts = "".join(
                    f'<li><b>{esc(o.get("label",""))}</b> — {esc(o.get("description",""))}</li>'
                    for o in q.get("options", [])
                )
                qhtml.append(
                    f'<div class="q"><div class="q-text">{esc(q.get("question",""))}</div>'
                    f'<ul class="q-opts">{opts}</ul></div>'
                )
            ans = esc(b.get("answer", "")[:1200])
            parts.append(
                '<details class="ask" open><summary>Questions asked</summary>'
                + "".join(qhtml)
                + (f'<div class="q-answer anno" data-bid="{bid()}">{ans}</div>' if ans else "")
                + "</details>"
            )
    return "\n".join(parts)


def render_session_inner(data, date_str):
    """Render the BODY of a session: header + inner TOC + sections.
    Returned as an HTML string (mounted dynamically in the viewer)."""
    title = data["title"]
    sections = data["sections"]

    toc = []
    body = []
    for idx, s in enumerate(sections, 1):
        sid = slug(idx)
        toc.append(
            f'<li><a href="#{sid}" data-target="{sid}">'
            f'<span class="n">{idx:02d}</span><span class="t">Prompt {idx}</span></a></li>'
        )
        prompt_html = ""
        if s["prompt"]:
            prompt_html = (
                f'<div class="prompt"><div class="prompt-tag">Prompt {idx}</div>'
                f'<div class="prompt-body anno" data-bid="s{idx}-p">{md_to_html(s["prompt"])}</div></div>'
            )
        body.append(
            f'<section id="{sid}" class="exchange" data-idx="{idx}">'
            f'<div class="exchange-n">{idx:02d}</div>'
            f'<div class="col-read">{prompt_html}'
            f'<div class="answer">{render_blocks(s["blocks"], idx)}</div></div>'
            f'<div class="gutter"></div>'
            f'</section>'
        )

    head = (
        f'<div class="gridrow dochead">'
        f'<div class="col-read"><h1>{esc(title)}</h1>'
        f'<div class="sub">{len(sections)} prompts · {esc(date_str)}</div>'
        f'<div class="accent-rule"></div></div></div>'
    )
    return {
        "toc": "\n".join(toc),
        "html": head + "\n" + "\n".join(body),
        "count": len(sections),
    }

# ─────────────────────────── Template (inline CSS) ───────────────────────────

INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memorium</title>
<link rel="icon" href="__FAVICON__">
<style>
  /* Fraunces, Atkinson Hyperlegible Next, JetBrains Mono: SIL Open Font License 1.1 */
  @font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url(data:font/woff2;base64,__FONT_DISPLAY__) format("woff2");}
  @font-face{font-family:"Atkinson Hyperlegible Next";font-weight:400 700;font-display:swap;src:url(data:font/woff2;base64,__FONT_TEXT__) format("woff2");}
  @font-face{font-family:"JetBrains Mono";font-weight:400 700;font-display:swap;src:url(data:font/woff2;base64,__FONT_MONO__) format("woff2");}
  :root{
    --paper:#F6EDDC; --panel:#E5DAC0; --ink:#20241A; --muted:#69604E;
    --accent:#8E9C63; --accent-d:#6E7A4B; --accent2:#A4AC86; --accent2-l:#C2C9A8;
    --line:#DBCFB4; --code-bg:#E2D9C4;
    --term-bg:#20241A; --term-ink:#E9E3D7; --term-green:#A4AC86; --term-amber:#E2B05E;
    --hl1:rgba(142,156,99,.34); --hl2:rgba(127,176,105,.36); --hl3:rgba(96,150,200,.32);
    --hl4:rgba(222,176,70,.42); --hl5:rgba(170,120,200,.32);
    --sw1:#8E9C63; --sw2:#7FB069; --sw3:#5E96C8; --sw4:#DEB046; --sw5:#AA78C8;
    --white:#fff; --paper-2:#F2EAD8; --on-dark:#eee; --on-dark-dim:#cfc9bf;
    --olive-dd:#566340; --olive-ddd:#475434; --term-line:#343D28; --term-edge:#171B10;
    --fs-1:12px; --fs-2:13px; --fs-3:15px; --fs-4:17px; --fs-5:18px; --fs-6:22px; --fs-7:34px; --fs-display:52px;
    --fs-read:15px; --lh-read:1.55; --fs-code:14px;
    /* 1 caption · 2 meta · 3 UI · read (transcripts, Manual) · 4 Organize empty/folder · 5 lead · 6 section · 7 page · display */
    --r-1:4px; --r-2:8px; --r-3:14px; --r-pill:999px;
    --nav-h:54px;
    --display:"Fraunces",Georgia,serif;
    --text:"Atkinson Hyperlegible Next",system-ui,-apple-system,"Segoe UI",sans-serif;
    --mono:"JetBrains Mono",ui-monospace,"Cascadia Code","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--text);
    font-size:var(--fs-5);line-height:1.65;-webkit-font-smoothing:antialiased;}
  /* Code is shown as typed: no programming ligatures (-> stays two characters) */
  code,pre,kbd,.term-cmd,.term-out{font-variant-ligatures:none;}
  /* Only Fraunces 600 is embedded: display text must not ask for another weight */
  h1,.man h2,.exchange-n,.nav-brand{font-family:var(--display);}
  kbd,.tool,.sess .sm,.sess-toc a .n,.prompt-tag,.dirpill{font-family:var(--mono);}
  ::selection{background:rgba(142,156,99,.24);}

  /* Buttons: two variants. Toolbar buttons on dark surfaces are a separate context, not a variant. */
  #ed-newfolder,.idbtn,.idcopy,#notepop .save,.enter-btn{
    background:var(--accent);color:var(--white);border:1px solid var(--accent);border-radius:var(--r-2);
    font-weight:600;cursor:pointer;}
  #ed-newfolder:hover,.idbtn:hover,.idcopy:hover,#notepop .save:hover,.enter-btn:hover{
    background:var(--accent-d);border-color:var(--accent-d);}
  .ed-del,#notepop button,.nb-etop button,.nb-toolbar button{
    background:var(--white);color:var(--muted);border:1px solid var(--line);border-radius:var(--r-2);cursor:pointer;}
  .ed-del:hover,#notepop button:hover,.nb-etop button:hover,.nb-toolbar button:hover{color:var(--ink);border-color:var(--muted);}
  #seltools .cmt,#pager button,#findbar button{
    background:none;border:none;color:var(--on-dark);cursor:pointer;}
  #seltools .cmt:hover,#pager button:hover,#findbar button:hover{background:var(--olive-ddd);color:var(--accent2-l);}

  /* ───── Navbar ───── */
  nav#topnav{position:fixed;top:0;left:0;right:0;height:var(--nav-h);z-index:80;
    display:flex;align-items:center;gap:20px;padding:0 22px;
    background:rgba(246,237,220,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);}
  .nav-brand{display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:600;font-size:var(--fs-5);letter-spacing:-.01em;}
  /* Memorium mark: rippling ribbon (curve + arrow + 2 alternating nodes), SVG */
  .brand-svg{display:inline-block;vertical-align:middle;}
  .nav-brand .brand-svg{width:42px;height:auto;}
  .nav-brand:hover{color:var(--accent-d);}
  .nav-tabs{display:flex;gap:4px;margin-left:auto;}
  .nav-tabs button{font-size:var(--fs-3);border:1px solid transparent;background:none;color:var(--muted);
    border-radius:var(--r-2);padding:6px 14px;cursor:pointer;letter-spacing:.02em;}
  .nav-tabs button:hover{background:var(--panel);color:var(--ink);}
  .nav-tabs button.active{background:var(--white);border-color:var(--line);color:var(--accent-d);font-weight:700;}
  .dirpill{font-size:var(--fs-2);margin-left:12px;padding:4px 10px;border-radius:var(--r-pill);
    border:1px solid var(--line);color:var(--muted);cursor:pointer;white-space:nowrap;}
  .dirpill:hover{color:var(--ink);border-color:var(--muted);}
  .dirpill.on{color:var(--accent-d);border-color:var(--accent);cursor:default;}
  .dirpill.off{opacity:.6;cursor:not-allowed;}

  .view{display:none;padding-top:var(--nav-h);}
  .view.active{display:block;}

  /* ───── Dashboard ───── */
  .dash-hero{text-align:center;padding:64px 24px 30px;}
  .dash-hero h1{font-size:var(--fs-display);margin:0 0 10px;letter-spacing:-.02em;font-weight:600;}
  .dash-hero .brand-svg{width:66px;height:auto;margin-right:16px;}
  .dash-hero p{color:var(--muted);font-size:var(--fs-3);margin:0;}
  .dash-hero .accent-rule{height:3px;width:64px;background:var(--accent);margin:22px auto 0;border-radius:var(--r-1);}
  .dash-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(258px,1fr));gap:18px;
    max-width:1080px;margin:0 auto;padding:18px 24px 90px;}
  .pcard{background:var(--white);border:1px solid var(--line);border-radius:var(--r-3);padding:18px 18px 16px;cursor:pointer;
    transition:transform .13s,box-shadow .13s,border-color .13s;position:relative;overflow:hidden;}
  .pcard:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent);opacity:.85;}
  .pcard:hover{transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.09);border-color:var(--accent);}
  .pcard .pname{font-weight:700;font-size:var(--fs-5);margin:0 0 12px;padding-left:8px;line-height:1.3;word-break:break-word;}
  .pcard .pstats{display:flex;gap:16px;padding-left:8px;font-size:var(--fs-2);color:var(--muted);}
  .pcard .pstats b{color:var(--ink);font-size:var(--fs-6);font-weight:700;display:block;}
  .pcard .pnotes{position:absolute;top:14px;right:14px;background:var(--accent2);color:var(--white);font-size:var(--fs-1);
    border-radius:var(--r-pill);padding:2px 9px;font-weight:700;}

  /* ───── Organize ───── */
  .ed-wrap{max-width:880px;margin:0 auto;padding:34px 24px 110px;}
  .ed-head h1{font-size:var(--fs-7);margin:0 0 8px;font-weight:600;letter-spacing:-.01em;}
  .ed-head p{color:var(--muted);font-size:var(--fs-3);margin:0 0 8px;max-width:640px;line-height:1.55;}
  #ed-toolbar{margin:10px 0 22px;}
  #ed-newfolder{font-size:var(--fs-3);padding:8px 14px;}
  .ed-empty{color:var(--muted);font-size:var(--fs-4);line-height:1.7;padding:36px 0;text-align:center;}
  .ed-folder{border:1px solid var(--line);border-radius:var(--r-3);background:var(--panel);margin:0 0 16px;padding:12px 14px;transition:border-color .12s,background .12s,box-shadow .12s;}
  .ed-folder.drop{border-color:var(--accent);background:#F0E7D0;box-shadow:0 0 0 2px rgba(142,156,99,.18);}
  .ed-fhead{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
  .ed-fname{font-size:var(--fs-4);font-weight:700;color:var(--accent-d);background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:5px 9px;min-width:180px;}
  .ed-fname:focus{outline:none;border-color:var(--accent);}
  .ed-fcount{font-size:var(--fs-1);color:var(--muted);}
  .ed-del{margin-left:auto;padding:2px 9px;font-size:var(--fs-2);}
  .ed-sess{display:flex;flex-direction:column;gap:4px;}
  .ed-row{display:flex;align-items:center;gap:8px;background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:6px 9px;cursor:grab;}
  .ed-row.dragging{opacity:.4;}
  .ed-grip{color:var(--muted);font-size:var(--fs-3);}
  .ed-title{flex:1;font-size:var(--fs-3);color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .ed-move{font-size:var(--fs-2);border:1px solid var(--line);border-radius:var(--r-2);background:var(--panel);color:var(--muted);padding:2px 5px;max-width:140px;cursor:pointer;}
  .ed-move:hover{border-color:var(--accent);color:var(--ink);}
  .ed-ren{background:none;border:none;color:var(--muted);cursor:pointer;font-size:var(--fs-3);padding:2px 6px;border-radius:var(--r-1);}
  .ed-ren:hover{color:var(--accent-d);background:var(--panel);}
  .ed-drop-hint{font-size:var(--fs-2);color:var(--muted);font-style:italic;padding:9px;text-align:center;border:1px dashed var(--line);border-radius:var(--r-2);}
  .nb-empty{color:var(--muted);text-align:center;padding:70px 20px;line-height:1.8;}
  .nb-proj{margin:30px 0 0;}
  .nb-proj > h2{font-size:var(--fs-3);letter-spacing:.12em;text-transform:uppercase;color:var(--accent-d);font-weight:700;
    margin:0 0 4px;display:flex;align-items:baseline;gap:10px;}
  .nb-proj > h2 .c{font-size:var(--fs-1);color:var(--muted);font-weight:400;letter-spacing:0;text-transform:none;}
  .nb-sess{margin:14px 0 0;}
  .nb-sess > h3{font-size:var(--fs-3);font-weight:700;margin:0 0 7px;color:var(--ink);cursor:pointer;}
  .nb-sess > h3:hover{color:var(--accent-d);}
  .nb-item{display:flex;gap:11px;background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);
    padding:10px 13px;margin:0 0 8px;cursor:pointer;transition:.12s;}
  .nb-item:hover{border-color:var(--accent2);transform:translateX(-2px);box-shadow:0 2px 10px rgba(0,0,0,.05);}
  .nb-dot{flex:none;width:11px;height:11px;border-radius:var(--r-1);margin-top:5px;}
  .nb-body{min-width:0;}
  .nb-quote{font-size:var(--fs-2);color:var(--muted);border-left:2px solid var(--line);padding-left:9px;
    font-style:italic;word-break:break-word;}
  .nb-note{font-size:var(--fs-3);color:var(--ink);margin-top:5px;}
  .nb-source .nb-sess>h3,.nb-source .nb-item{cursor:grab;}
  .nb-source .nb-sess>h3:active,.nb-source .nb-item:active{cursor:grabbing;}

  .layout{display:grid;grid-template-columns:var(--side-w,300px) 1fr;}

  /* ───── Sidebar : projets → sessions ───── */
  aside#sidebar{position:sticky;top:var(--nav-h);align-self:start;height:calc(100vh - var(--nav-h));overflow:hidden;
    background:var(--panel);border-right:1px solid var(--line);}
  #side-inner{height:100%;overflow-y:auto;padding:18px 16px 60px;}
  #side-resizer{position:absolute;top:0;right:0;width:6px;height:100%;cursor:col-resize;z-index:5;transition:background .12s;}
  #side-resizer:hover,#side-resizer.drag{background:var(--accent);opacity:.4;}
  #search{width:100%;font-size:var(--fs-3);border:1px solid var(--line);border-radius:var(--r-2);
    padding:8px 11px;background:var(--white);color:var(--ink);margin-bottom:14px;}
  #search:focus{outline:none;border-color:var(--accent);}

  .proj{margin-bottom:4px;}
  .proj-head{display:flex;align-items:center;gap:7px;padding:6px 8px;cursor:pointer;border-radius:var(--r-2);
    font-size:var(--fs-3);letter-spacing:.04em;color:var(--accent-d);font-weight:700;text-transform:uppercase;}
  .proj-head:hover{background:var(--paper-2);}
  .proj-head .caret{transition:transform .15s;color:var(--muted);font-size:var(--fs-1);}
  .proj.collapsed .caret{transform:rotate(-90deg);}
  .proj-head .pc{margin-left:auto;font-size:var(--fs-1);color:var(--muted);font-weight:400;}
  .proj-sessions{padding:2px 0 6px;}
  .proj.collapsed .proj-sessions{display:none;}

  .sess{padding:7px 10px;border-radius:var(--r-2);cursor:pointer;border-left:2px solid transparent;margin:1px 0;}
  .sess:hover{background:var(--paper-2);}
  .sess.active{background:var(--white);border-left-color:var(--accent);}
  .sess .st{font-size:var(--fs-3);color:var(--ink);line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .sess.active .st{color:var(--ink);font-weight:600;}
  .sess .sm{font-size:var(--fs-2);color:var(--muted);margin-top:3px;display:flex;gap:8px;}
  .sess .sm .pr{color:var(--accent2);}
  .sess-toc{list-style:none;margin:4px 0 2px;padding:0 0 0 6px;border-left:1px dashed var(--line);}
  .sess-toc a{display:flex;gap:8px;align-items:baseline;padding:3px 8px;border-radius:var(--r-2);color:var(--muted);
    text-decoration:none;font-size:var(--fs-2);}
  .sess-toc a .n{color:var(--accent);font-size:var(--fs-1);min-width:18px;}
  .sess-toc a:hover{background:var(--paper-2);color:var(--ink);}
  .sess-toc a.active{color:var(--ink);font-weight:600;}

  /* ───── Zone principale (lecture) ───── */
  main{padding:0 0 200px;min-height:calc(100vh - var(--nav-h));position:relative;}
  #emptyread{display:flex;flex-direction:column;align-items:center;justify-content:center;height:calc(100vh - var(--nav-h));
    text-align:center;color:var(--muted);padding:40px;}
  #emptyread .big{font-size:var(--fs-6);color:var(--ink);margin-bottom:10px;font-weight:700;}
  #emptyread kbd{background:var(--white);border:1px solid var(--line);border-bottom-width:2px;border-radius:var(--r-1);padding:0 5px;font-size:var(--fs-2);}
  #viewer{padding:40px 0 0;}
  #viewer:empty{display:none;}

  .gridrow{display:grid;grid-template-columns:74px minmax(0,720px) 286px;column-gap:30px;justify-content:center;position:relative;}
  .dochead{margin:0 0 56px;}
  .dochead .col-read{grid-column:2;}
  .dochead h1{font-size:var(--fs-7);line-height:1.18;margin:0 0 12px;font-weight:600;letter-spacing:-.01em;}
  .dochead .sub{color:var(--muted);font-size:var(--fs-2);}
  .dochead .accent-rule{height:3px;width:60px;background:var(--accent);margin-top:18px;border-radius:var(--r-1);}
  button{font-family:inherit;}
  .man{max-width:760px;margin:0 auto;padding:34px 26px 100px;}
  .man h1{font-size:var(--fs-7);margin:0 0 10px;letter-spacing:-.01em;font-weight:600;}
  .man .lede{color:var(--muted);font-size:var(--fs-read);line-height:var(--lh-read);margin:0 0 6px;}
  .man .accent-rule{height:3px;width:60px;background:var(--accent);margin:18px 0 34px;border-radius:var(--r-1);}
  .man h2{font-size:var(--fs-6);font-weight:600;margin:38px 0 12px;padding-top:22px;border-top:1px solid var(--line);}
  .man h3{font-size:var(--fs-2);margin:22px 0 8px;color:var(--accent-d);text-transform:uppercase;letter-spacing:.08em;}
  .man p{font-size:var(--fs-read);line-height:var(--lh-read);margin:0 0 13px;}
  .man li{font-size:var(--fs-read);line-height:var(--lh-read);margin-bottom:7px;}
  .man .note{background:var(--white);border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:var(--r-2);padding:13px 17px;margin:18px 0;font-size:var(--fs-3);line-height:1.7;}
  .man .bash{margin:14px 0 6px;}
  .man .cmdrow{display:flex;align-items:center;gap:12px;margin:0 0 22px;}
  .man .cmdrow .idbtn{margin-left:0;}
  .idbtn{margin-left:12px;padding:5px 13px;font-size:var(--fs-2);vertical-align:1px;
    display:inline-flex;align-items:center;gap:7px;}
  .idbtn svg{width:13px;height:13px;flex:none;}
  .idcard{margin-top:14px;background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:13px 16px;max-width:720px;}
  .idrow{display:flex;gap:12px;font-size:var(--fs-2);padding:3px 0;align-items:baseline;}
  .idrow span{color:var(--muted);min-width:46px;flex:none;}
  .idrow b{font-weight:600;word-break:break-all;}
  .idrow b.mono{font-family:var(--mono);font-size:var(--fs-2);font-weight:500;}
  .idcopy{margin-top:10px;padding:6px 13px;font-size:var(--fs-2);}

  .exchange{display:grid;grid-template-columns:74px minmax(0,720px) 286px;column-gap:30px;justify-content:center;position:relative;margin:0 0 78px;}
  .exchange-n{grid-column:1;text-align:right;font-size:var(--fs-7);font-weight:600;color:var(--accent);
    line-height:1;padding-top:2px;position:sticky;top:calc(var(--nav-h) + 18px);align-self:start;letter-spacing:-.02em;}
  .col-read{grid-column:2;min-width:0;}
  .gutter{grid-column:3;position:relative;min-width:0;}

  .prompt{background:var(--white);border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:var(--r-2);padding:13px 17px;margin:0 0 26px;box-shadow:0 1px 2px rgba(0,0,0,.03);}
  .prompt-tag{font-size:var(--fs-1);letter-spacing:.16em;text-transform:uppercase;color:var(--accent-d);font-weight:700;margin-bottom:5px;}
  .prompt-body{font-size:var(--fs-read);line-height:var(--lh-read);}
  .prompt-body p{margin:.35em 0;}

  .prose p{margin:.95em 0;}
  .prose h3{font-size:var(--fs-6);margin:1.5em 0 .5em;font-weight:700;color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:4px;}
  .prose h4,.prose h5,.prose h6{font-size:var(--fs-read);margin:1.3em 0 .4em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-d);}
  .prose ul,.prose ol{padding-left:1.3em;margin:.7em 0;}
  .prose li{margin:.28em 0;}
  .prose li::marker{color:var(--accent2);}
  .prose blockquote{border-left:3px solid var(--accent2-l);margin:1em 0;padding:.1em 1em;color:var(--muted);}
  .prose hr{border:none;border-top:1px solid var(--line);margin:1.7em 0;}
  .prose a{color:var(--accent-d);text-underline-offset:2px;}
  .prose strong{color:var(--accent-d);font-weight:700;}
  code{font-family:var(--mono);font-size:.92em;background:var(--code-bg);padding:.08em .35em;border-radius:var(--r-1);border:1px solid #e4ddcc;}
  .prose strong code{color:var(--accent2);}
  pre.code{background:var(--term-bg);color:var(--term-ink);border-radius:var(--r-2);padding:13px 15px;overflow:auto;margin:1.1em 0;max-height:460px;}
  pre.code code{background:none;padding:0;border:none;font-size:var(--fs-code);line-height:1.55;color:var(--term-ink);}

  .tool{display:inline-flex;align-items:center;gap:7px;font-size:var(--fs-2);color:var(--muted);background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:3px 11px;margin:4px 5px 4px 0;}
  .tool-icon{color:var(--accent2);}

  .bash{margin:16px 0;border-radius:var(--r-2);overflow:hidden;border:1px solid var(--term-edge);box-shadow:0 2px 12px rgba(0,0,0,.13);font-size:var(--fs-code);}
  .term-cmd{display:flex;align-items:baseline;gap:9px;background:var(--term-edge);color:var(--term-ink);padding:8px 13px;font-family:var(--mono);}
  .term-cmd .ps{flex:none;width:1ch;color:var(--term-green);opacity:.55;user-select:none;}
  .bash.err .term-cmd .ps{color:var(--term-amber);opacity:.9;}
  .term-cmd .ctext{min-width:0;white-space:pre-wrap;word-break:break-word;}
  .term-cmd .lc{flex:none;margin-left:auto;padding-left:12px;font-size:var(--fs-1);color:var(--accent2-l);opacity:.6;}
  .bash summary.term-cmd{cursor:pointer;list-style:none;}
  .bash summary.term-cmd::-webkit-details-marker{display:none;}
  .bash summary.term-cmd::before{content:"▸";flex:none;color:var(--accent2-l);opacity:.6;}
  .bash details[open] > summary.term-cmd::before{content:"▾";}
  .bash details:not([open]) .ctext{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .term-out{background:var(--term-bg);color:var(--term-ink);padding:10px 13px;white-space:pre-wrap;word-break:break-word;line-height:1.5;font-family:var(--mono);}
  .bash.err .term-out{color:var(--term-amber);}

  details.agent{margin:13px 0;border:1px dashed var(--line);border-radius:var(--r-2);background:#FBFAF6;}
  details.agent summary{cursor:pointer;padding:9px 14px;font-size:var(--fs-2);color:var(--muted);}
  details.agent[open] summary{color:var(--accent-d);}
  details.agent .prose{padding:0 16px 12px;font-size:var(--fs-read);}
  .plan{margin:16px 0;border:1px solid var(--line);border-radius:var(--r-2);background:var(--white);overflow:hidden;}
  .plan-head{background:var(--panel);padding:7px 15px;font-size:var(--fs-1);letter-spacing:.1em;text-transform:uppercase;color:var(--accent-d);font-weight:700;}
  .plan .prose{padding:2px 16px 12px;}
  details.ask{margin:16px 0;border:1px solid var(--line);border-radius:var(--r-2);background:var(--white);overflow:hidden;}
  details.ask summary{cursor:pointer;padding:9px 14px;background:var(--panel);font-size:var(--fs-2);color:var(--accent-d);font-weight:700;}
  .ask .q{padding:7px 16px;border-top:1px solid var(--line);}
  .ask .q-text{font-weight:700;margin:6px 0;}
  .ask .q-opts{font-size:var(--fs-3);color:var(--muted);}
  .ask .q-answer{padding:9px 16px;font-size:var(--fs-2);color:var(--muted);border-top:1px solid var(--line);white-space:pre-wrap;}
  .interrupt{font-size:var(--fs-2);color:var(--accent-d);background:#FBEFEA;border-radius:var(--r-2);padding:5px 11px;margin:10px 0;display:inline-block;}

  mark.hl{background:var(--c,var(--hl1));color:inherit;border-radius:var(--r-1);padding:.02em 0;box-decoration-break:clone;-webkit-box-decoration-break:clone;cursor:pointer;}
  mark.hl.has-note{border-bottom:2px solid var(--accent);}
  mark.hl.flash{animation:flash 1.1s ease;}
  @keyframes flash{0%,100%{box-shadow:none}30%{box-shadow:0 0 0 3px var(--accent2-l)}}
  .gutter .cmt-card{position:absolute;left:0;right:6px;background:var(--white);border:1px solid var(--line);border-left:3px solid var(--accent2);border-radius:var(--r-2);padding:8px 10px;font-size:var(--fs-2);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:transform .12s,box-shadow .12s;}
  .gutter .cmt-card:hover{transform:translateX(-3px);box-shadow:0 3px 10px rgba(0,0,0,.10);}
  .cmt-quote{color:var(--muted);margin-bottom:3px;border-left:2px solid var(--line);padding-left:6px;}
  .cmt-note{color:var(--ink);}

  #seltools{position:fixed;z-index:90;display:none;gap:3px;align-items:center;background:var(--olive-dd);border-radius:var(--r-2);padding:5px;box-shadow:0 6px 24px rgba(0,0,0,.28);transform:translate(-50%,-118%);}
  #seltools .sw{width:17px;height:17px;border-radius:var(--r-1);cursor:pointer;border:1px solid rgba(255,255,255,.18);}
  #seltools .sw:hover{transform:scale(1.12);}
  #seltools .sep{width:1px;height:18px;background:#6B7A50;margin:0 3px;}
  #seltools .cmt{font-size:var(--fs-2);padding:4px 8px;border-radius:var(--r-2);}
  #notepop{position:fixed;z-index:95;display:none;width:266px;background:var(--white);border:1px solid var(--line);border-radius:var(--r-3);box-shadow:0 10px 30px rgba(0,0,0,.20);padding:12px;}
  #notepop .swatches{display:flex;gap:7px;margin-bottom:10px;}
  #notepop .swatches .sw{width:20px;height:20px;border-radius:var(--r-2);cursor:pointer;border:2px solid transparent;}
  #notepop .swatches .sw.on{border-color:var(--ink);box-shadow:0 0 0 1px var(--white) inset;}
  #notepop textarea{width:100%;min-height:70px;resize:vertical;font-size:var(--fs-3);border:1px solid var(--line);border-radius:var(--r-2);padding:8px;background:var(--paper);color:var(--ink);}
  #notepop .row{display:flex;justify-content:space-between;margin-top:9px;}
  #notepop button{font-size:var(--fs-2);padding:5px 11px;}

  #pager{position:fixed;right:22px;bottom:22px;z-index:75;display:none;align-items:center;gap:2px;background:var(--olive-dd);color:var(--on-dark);border-radius:var(--r-2);padding:5px;box-shadow:0 6px 22px rgba(0,0,0,.25);}
  #pager button{font-size:var(--fs-5);padding:4px 10px;border-radius:var(--r-2);}
  #pager .pos{font-size:var(--fs-2);color:var(--on-dark-dim);padding:0 4px;min-width:62px;text-align:center;}
  #pager .pos b{color:var(--accent);}
  #cbtn{position:fixed;right:22px;top:calc(var(--nav-h) + 14px);z-index:75;background:var(--olive-dd);color:var(--on-dark);border:none;font-size:var(--fs-2);border-radius:var(--r-2);padding:9px 13px;cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.25);display:none;align-items:center;gap:8px;}
  #cbtn:hover{background:var(--olive-ddd);}
  #cbtn .badge{background:var(--accent2);color:var(--white);border-radius:var(--r-pill);font-size:var(--fs-1);padding:1px 7px;min-width:20px;text-align:center;}
  #recap{position:fixed;top:0;right:0;z-index:85;width:340px;max-width:90vw;height:100vh;background:var(--panel);border-left:1px solid var(--line);box-shadow:-8px 0 30px rgba(0,0,0,.10);transform:translateX(100%);transition:transform .22s ease;display:flex;flex-direction:column;}
  #recap.open{transform:translateX(0);}
  #recap header{padding:18px 18px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;}
  #recap header h2{font-size:var(--fs-2);letter-spacing:.12em;text-transform:uppercase;margin:0;color:var(--accent-d);}
  #recap .close{background:none;border:none;font-size:var(--fs-6);cursor:pointer;color:var(--muted);}
  #recap-list{padding:12px;overflow-y:auto;flex:1;}
  #recap .empty{color:var(--muted);font-size:var(--fs-2);text-align:center;padding:40px 16px;line-height:1.7;}
  .recap-item{background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:11px 12px;margin-bottom:10px;cursor:pointer;transition:.12s;}
  .recap-item:hover{border-color:var(--accent2);transform:translateX(-2px);}
  .ri-head{font-size:var(--fs-1);letter-spacing:.1em;text-transform:uppercase;color:var(--accent2);font-weight:700;margin-bottom:5px;}
  .ri-quote{font-size:var(--fs-2);color:var(--muted);border-left:2px solid var(--line);padding-left:7px;margin-bottom:5px;}
  .ri-note{font-size:var(--fs-3);color:var(--ink);}

  @media (max-width:1180px){ .gridrow,.exchange{grid-template-columns:60px minmax(0,720px) 0;column-gap:20px;} .gutter{display:none;} }
  @media (max-width:860px){
    .layout{grid-template-columns:1fr;}
    aside#sidebar{position:static;height:auto;border-right:none;border-bottom:1px solid var(--line);}
    #side-inner{max-height:46vh;}
    #side-resizer{display:none;}
    .gridrow,.exchange{grid-template-columns:40px 1fr 0;column-gap:14px;}
    #viewer{padding:24px 16px 0;}
    .exchange-n{font-size:var(--fs-6);}
    #findbar{left:12px;right:12px;bottom:12px;}
  }

  /* ═══════ Transitions entre vues ═══════ */
  .view.active{animation:viewIn .42s cubic-bezier(.22,.7,.2,1) both;}
  @keyframes viewIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

  /* ═══════ Tableaux — rendus lisibles (fini les pipes bruts) ═══════ */
  .tbl-wrap{overflow-x:auto;margin:13px 0;border:1px solid var(--line);border-radius:var(--r-2);}
  .tbl-wrap table{border-collapse:collapse;width:100%;font-size:var(--fs-3);line-height:1.55;}
  .tbl-wrap th,.tbl-wrap td{padding:7px 13px;border-bottom:1px solid var(--line);
    border-right:1px solid var(--line);text-align:left;vertical-align:top;}
  .tbl-wrap thead th{background:rgba(142,156,99,.10);font-weight:700;white-space:nowrap;}
  .tbl-wrap tbody tr:last-child td{border-bottom:none;}
  .tbl-wrap th:last-child,.tbl-wrap td:last-child{border-right:none;}
  .tbl-wrap tbody tr:hover{background:rgba(0,0,0,.02);}
  .tbl-wrap code{white-space:nowrap;}

  /* ═══════ Welcome: title and search on the left, pen-drawn thread on the right ═══════ */
  .welcome{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:40px;align-items:center;
    max-width:1280px;margin:0 auto;min-height:calc(100vh - var(--nav-h));padding:40px 32px 56px;}
  .w-left{max-width:480px;}
  #wTitle{font-size:clamp(56px,7vw,96px);font-weight:600;line-height:1;letter-spacing:-.025em;color:var(--olive-dd);
    margin:0;min-height:1em;white-space:nowrap;}
  #wTitle:after{content:"";display:inline-block;width:.05em;height:.82em;margin-left:.06em;vertical-align:-.06em;
    background:var(--accent);animation:caret 1s step-end infinite;}
  #wTitle.typed:after{animation:none;opacity:0;}
  @keyframes caret{50%{opacity:0}}
  .wsub{font-size:var(--fs-5);line-height:1.5;color:var(--muted);margin:18px 0 30px;max-width:30ch;}
  .wsearch{display:flex;align-items:center;gap:10px;background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);
    padding:0 14px;height:48px;transition:border-color .15s,box-shadow .15s;}
  .wsearch:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(142,156,99,.18);}
  .wsearch .mag{color:var(--muted);font-size:var(--fs-5);line-height:1;}
  .wsearch input{flex:1;min-width:0;border:0;outline:0;background:none;font:var(--fs-3)/1 var(--mono);color:var(--ink);}
  .wsearch input::placeholder{font-family:var(--text);color:var(--muted);opacity:.75;}
  .wsearch kbd{font-size:var(--fs-1);line-height:1;color:var(--muted);border:1px solid var(--line);border-radius:var(--r-1);padding:4px 6px;}
  .enter-btn{margin-top:22px;display:inline-flex;align-items:center;gap:10px;font-size:var(--fs-3);padding:14px 22px;
    box-shadow:0 6px 18px rgba(110,122,75,.26);transition:transform .14s,background .14s,box-shadow .14s;}
  .enter-btn:hover{transform:translateY(-1px);box-shadow:0 10px 24px rgba(110,122,75,.32);}
  .enter-btn:active{transform:none;}
  .wnote{margin-top:26px;font:500 var(--fs-1)/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);}
  .w-fade{opacity:0;animation:upFade .7s forwards;}
  @keyframes upFade{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
  .w-right svg{display:block;width:100%;height:auto;overflow:visible;}
  .wlbl{font:400 var(--fs-1) var(--mono);paint-order:stroke;stroke:var(--paper);stroke-width:5px;stroke-linejoin:round;}
  .wlbl .h{fill:var(--accent-d);opacity:.85;}
  .wlbl .m{fill:var(--ink);}
  .wlbl.hit{font-weight:700;}
  .wlbl.hit .h{fill:var(--accent);opacity:1;}
  @media (max-width:900px){
    .welcome{grid-template-columns:1fr;gap:12px;padding:32px 16px 48px;min-height:0;}
    .w-left{max-width:none;}
  }
  @media (prefers-reduced-motion:reduce){
    #wTitle:after{animation:none;opacity:0;}
    .w-fade{animation:none;opacity:1;transform:none;}
    .enter-btn{transition:none;}
  }

  /* ═══════ Full-text search results ═══════ */
  .sr-head{font-size:var(--fs-1);letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
    font-weight:700;margin:2px 2px 11px;}
  .sr-none{color:var(--muted);font-size:var(--fs-2);text-align:center;padding:34px 12px;line-height:1.7;}
  .sresult{background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:10px 12px;margin:0 0 8px;
    cursor:pointer;transition:transform .12s,border-color .12s,box-shadow .12s;}
  .sresult:hover{border-color:var(--accent);transform:translateX(-2px);box-shadow:0 3px 12px rgba(0,0,0,.06);}
  .sresult .sr-title{font-size:var(--fs-2);font-weight:600;color:var(--ink);line-height:1.35;margin-bottom:4px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .sresult .sr-meta{font-size:var(--fs-1);color:var(--muted);display:flex;gap:9px;align-items:center;margin-bottom:5px;flex-wrap:wrap;}
  .sresult .sr-count{color:var(--accent-d);font-weight:700;}
  .sresult .sr-snip{font-size:var(--fs-2);color:var(--muted);line-height:1.55;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
  .sresult .sr-snip mark{background:var(--hl4);color:inherit;padding:0 1px;border-radius:var(--r-1);}

  /* ═══════ Find-bar (occurrences dans la session) ═══════ */
  mark.find{background:#FFE39A;color:inherit;border-radius:var(--r-1);padding:.02em 0;
    box-decoration-break:clone;-webkit-box-decoration-break:clone;}
  mark.find.cur{background:var(--accent);color:var(--white);}
  #findbar{position:fixed;left:320px;bottom:22px;z-index:78;display:none;align-items:center;gap:6px;
    background:var(--olive-dd);color:var(--on-dark);border-radius:var(--r-2);padding:6px 8px;box-shadow:0 6px 22px rgba(0,0,0,.25);
    font-size:var(--fs-2);}
  #findbar.show{display:flex;}
  #findbar .fq{max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--accent2-l);}
  #findbar button{font-size:var(--fs-5);padding:2px 8px;border-radius:var(--r-2);line-height:1;}
  #findbar .fpos{min-width:44px;text-align:center;color:var(--on-dark-dim);}
  #findbar .fpos b{color:var(--accent);}
  #findbar .fclose{font-size:var(--fs-3);}

  /* ═══════ Readability ═══════ */
  .prose{font-size:var(--fs-read);line-height:var(--lh-read);}
  .prose h3{margin-top:1.7em;}
</style>
</head>
<body>
<nav id="topnav">
  <div class="nav-brand" id="navHome"><svg class="brand-svg" viewBox="0 0 48 24" fill="none" aria-hidden="true"><path d="M3 12 C9 5.5, 15 18.5, 22 12 C29 5.5, 34 16.5, 40 12" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round"/><path d="M40 8.5 L45 12 L40 15.5" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M11 8.5 V6" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="11" cy="4.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/><path d="M30 14.5 V17.5" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="30" cy="19.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/></svg>Memorium</div>
  <div class="nav-tabs">
    <button data-view="dash" class="active">Dashboard</button>
    <button data-view="organize">Organize</button>
    <button data-view="manual">Manual</button>
  </div>
  <span class="dirpill" id="dirpill"></span>
</nav>

<section id="view-welcome" class="view active">
  <div class="welcome">
    <div class="w-left">
      <h1 id="wTitle"></h1>
      <p class="wsub w-fade" style="animation-delay:1.1s">The living memory of your Claude&nbsp;Code sessions &mdash; reread, search, annotate.</p>
      <form id="wform" class="w-fade" style="animation-delay:1.25s" autocomplete="off">
        <label class="wsearch"><span class="mag" aria-hidden="true">⌕</span>
          <input id="wq" type="search" placeholder="Search every session…" aria-label="Search every session">
          <kbd>Enter</kbd></label>
      </form>
      <button id="enterBtn" class="enter-btn w-fade" style="animation-delay:1.4s">Open the journal →</button>
      <div class="wnote w-fade" style="animation-delay:1.55s">__COUNT__ conversations indexed</div>
    </div>
    <div class="w-right" aria-hidden="true">
      <svg id="thread" viewBox="0 0 640 380">
        <defs>
          <mask id="reveal" maskUnits="userSpaceOnUse" x="-20" y="-20" width="680" height="420">
            <path id="maskPath" fill="none" stroke="#fff" stroke-width="46" stroke-linecap="round"/>
          </mask>
          <linearGradient id="beamGlow" x1="0" x2="1">
            <stop offset="0" style="stop-color:var(--accent);stop-opacity:0"/>
            <stop offset=".5" style="stop-color:var(--accent);stop-opacity:.26"/>
            <stop offset="1" style="stop-color:var(--accent);stop-opacity:0"/>
          </linearGradient>
        </defs>
        <g id="threadStroke" mask="url(#reveal)"><path id="threadBody" fill="var(--accent)"/></g>
        <path id="threadArrow" fill="none" stroke="var(--accent)" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round" opacity="0"/>
        <g id="threadNodes"></g>
        <g id="beam" opacity="0"><rect x="-22" y="16" width="44" height="348" fill="url(#beamGlow)"/><rect x="-1" y="16" width="2" height="348" rx="1" fill="var(--accent-d)"/></g>
      </svg>
    </div>
  </div>
</section>

<section id="view-dash" class="view">
  <div class="dash-hero">
    <h1><svg class="brand-svg" viewBox="0 0 48 24" fill="none" aria-hidden="true"><path d="M3 12 C9 5.5, 15 18.5, 22 12 C29 5.5, 34 16.5, 40 12" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round"/><path d="M40 8.5 L45 12 L40 15.5" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M11 8.5 V6" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="11" cy="4.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/><path d="M30 14.5 V17.5" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="30" cy="19.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/></svg>Memorium</h1>
    <p>__COUNT__ conversations · the memory of your Claude Code sessions</p>
    <div class="accent-rule"></div>
  </div>
  <div id="dash-grid" class="dash-grid"></div>
</section>

<section id="view-read" class="view">
  <div class="layout">
    <aside id="sidebar">
      <div id="side-resizer" title="Glisser pour redimensionner"></div>
      <div id="side-inner">
        <input id="search" placeholder="Search for a word or a phrase…" autocomplete="off">
        <div id="sesslist"></div>
      </div>
    </aside>
    <main>
      <div id="emptyread">
        <div class="big">Pick a session on the left</div>
        <div>Select text to <b>highlight</b> or <b>comment</b> · <kbd>j</kbd>/<kbd>k</kbd> moves between prompts.</div>
      </div>
      <div id="viewer"></div>
    </main>
  </div>
</section>

<section id="view-manual" class="view">
<div class="man">
  <h1>Manual</h1>
  <p class="lede">What Memorium does, how your sessions are stored, and how to bring one back into Claude Code.</p>
  <div class="accent-rule"></div>

  <h2>What Memorium is</h2>
  <p>Every time you work with Claude Code, the whole exchange is written to disk as a JSONL file &mdash; one line per event, prompts and tool calls included. Those files are the raw record of how your project was actually built: which options were weighed, which one was picked, and why. They are also unreadable in practice, scattered across encoded folder names, and deleted on a timer.</p>
  <p>Memorium turns that raw record into a static HTML journal you can read, search, annotate and keep. No server, no database, no account: one Python file reads the JSONL and writes a folder of HTML you can open anywhere.</p>
  <p>The point is not nostalgia. It is <b>provenance</b>. Six months from now, a line of code will make no sense to anyone, including you. The commit message will say <i>what</i> changed. Memorium is where you find <i>why</i> &mdash; the session where the trade-off was argued and settled.</p>

  <h2>Where your sessions live</h2>
  <p>Claude Code stores each session as a single file:</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>ls ~/.claude/projects/&lt;encoded-project-path&gt;/&lt;session-id&gt;.jsonl</div></div>
  <p>The folder name is the project path with separators replaced by dashes, which makes it ambiguous to read back &mdash; a dash in your folder name is indistinguishable from a separator. That is why Memorium reads the real working directory from inside the file instead of guessing it from the folder name.</p>
  <div class="note"><b>These files are deleted automatically.</b> Claude Code removes anything older than <code>cleanupPeriodDays</code>, which defaults to <b>30 days</b>. No warning, no recycle bin. A session you have not touched in a month is simply gone, and with it every decision it recorded.</div>

  <h2>Resume a session in Claude Code</h2>
  <p>Open any session in Memorium and click <b>Resume this session</b> under its title. The panel shows its name, date, working directory and full ID, and the button copies a two-line command to your clipboard. Paste it into a terminal:</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>cd "C:\Users\you\projects\your-project"</div><div class="term-cmd"><span class="ps">$</span>claude --resume 8f2c41ab-...</div></div>
  <p>You get the conversation back exactly where you left it: full history, full context, same working directory. Claude Code picks up as if you had never closed the window.</p>
  <h3>Why the first line matters</h3>
  <p><code>--resume</code> only looks for sessions belonging to the <b>current</b> directory. Run it from the wrong place and Claude Code will tell you the session does not exist, even though the ID is correct and the file is right there on disk. The <code>cd</code> is not decoration.</p>

  <h2>--continue is not --resume</h2>
  <p>Two flags, two different jobs, and the difference bites people who close an editor with several sessions open.</p>
  <p><code>claude --continue</code> takes no argument. It reopens <b>the most recent</b> conversation for the current directory &mdash; always the same one, no matter how many times you run it. If you were working across five parallel sessions, four of them are unreachable this way. They are not lost; they are simply not what that flag means.</p>
  <p><code>claude --resume &lt;id&gt;</code> targets one precise session. Running <code>claude --resume</code> with no ID opens a picker instead, which lists sessions by title &mdash; useful when you remember what you were doing, useless when five sessions share a vague title. That is the gap Memorium fills: full-text search across every transcript, then one click to copy the exact command for the one you found.</p>

  <h2>Stop losing sessions</h2>
  <p>The single most valuable thing you can do is raise the retention window before it deletes anything. In <code>~/.claude/settings.json</code>:</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>"cleanupPeriodDays": 3650</div></div>
  <p>Ten years instead of thirty days. This is a Claude Code setting, not a Memorium one &mdash; it works whether or not you use this tool, and it is the only thing that prevents deletion at the source. Everything else is a copy made after the fact.</p>

  <h2>Command line</h2>
  <p>Five commands, and they do not carry equal weight. <code>init</code> is the one that protects you; <code>archive</code> and <code>restore</code> are what save you when the machine itself is gone.</p>

  <h3>memorium init &mdash; start here</h3>
  <p>One command, run once. It raises <code>cleanupPeriodDays</code> so Claude Code stops deleting your sessions, and installs a <code>SessionEnd</code> hook so every conversation is archived the moment it ends. It prints exactly what it will write to your settings, backs up the file first, and asks before touching anything. Nothing to remember afterwards.</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>memorium init</div></div>
  <p>If you run only one command from this page, run this one. Raising the retention window is what actually prevents deletion; everything else is a copy made after the fact.</p>

  <h3>memorium archive</h3>
  <p>Copies the raw JSONL files out of <code>~/.claude/projects</code> into a compressed archive of your own, skipping anything already saved. Runs by itself once <code>init</code> is done; run it by hand any time you want.</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>memorium archive</div></div>
  <p>Why raw files and not the HTML: the journal lets you <i>read</i> a session, but Claude Code can only reopen the original file. A rendering is not a session.</p>

  <h3>memorium restore</h3>
  <p>Puts an archived session back where Claude Code expects it, so <code>claude --resume</code> works on it again.</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>memorium restore 8f2c41ab-...</div></div>
  <p>This is the one people misread. With <code>init</code> in place nothing gets purged, so restoring after a deletion becomes rare. What stays common is everything else: <b>a new machine, a reinstall, a dead disk, or simply a second computer.</b> Retention settings protect a folder on one machine; they do nothing when that machine is gone. Your archive travels, and this is what makes it usable again on the other side.</p>
  <p>It refuses to overwrite a session that still exists locally &mdash; the live file may be newer than the archive, and losing real work to a stale copy is worse than any purge. Pass <code>--force</code> when you know the archive is the version you want. An ID prefix is enough, like a commit hash.</p>

  <h3>memorium</h3>
  <p>Reads every session and writes the HTML journal to <code>./export</code>, then opens it in your browser.</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>memorium</div></div>

  <h3>memorium serve</h3>
  <p>Same build, then serves it on <code>http://localhost:8137</code>. Required for editing: renaming and moving sessions write to disk through the File System Access API, which browsers disable on <code>file://</code> pages. Reading works fine either way.</p>
  <div class="bash"><div class="term-cmd"><span class="ps">$</span>memorium serve</div></div>

  <h2>What Memorium never does</h2>
  <p>It never modifies your JSONL files. Renaming a session or moving it to another folder writes to a separate metadata layer inside the export, so Claude Code and any other tool reading those files keep working exactly as before. Delete the export folder and you lose annotations, not history.</p>
  <p>Nothing is uploaded anywhere. The export is plain files on your machine &mdash; which also means a transcript can contain paths, project names and secrets, so look before you share one.</p>
</div>
</section>
<section id="view-organize" class="view">
  <div class="ed-wrap">
    <div class="ed-head">
      <h1>Organize</h1>
      <p>Rename folders and sessions, drag a session into another folder. Nothing moves inside <code>~/.claude/projects</code> &mdash; everything is stored in <code>data/metadata.json</code>.</p>
      <div class="accent-rule"></div>
    </div>
    <div id="ed-toolbar"><button id="ed-newfolder">＋ New folder</button></div>
    <div id="ed-body"></div>
  </div>
</section>

<div id="seltools"></div>
<div id="notepop">
  <div class="swatches" id="note-swatches"></div>
  <textarea id="note-text" placeholder="Votre commentaire (facultatif)…"></textarea>
  <div class="row"><button class="del" id="note-del">Retirer</button><button class="save" id="note-save">Enregistrer</button></div>
</div>
<button id="cbtn">Commentaires <span class="badge" id="cbadge">0</span></button>
<aside id="recap">
  <header><h2>Commentaires (<span id="recap-count">0</span>)</h2><button class="close" id="recap-close">×</button></header>
  <div id="recap-list"></div>
</aside>
<div id="pager">
  <button id="prev">‹</button><span class="pos"><b id="pcur">01</b> / <span id="ptot">01</span></span><button id="next">›</button>
</div>
<div id="findbar">
  <span class="fq" id="findq"></span>
  <button id="findprev" title="Previous">‹</button>
  <span class="fpos"><b id="fcur">0</b>/<span id="ftot">0</span></span>
  <button id="findnext" title="Next">›</button>
  <button id="findclose" class="fclose" title="Close">✕</button>
</div>

<script src="searchindex.js"></script>
<script>
const MANIFEST = __MANIFEST__;
const cache = {};
let curSid=null, anns=[], pendingSid=null, curView="dash", curProject=null;
let META={folders:{},sessions:{}};   // overrides loaded from data/metadata.json (derived layer, never the source)
const viewer=document.getElementById("viewer");
const emptyread=document.getElementById("emptyread");

const HLVAR={"1":"var(--hl1)","2":"var(--hl2)","3":"var(--hl3)","4":"var(--hl4)","5":"var(--hl5)"};
const SWVAR={"1":"var(--sw1)","2":"var(--sw2)","3":"var(--sw3)","4":"var(--sw4)","5":"var(--sw5)"};
const COLORS=["1","2","3","4","5"];

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}
function keyFor(sid){return "claude-export::"+sid;}
function loadAnns(sid){try{return (JSON.parse(localStorage.getItem(keyFor(sid))||"{}").annotations)||[];}catch(e){return[];}}
function saveAnns(){if(curSid)localStorage.setItem(keyFor(curSid),JSON.stringify({annotations:anns}));}
function countNotes(sid){return loadAnns(sid).filter(a=>a.note).length;}
function countAnns(sid){return loadAnns(sid).length;}
function uid(){return "a"+Date.now().toString(36)+Math.random().toString(36).slice(2,6);}
function metaOf(sid){return MANIFEST.find(m=>m.sid===sid);}

/* Accessors : titre/dossier effectifs = override metadata sinon valeur d'origine */
function effTitle(m){const o=META.sessions[m.sid];return (o&&o.title)||m.title;}
function effFolder(m){const o=META.sessions[m.sid];return (o&&o.folder)||m.project;}
function folderName(id){const f=META.folders[id];return (f&&f.name)||id;}

/* ───── Store disque (File System Access API) ───── */
const DB_NAME="memorium-store", STORE="handles", HKEY="rootDir";
function idb(){return new Promise((res,rej)=>{const r=indexedDB.open(DB_NAME,1);
  r.onupgradeneeded=()=>r.result.createObjectStore(STORE);
  r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);});}
async function idbGet(k){const db=await idb();return new Promise((res,rej)=>{const t=db.transaction(STORE).objectStore(STORE).get(k);t.onsuccess=()=>res(t.result);t.onerror=()=>rej(t.error);});}
async function idbSet(k,v){const db=await idb();return new Promise((res,rej)=>{const tx=db.transaction(STORE,"readwrite");tx.objectStore(STORE).put(v,k);tx.oncomplete=()=>res();tx.onerror=()=>rej(tx.error);});}

let rootDir=null;                              // handle du dossier export/
const FS_OK=("showDirectoryPicker" in window); // faux sur Firefox/Safari

async function connectDir(){
  if(!FS_OK){alert("Disk writing is not supported — use Chrome or Edge.");return false;}
  rootDir=await window.showDirectoryPicker({mode:"readwrite"});
  await idbSet(HKEY,rootDir); updateDirPill(); await initStore(); return true;
}
async function restoreDir(){                    // on load: reuse the stored handle
  if(!FS_OK){updateDirPill();return;}
  const h=await idbGet(HKEY); if(!h){updateDirPill();return;}
  if(await h.queryPermission({mode:"readwrite"})==="granted"){rootDir=h;updateDirPill();await initStore();}
  else updateDirPill();                          // permission perdue → bouton "Reconnecter"
}
async function reconnectDir(){                   // ask for permission again (one gesture, required by the API)
  const h=await idbGet(HKEY); if(!h)return connectDir();
  if(await h.requestPermission({mode:"readwrite"})==="granted"){rootDir=h;updateDirPill();await initStore();return true;}
  return false;
}
async function dataDir(){ return rootDir?rootDir.getDirectoryHandle("data",{create:true}):null; }
async function readData(name){
  const d=await dataDir(); if(!d)return null;
  try{return await (await (await d.getFileHandle(name)).getFile()).text();}catch(e){return null;}
}
async function writeData(name,text){
  const d=await dataDir(); if(!d)throw new Error("Folder not connected");
  const w=await (await d.getFileHandle(name,{create:true})).createWritable();
  await w.write(text); await w.close();
}
async function initStore(){                      // load metadata.json into META, then refresh
  if(!rootDir)return;
  let meta=await readData("metadata.json");
  if(meta===null){await writeData("metadata.json","{}\n");meta="{}";}
  try{const j=JSON.parse(meta); META={folders:j.folders||{},sessions:j.sessions||{}};}
  catch(e){console.warn("metadata.json unreadable, ignored",e);}
  buildDashboard();
  if(curView==="read") buildSidebar("");
  if(curSid) document.querySelectorAll(".sess").forEach(e=>e.classList.toggle("active",e.dataset.sid===curSid));
  console.log("[memorium] store OK —",Object.keys(META.folders).length,"folders,",Object.keys(META.sessions).length,"sessions modified");
}
function updateDirPill(){
  const el=document.getElementById("dirpill"); if(!el)return;
  if(!FS_OK){el.textContent="Writing unavailable (Chrome/Edge)";el.className="dirpill off";el.onclick=null;return;}
  if(rootDir){el.textContent="● Folder connected";el.className="dirpill on";el.onclick=null;}
  else{el.textContent="○ Connect the export folder";el.className="dirpill";
       el.onclick=async()=>{(await idbGet(HKEY))?reconnectDir():connectDir();};}
}

/* ───── Navigation entre vues ───── */
function setView(v){
  curView=v;
  if(window.__heroSetActive)window.__heroSetActive(v==="welcome");
  document.querySelectorAll(".view").forEach(e=>e.classList.toggle("active",e.id==="view-"+v));
  document.querySelectorAll(".nav-tabs button").forEach(b=>b.classList.toggle("active",b.dataset.view===v));
  const reading = (v==="read" && curSid);
  document.getElementById("cbtn").style.display = reading?"flex":"none";
  document.getElementById("pager").style.display = reading?"flex":"none";
  if(v!=="read"){document.getElementById("recap").classList.remove("open");hideBar();closeNote();}
  if(v==="dash")buildDashboard();
  if(v==="organize")buildOrganize();
  window.scrollTo({top:0});
}
document.querySelectorAll(".nav-tabs button").forEach(b=>b.onclick=()=>setView(b.dataset.view));
document.getElementById("navHome").onclick=()=>setView("dash");

/* ───── Dashboard ───── */
function projectGroups(){
  const g={};
  MANIFEST.forEach(m=>{const f=effFolder(m);(g[f]=g[f]||[]).push(m);});
  return g;
}
function buildDashboard(){
  const g=projectGroups();
  const order=Object.keys(g).sort((a,b)=>Math.max(...g[b].map(s=>s.mtime))-Math.max(...g[a].map(s=>s.mtime)));
  const grid=document.getElementById("dash-grid"); grid.innerHTML="";
  order.forEach(proj=>{
    const sess=g[proj];
    const prompts=sess.reduce((t,s)=>t+(s.prompts||0),0);
    const notes=sess.reduce((t,s)=>t+countNotes(s.sid),0);
    const c=document.createElement("div"); c.className="pcard";
    c.innerHTML='<div class="pname">'+esc(folderName(proj))+'</div>'+
      '<div class="pstats"><div><b>'+sess.length+'</b>sessions</div><div><b>'+prompts+'</b>prompts</div></div>'+
      (notes?'<div class="pnotes">'+notes+' ✎</div>':'');
    c.onclick=()=>openProject(proj);
    grid.appendChild(c);
  });
}
function openProject(proj){
  curProject=proj;
  setView("read");
  buildSidebar("");
  const sess=(projectGroups()[proj]||[]).slice().sort((a,b)=>b.mtime-a.mtime);
  if(sess.length)openSession(sess[0].sid);
}

/* ───── Organize : dossiers & sessions (couche metadata.json) ───── */
async function saveMeta(){
  if(!rootDir){alert("Connect the export folder first (pill in the top right).");return false;}
  try{await writeData("metadata.json",JSON.stringify(META,null,2));return true;}
  catch(e){alert("Write failed: "+e.message);return false;}
}
function cleanupSession(sid){                    // drop the entry once no override is left
  const o=META.sessions[sid]; if(o&&!o.title&&!o.folder)delete META.sessions[sid];
}
function refreshAll(){                           // any META mutation affects all three views
  const s=document.getElementById("search");
  if(s.value){s.value="";closeFind();}           // editing resets the search context
  buildOrganize(); buildDashboard(); buildSidebar("");
}
async function moveSession(sid,id){              // move a session into folder `id`
  const m=metaOf(sid); if(!m||effFolder(m)===id)return;
  META.sessions[sid]=META.sessions[sid]||{};
  if(id===m.project)delete META.sessions[sid].folder; else META.sessions[sid].folder=id;  // back to the original folder = no override
  cleanupSession(sid);
  if(await saveMeta())refreshAll();
}
function allFolderIds(){
  const s=new Set();
  MANIFEST.forEach(m=>s.add(m.project));         // original project folders
  Object.keys(META.folders).forEach(id=>s.add(id));
  return [...s].filter(id=>!(META.folders[id]&&META.folders[id].deleted));  // hide emptied project folders that were removed
}
function buildOrganize(){
  const body=document.getElementById("ed-body"); body.innerHTML="";
  if(!rootDir){
    body.innerHTML='<div class="ed-empty">To organize your sessions, connect the <b>export</b> folder with the pill in the top right.<br>Changes are stored in <code>data/metadata.json</code>.</div>';
    return;
  }
  const groups=projectGroups();
  const ids=allFolderIds().sort((a,b)=>{
    const ca=a.startsWith("f_"), cb=b.startsWith("f_");
    if(ca!==cb)return ca?-1:1;                 // custom folders first
    if(ca)return a<b?1:-1;                      // custom folders: id ~ timestamp, newest first
    return folderName(a).localeCompare(folderName(b));  // project folders: alphabetical
  });
  ids.forEach(id=>{
    const sess=(groups[id]||[]).slice().sort((a,b)=>b.mtime-a.mtime);
    const custom=id.startsWith("f_");
    const card=document.createElement("div"); card.className="ed-folder"; card.dataset.folder=id;
    const head=document.createElement("div"); head.className="ed-fhead";
    const nameIn=document.createElement("input"); nameIn.className="ed-fname"; nameIn.value=folderName(id);
    nameIn.onchange=async()=>{const v=nameIn.value.trim(); if(!v){nameIn.value=folderName(id);return;}
      META.folders[id]=Object.assign(META.folders[id]||{},{name:v}); if(await saveMeta())refreshAll();};
    head.appendChild(nameIn);
    const cnt=document.createElement("span"); cnt.className="ed-fcount"; cnt.textContent=sess.length+" sess"; head.appendChild(cnt);
    if(sess.length===0){                          // suppression permise seulement si le dossier est vide
      const del=document.createElement("button"); del.className="ed-del"; del.textContent="✕";
      del.title=custom?"Delete this folder":"Remove this empty folder from the list";
      del.onclick=async()=>{
        if(custom)delete META.folders[id];
        else META.folders[id]=Object.assign(META.folders[id]||{},{deleted:true});
        if(await saveMeta())refreshAll();
      };
      head.appendChild(del);
    }
    card.appendChild(head);
    const listEl=document.createElement("div"); listEl.className="ed-sess";
    sess.forEach(m=>{
      const row=document.createElement("div"); row.className="ed-row"; row.draggable=true; row.dataset.sid=m.sid;
      row.innerHTML='<span class="ed-grip">⠿</span><span class="ed-title">'+esc(effTitle(m))+'</span>';
      row.ondragstart=e=>{e.dataTransfer.setData("text/sid",m.sid);e.dataTransfer.effectAllowed="move";row.classList.add("dragging");};
      row.ondragend=()=>row.classList.remove("dragging");
      const mv=document.createElement("select"); mv.className="ed-move";
      const ph=document.createElement("option"); ph.value=""; ph.textContent="⤳ Move to…"; ph.disabled=true; ph.selected=true; mv.appendChild(ph);
      allFolderIds().filter(fid=>fid!==id).sort((a,b)=>folderName(a).localeCompare(folderName(b))).forEach(fid=>{
        const op=document.createElement("option"); op.value=fid; op.textContent=folderName(fid); mv.appendChild(op);
      });
      mv.onmousedown=e=>e.stopPropagation();
      mv.onchange=()=>{ if(mv.value)moveSession(m.sid,mv.value); };
      row.appendChild(mv);
      const ren=document.createElement("button"); ren.className="ed-ren"; ren.textContent="✎"; ren.title="Rename session";
      ren.onmousedown=e=>e.stopPropagation();
      ren.onclick=async()=>{const v=prompt("New title:",effTitle(m)); if(v===null)return; const t=v.trim();
        META.sessions[m.sid]=META.sessions[m.sid]||{};
        if(t&&t!==m.title)META.sessions[m.sid].title=t; else delete META.sessions[m.sid].title;
        cleanupSession(m.sid); if(await saveMeta())refreshAll();};
      row.appendChild(ren);
      listEl.appendChild(row);
    });
    if(!sess.length){const e=document.createElement("div"); e.className="ed-drop-hint"; e.textContent="Drop sessions here"; listEl.appendChild(e);}
    card.appendChild(listEl);
    card.ondragover=e=>{e.preventDefault();card.classList.add("drop");};
    card.ondragleave=()=>card.classList.remove("drop");
    card.ondrop=e=>{e.preventDefault();card.classList.remove("drop");
      const sid=e.dataTransfer.getData("text/sid"); if(sid)moveSession(sid,id);};
    body.appendChild(card);
  });
}
document.getElementById("ed-newfolder").onclick=async()=>{
  const id="f_"+Date.now().toString(36)+Math.random().toString(36).slice(2,5);
  META.folders[id]={name:"New folder"};
  if(await saveMeta()){
    buildOrganize();
    const card=document.querySelector('.ed-folder[data-folder="'+id+'"]');
    if(card){card.scrollIntoView({block:"center"}); const inp=card.querySelector(".ed-fname"); if(inp){inp.focus();inp.select();}}
  }
};

/* ───── Sidebar ───── */
function buildSidebar(filter){
  filter=(filter||"").toLowerCase();
  const groups={};
  MANIFEST.forEach(m=>{
    if(filter && !(effTitle(m).toLowerCase().includes(filter)||folderName(effFolder(m)).toLowerCase().includes(filter)))return;
    const f=effFolder(m);(groups[f]=groups[f]||[]).push(m);
  });
  const order=Object.keys(groups).sort((a,b)=>Math.max(...groups[b].map(s=>s.mtime))-Math.max(...groups[a].map(s=>s.mtime)));
  const list=document.getElementById("sesslist");
  list.innerHTML="";
  order.forEach(proj=>{
    const sess=groups[proj].sort((a,b)=>b.mtime-a.mtime);
    const pd=document.createElement("div"); pd.className="proj"; pd.dataset.proj=proj;
    if(curProject && proj!==curProject) pd.classList.add("collapsed");
    const nNotes=sess.reduce((t,s)=>t+countNotes(s.sid),0);
    pd.innerHTML='<div class="proj-head"><span class="caret">▾</span>'+esc(folderName(proj))+
      '<span class="pc">'+sess.length+' sess'+(nNotes?' · '+nNotes+'✎':'')+'</span></div>';
    const wrap=document.createElement("div"); wrap.className="proj-sessions";
    sess.forEach(m=>{
      const el=document.createElement("div"); el.className="sess"; el.dataset.sid=m.sid;
      const nn=countNotes(m.sid);
      el.innerHTML='<div class="st">'+esc(effTitle(m))+'</div><div class="sm"><span class="pr">'+m.prompts+' prompts</span><span>'+m.date+'</span>'+(nn?'<span style="color:var(--accent)">'+nn+'✎</span>':'')+'</div>';
      el.onclick=()=>openSession(m.sid);
      wrap.appendChild(el);
    });
    pd.appendChild(wrap);
    pd.querySelector(".proj-head").onclick=()=>pd.classList.toggle("collapsed");
    list.appendChild(pd);
  });
}

/* ───── Chargement paresseux ───── */
window.__loadSession=function(sid,payload){cache[sid]=payload; if(pendingSid===sid)mount(sid);};
function copyText(t){                            // navigator.clipboard requires a secure context: missing on file://
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(t);
  const ta=document.createElement("textarea"); ta.value=t;
  ta.style.cssText="position:fixed;opacity:0"; document.body.appendChild(ta); ta.select();
  try{document.execCommand("copy");}finally{ta.remove();}
  return Promise.resolve();
}
function buildIdCard(sid){                       // session identity: everything needed to relaunch it in Claude Code
  const m=metaOf(sid); if(!m)return;
  const sub=viewer.querySelector(".sub"); if(!sub)return;
  const btn=document.createElement("button"); btn.className="idbtn"; btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/><path d="M3.2 4.2 V9.4 H8.4" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Resume this session</span>';
  btn.title="Session identity";
  const card=document.createElement("div"); card.className="idcard"; card.style.display="none";
  const cmd='cd "'+m.cwd+'"\nclaude --resume '+sid;
  card.innerHTML=
    '<div class="idrow"><span>Name</span><b>'+esc(effTitle(m))+'</b></div>'+
    '<div class="idrow"><span>Date</span><b>'+esc(m.date)+'</b></div>'+
    '<div class="idrow"><span>Directory</span><b class="mono">'+esc(m.cwd)+'</b></div>'+
    '<div class="idrow"><span>ID</span><b class="mono">'+esc(sid)+'</b></div>';
  const cp=document.createElement("button"); cp.className="idcopy"; cp.textContent="Copy resume command";
  cp.onclick=()=>{copyText(cmd); cp.textContent="✓ Copied"; setTimeout(()=>cp.textContent="Copy resume command",2000);};
  card.appendChild(cp);
  btn.onclick=()=>{card.style.display=card.style.display==="none"?"block":"none";};
  sub.appendChild(btn);
  sub.parentNode.insertBefore(card,sub.nextSibling);
}
function initManualCopy(){                       // every manual block gets the same copy button as the identity panel
  document.querySelectorAll("#view-manual .bash").forEach(b=>{
    const cmd=[...b.querySelectorAll(".term-cmd")].map(l=>l.textContent.replace(/^\$\s*/,"")).join("\n");
    const row=document.createElement("div"); row.className="cmdrow";
    const btn=document.createElement("button"); btn.className="idbtn"; btn.textContent="Copy";
    btn.onclick=()=>{copyText(cmd); btn.textContent="Copied"; setTimeout(()=>btn.textContent="Copy",2000);};
    b.parentNode.insertBefore(row,b.nextSibling); row.appendChild(btn);
  });
}
function openSession(sid){
  if(curView!=="read")setView("read");
  pendingSid=sid;
  emptyread.style.display="none";
  if(cache[sid]){mount(sid);return;}
  viewer.innerHTML='<div class="gridrow"><div class="col-read" style="grid-column:2;color:var(--muted);padding:40px 0">Loading…</div></div>';
  const s=document.createElement("script"); s.src="sessions/"+sid+".js";
  s.onerror=()=>{viewer.innerHTML='<div class="gridrow"><div class="col-read" style="grid-column:2;color:var(--accent-d);padding:40px 0">Could not load this session.</div></div>';};
  document.body.appendChild(s);
}

function mount(sid){
  curSid=sid;
  const p=cache[sid]; if(!p)return;
  emptyread.style.display="none";
  viewer.innerHTML=p.html;
  buildIdCard(sid);
  // The sidebar follows the open session: re-scope when arriving from another folder (e.g. a search result).
  const mo=metaOf(sid), proj=mo?effFolder(mo):null;
  if(proj && proj!==curProject){curProject=proj;buildSidebar("");}
  document.querySelectorAll(".sess").forEach(e=>e.classList.toggle("active",e.dataset.sid===sid));
  document.querySelectorAll(".sess-toc").forEach(e=>e.remove());
  const active=document.querySelector('.sess[data-sid="'+sid+'"]');
  if(active){const ol=document.createElement("ol"); ol.className="sess-toc"; ol.innerHTML=p.toc;
    active.appendChild(ol);
    ol.querySelectorAll("a").forEach(a=>a.onclick=ev=>{ev.preventDefault();ev.stopPropagation();const t=document.getElementById(a.dataset.target);if(t)t.scrollIntoView({behavior:"smooth",block:"start"});});
  }
  anns=loadAnns(sid);
  applyAllAnns();
  initPager();
  refresh();
  if(pendingFind){const q=pendingFind;pendingFind=null;setTimeout(()=>runFind(q),50);}else{closeFind();}
  document.getElementById("cbtn").style.display="flex";
  document.getElementById("pager").style.display="flex";
  window.scrollTo({top:0});
  history.replaceState(null,"","#"+sid);
}

function applyAllAnns(){
  const byBid={}; anns.forEach(a=>{(byBid[a.bid]=byBid[a.bid]||[]).push(a);});
  Object.entries(byBid).forEach(([bid,ls])=>{
    const cont=viewer.querySelector('[data-bid="'+CSS.escape(bid)+'"]'); if(!cont)return;
    ls.sort((a,b)=>a.start-b.start).forEach(a=>applyAnn(cont,a));
  });
}

/* ───── Surlignage / annotations ───── */
function textNodes(el){const o=[],w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null);let n;while(n=w.nextNode())o.push(n);return o;}
function offsetOf(cont,node,off){let t=0;for(const tn of textNodes(cont)){if(tn===node)return t+off;t+=tn.nodeValue.length;}return t;}
function applyAnn(cont,a){
  let pos=0;
  for(const tn of textNodes(cont)){
    const len=tn.nodeValue.length, lo=Math.max(a.start,pos), hi=Math.min(a.end,pos+len);
    if(lo<hi){
      const r=document.createRange(); r.setStart(tn,lo-pos); r.setEnd(tn,hi-pos);
      const m=document.createElement("mark");
      m.className="hl"+(a.note?" has-note":""); m.dataset.aid=a.id;
      m.style.setProperty("--c",HLVAR[a.color]||"var(--hl1)");
      try{r.surroundContents(m);}catch(e){}
    }
    pos+=len;
  }
}
function recolorAnn(id,color){
  const a=anns.find(x=>x.id===id); if(!a)return;
  a.color=color; saveAnns();
  viewer.querySelectorAll('mark.hl[data-aid="'+id+'"]').forEach(m=>m.style.setProperty("--c",HLVAR[color]||"var(--hl1)"));
}

let pending=null;
const bar=document.getElementById("seltools");
bar.innerHTML=COLORS.map(c=>'<span class="sw" data-color="'+c+'" style="background:'+SWVAR[c]+'" title="Highlight"></span>').join("")+
  '<span class="sep"></span><button class="cmt" data-act="cmt">✎ Comment</button>';
function hideBar(){bar.style.display="none";pending=null;}
function showBar(rect){bar.style.display="flex";bar.style.left=(rect.left+rect.width/2)+"px";bar.style.top=rect.top+"px";}
function annoOf(range){let n=range.commonAncestorContainer;if(n.nodeType===3)n=n.parentNode;return n.closest?n.closest(".anno"):null;}
document.addEventListener("mouseup",e=>{
  if(e.target.closest("#seltools")||e.target.closest("#notepop"))return;
  setTimeout(()=>{
    const sel=window.getSelection();
    if(!sel||sel.isCollapsed||!sel.toString().trim()){hideBar();return;}
    const r=sel.getRangeAt(0), cont=annoOf(r);
    if(!cont||!viewer.contains(cont)){hideBar();return;}
    const start=offsetOf(cont,r.startContainer,r.startOffset), end=offsetOf(cont,r.endContainer,r.endOffset);
    if(end<=start){hideBar();return;}
    pending={bid:cont.dataset.bid,start,end,quote:sel.toString().replace(/\s+/g," ").trim().slice(0,300)};
    showBar(r.getBoundingClientRect());
  },1);
});
bar.addEventListener("click",e=>{
  if(!pending)return;
  const swEl=e.target.closest(".sw"), btn=e.target.closest("button");
  if(swEl){createAnn(swEl.dataset.color,false);return;}
  if(btn&&btn.dataset.act==="cmt"){createAnn("1",true);return;}
});
function createAnn(color,withNote){
  const a={id:uid(),bid:pending.bid,start:pending.start,end:pending.end,quote:pending.quote,color,note:withNote?"":null};
  anns.push(a); saveAnns();
  const cont=viewer.querySelector('[data-bid="'+CSS.escape(a.bid)+'"]'); if(cont)applyAnn(cont,a);
  window.getSelection().removeAllRanges(); hideBar(); refresh();
  if(withNote)openNote(a.id);
}

const pop=document.getElementById("notepop"), noteText=document.getElementById("note-text");
const swatchWrap=document.getElementById("note-swatches");
swatchWrap.innerHTML=COLORS.map(c=>'<span class="sw" data-color="'+c+'" style="background:'+SWVAR[c]+'"></span>').join("");
let editId=null;
swatchWrap.addEventListener("click",e=>{
  const sw=e.target.closest(".sw"); if(!sw||!editId)return;
  recolorAnn(editId,sw.dataset.color);
  swatchWrap.querySelectorAll(".sw").forEach(x=>x.classList.toggle("on",x===sw));
});
function openNote(id){
  const a=anns.find(x=>x.id===id); if(!a)return;
  editId=id; noteText.value=a.note||"";
  swatchWrap.querySelectorAll(".sw").forEach(x=>x.classList.toggle("on",x.dataset.color===(a.color||"1")));
  const m=viewer.querySelector('mark.hl[data-aid="'+id+'"]'); const rect=m?m.getBoundingClientRect():{left:innerWidth/2,bottom:120,top:120};
  pop.style.display="block";
  let left=Math.min(rect.left,innerWidth-280), top=rect.bottom+8;
  if(top>innerHeight-210)top=Math.max(12,rect.top-220);
  pop.style.left=Math.max(10,left)+"px"; pop.style.top=top+"px"; noteText.focus();
}
function closeNote(){pop.style.display="none";editId=null;}
document.getElementById("note-save").onclick=()=>{
  const a=anns.find(x=>x.id===editId); if(!a)return;
  const v=noteText.value.trim(); a.note=v||null; saveAnns();
  viewer.querySelectorAll('mark.hl[data-aid="'+a.id+'"]').forEach(m=>m.classList.toggle("has-note",!!a.note));
  closeNote(); refresh();
};
document.getElementById("note-del").onclick=()=>{
  viewer.querySelectorAll('mark.hl[data-aid="'+editId+'"]').forEach(m=>m.replaceWith(document.createTextNode(m.textContent)));
  anns=anns.filter(x=>x.id!==editId); saveAnns(); closeNote(); refresh();
};
document.addEventListener("click",e=>{
  const m=e.target.closest("mark.hl");
  if(m){openNote(m.dataset.aid);return;}
  if(!e.target.closest("#notepop")&&!e.target.closest("#seltools"))closeNote();
});

/* ───── Gutter + recap ───── */
function refresh(){reflow();buildRecap();}
function reflow(){
  viewer.querySelectorAll(".exchange").forEach(ex=>{
    const g=ex.querySelector(".gutter"); if(!g)return; g.innerHTML="";
    const seen=new Set(), items=[];
    ex.querySelectorAll("mark.hl.has-note").forEach(m=>{
      const id=m.dataset.aid; if(seen.has(id))return; seen.add(id);
      const a=anns.find(x=>x.id===id); if(a)items.push({top:m.offsetTop,a});
    });
    items.sort((x,y)=>x.top-y.top);
    let last=-1e9, base=g.offsetTop;
    items.forEach(it=>{
      const c=document.createElement("div"); c.className="cmt-card";
      c.innerHTML='<div class="cmt-quote">'+esc(it.a.quote.slice(0,70))+'</div><div class="cmt-note">'+esc(it.a.note)+'</div>';
      c.onclick=()=>openNote(it.a.id);
      g.appendChild(c);
      let top=Math.max(it.top-base,last+8); c.style.top=top+"px"; last=top+c.offsetHeight;
    });
  });
}
function buildRecap(){
  const noted=anns.filter(a=>a.note);
  document.getElementById("recap-count").textContent=noted.length;
  document.getElementById("cbadge").textContent=noted.length;
  const list=document.getElementById("recap-list");
  if(!noted.length){list.innerHTML='<div class="empty">No comments in this session.<br>Select a passage, then choose “Comment”.</div>';return;}
  list.innerHTML="";
  noted.forEach(a=>{
    const sec=(a.bid.split("-")[0]||"s").replace("s","");
    const d=document.createElement("div"); d.className="recap-item";
    d.innerHTML='<div class="ri-head">Prompt '+sec+'</div><div class="ri-quote">'+esc(a.quote.slice(0,120))+'</div><div class="ri-note">'+esc(a.note)+'</div>';
    d.onclick=()=>{const m=viewer.querySelector('mark.hl[data-aid="'+a.id+'"]');if(m){m.scrollIntoView({behavior:"smooth",block:"center"});m.classList.add("flash");setTimeout(()=>m.classList.remove("flash"),1100);}};
    list.appendChild(d);
  });
}
const recap=document.getElementById("recap");
document.getElementById("cbtn").onclick=()=>recap.classList.toggle("open");
document.getElementById("recap-close").onclick=()=>recap.classList.remove("open");

/* ───── Pager (prompts de la session courante) ───── */
let secs=[], cur=0, sobs=null;
function setCur(i){cur=i;document.getElementById("pcur").textContent=String(i+1).padStart(2,"0");
  document.querySelectorAll(".sess-toc a").forEach(l=>l.classList.remove("active"));
  const id=secs[i]&&secs[i].id; const lk=document.querySelector('.sess-toc a[data-target="'+id+'"]'); if(lk)lk.classList.add("active");}
function initPager(){
  if(sobs)sobs.disconnect();
  secs=[...viewer.querySelectorAll(".exchange")]; cur=0;
  document.getElementById("ptot").textContent=String(secs.length).padStart(2,"0");
  document.getElementById("pcur").textContent="01";
  sobs=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting)setCur(secs.indexOf(e.target));})},{rootMargin:"-12% 0px -80% 0px"});
  secs.forEach(s=>sobs.observe(s));
}
function go(d){if(!secs.length)return;const n=Math.min(secs.length-1,Math.max(0,cur+d));secs[n].scrollIntoView({behavior:"smooth",block:"start"});}
document.getElementById("prev").onclick=()=>go(-1);
document.getElementById("next").onclick=()=>go(1);
document.addEventListener("keydown",e=>{
  if(e.target.matches("textarea,input"))return;
  if(e.key==="Escape"){closeNote();hideBar();recap.classList.remove("open");return;}
  if(curView!=="read")return;
  if(e.key==="j"||e.key==="ArrowDown"){e.preventDefault();go(1);}
  if(e.key==="k"||e.key==="ArrowUp"){e.preventDefault();go(-1);}
});

/* ───── Recherche plein-texte ───── */
let pendingFind=null, findHits=[], findIdx=-1;
function searchData(){return window.__SEARCH||{};}
function reEsc(s){return s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");}
function occCount(text,ql){let n=0,i=0;while((i=text.indexOf(ql,i))!==-1){n++;i+=ql.length;}return n;}
function snippet(text,ql){
  const i=text.indexOf(ql); if(i<0)return"";
  const s=Math.max(0,i-40), e=Math.min(text.length,i+ql.length+60);
  const snip=(s>0?"…":"")+text.slice(s,e)+(e<text.length?"…":"");
  return esc(snip).replace(new RegExp("("+reEsc(esc(ql))+")","ig"),"<mark>$1</mark>");
}
function onSearch(q){
  q=(q||"").trim();
  if(q.length<2){closeFind();buildSidebar(q);return;}
  buildResults(q);
}
function buildResults(q){
  const ql=q.toLowerCase(), idx=searchData();
  const list=document.getElementById("sesslist"); list.innerHTML="";
  const hits=[];
  MANIFEST.forEach(m=>{
    const text=idx[m.sid]||"";
    const inMeta=effTitle(m).toLowerCase().includes(ql)||folderName(effFolder(m)).toLowerCase().includes(ql);
    const c=occCount(text,ql);
    if(c>0||inMeta)hits.push({m,c,text});
  });
  hits.sort((a,b)=>b.c-a.c||b.m.mtime-a.m.mtime);
  if(!hits.length){list.innerHTML='<div class="sr-none">No results for<br>“'+esc(q)+' »</div>';return;}
  const tot=hits.reduce((t,h)=>t+h.c,0);
  const head=document.createElement("div"); head.className="sr-head";
  head.textContent=hits.length+' session'+(hits.length>1?'s':'')+' · '+tot+' occurrence'+(tot>1?'s':'');
  list.appendChild(head);
  hits.forEach(({m,c,text})=>{
    const d=document.createElement("div"); d.className="sresult";
    d.innerHTML='<div class="sr-title">'+esc(effTitle(m))+'</div>'+
      '<div class="sr-meta"><span style="color:var(--accent2)">'+esc(folderName(effFolder(m)))+'</span>'+
      (c?'<span class="sr-count">'+c+'×</span>':'<span>titre</span>')+'<span>'+esc(m.date)+'</span></div>'+
      (c?'<div class="sr-snip">'+snippet(text,ql)+'</div>':'');
    d.onclick=()=>{pendingFind=c?q:null;openSession(m.sid);};
    list.appendChild(d);
  });
}
/* Surlignage des occurrences dans la session ouverte */
function runFind(q){
  closeFind();
  const ql=(q||"").toLowerCase(); if(ql.length<2)return;
  const walker=document.createTreeWalker(viewer,NodeFilter.SHOW_TEXT,null);
  const targets=[]; let n;
  while(n=walker.nextNode()){
    const v=n.nodeValue; if(!v)continue;
    if(v.toLowerCase().indexOf(ql)<0)continue;
    if(n.parentNode&&n.parentNode.closest&&n.parentNode.closest("script,style,mark.find"))continue;
    targets.push(n);
  }
  targets.forEach(node=>{
    const v=node.nodeValue, low=v.toLowerCase(); let i=0,last=0;
    const frag=document.createDocumentFragment();
    while((i=low.indexOf(ql,last))!==-1){
      if(i>last)frag.appendChild(document.createTextNode(v.slice(last,i)));
      const mk=document.createElement("mark"); mk.className="find"; mk.textContent=v.slice(i,i+ql.length);
      frag.appendChild(mk); findHits.push(mk); last=i+ql.length;
    }
    if(last<v.length)frag.appendChild(document.createTextNode(v.slice(last)));
    node.parentNode.replaceChild(frag,node);
  });
  if(!findHits.length){closeFind();return;}
  findIdx=-1; gotoHit(0);
}
function gotoHit(i){
  if(!findHits.length)return;
  findHits.forEach(m=>m.classList.remove("cur"));
  findIdx=(i%findHits.length+findHits.length)%findHits.length;
  const m=findHits[findIdx]; m.classList.add("cur");
  m.scrollIntoView({behavior:"smooth",block:"center"});
  updateFindbar();
}
function updateFindbar(){
  const fb=document.getElementById("findbar"); fb.classList.add("show");
  document.getElementById("findq").textContent='« '+(document.getElementById("search").value.trim())+' »';
  document.getElementById("fcur").textContent=findHits.length?(findIdx+1):0;
  document.getElementById("ftot").textContent=findHits.length;
}
function closeFind(){
  findHits.forEach(m=>{if(m.parentNode)m.replaceWith(document.createTextNode(m.textContent));});
  findHits=[]; findIdx=-1;
  const fb=document.getElementById("findbar"); if(fb)fb.classList.remove("show");
}
document.getElementById("findnext").onclick=()=>gotoHit(findIdx+1);
document.getElementById("findprev").onclick=()=>gotoHit(findIdx-1);
document.getElementById("findclose").onclick=()=>closeFind();

document.getElementById("enterBtn").onclick=()=>setView("dash");

/* ───── Init ───── */
document.getElementById("search").addEventListener("input",e=>onSearch(e.target.value));
let rt; window.addEventListener("resize",()=>{clearTimeout(rt);rt=setTimeout(reflow,150);});
/* Resizable sidebar: width persisted in localStorage (pure UI preference, never on disk) */
(function(){
  const layout=document.querySelector(".layout"), rz=document.getElementById("side-resizer");
  const KEY="memorium-side-w", MIN=220, MAX=520;
  const saved=parseInt(localStorage.getItem(KEY)||"",10);
  if(saved) layout.style.setProperty("--side-w",Math.min(MAX,Math.max(MIN,saved))+"px");
  let drag=false;
  rz.addEventListener("mousedown",e=>{drag=true;rz.classList.add("drag");document.body.style.userSelect="none";e.preventDefault();});
  window.addEventListener("mousemove",e=>{if(!drag)return;const w=Math.min(MAX,Math.max(MIN,e.clientX));layout.style.setProperty("--side-w",w+"px");});
  window.addEventListener("mouseup",()=>{if(!drag)return;drag=false;rz.classList.remove("drag");document.body.style.userSelect="";
    localStorage.setItem(KEY,parseInt(layout.style.getPropertyValue("--side-w"))||300);});
})();

/* ═════════ Welcome: pen-drawn thread ═════════
   One sequence: the left column settles, a demo query is typed into the real search
   field, and from its first word on the search itself draws the thread: the scan beam
   is the pen tip. It sprouts the commit nodes, locks on the matching one, then hands
   the field back and lets the thread breathe. The rAF loop only runs while the welcome
   view is visible (setView -> window.__heroSetActive). */
(function(){
const RM=matchMedia("(prefers-reduced-motion: reduce)").matches;
const NS="http://www.w3.org/2000/svg", W=640, H=380, N=220;
const WORD="Memorium", titleEl=document.getElementById("wTitle"), wq=document.getElementById("wq");
const NODES=[
  {t:.10,up:true, h:"3e1f0aa",m:"feat: session export"},
  {t:.26,up:false,h:"9b01e44",m:"fix: docker port map"},
  {t:.42,up:true, h:"f24d80c",m:"feat: dark mode"},
  {t:.58,up:false,h:"a3f2c1d",m:"fix: auth token bug"},
  {t:.74,up:true, h:"c98d517",m:"perf: lazy render"},
  {t:.90,up:false,h:"7d40b2e",m:"test: playwright e2e"}
];
const QUERY="auth bug", HIT=3;
const SPROUT=.38, TYPE_CH=.028;
const QSTART=2.4, CH=.09, DRAW0=QSTART+4*CH, DRAW=1.9;   // the pen starts on "auth"
const SCAN_END=DRAW0+DRAW+1.4;
const ease=x=>x<.5?2*x*x:1-Math.pow(-2*x+2,2)/2;
const easeInv=y=>y<.5?Math.sqrt(y/2):1-Math.sqrt(2*(1-y))/2;
const backOut=x=>{const c=1.7;return 1+(c+1)*Math.pow(x-1,3)+c*Math.pow(x-1,2);};
const clamp01=x=>Math.max(0,Math.min(1,x));
const reach=t=>DRAW0+DRAW*easeInv(t);                     // when the pen tip reaches position t

function centre(t,ph,amp){
  const x=36+t*(W-96), env=.55+.45*Math.sin(Math.PI*t);
  return [x, H/2+62*env*Math.sin(t*Math.PI*2.5+.35)+amp*Math.sin(t*Math.PI*3+ph)];
}
function width(t){                                        // pen pressure: thin entry, full belly, taper before the arrow
  return 1.1+5.6*Math.pow(Math.sin(Math.PI*Math.min(1,t*1.06)),.7)*(.88+.12*Math.sin(t*17));
}
function geometry(ph,amp){
  const P=[],L=[],R=[];
  for(let i=0;i<=N;i++)P.push(centre(i/N,ph,amp));
  for(let i=0;i<=N;i++){
    const a=P[Math.max(0,i-1)],b=P[Math.min(N,i+1)];
    let dx=b[0]-a[0],dy=b[1]-a[1]; const len=Math.hypot(dx,dy)||1; dx/=len; dy/=len;
    const w=width(i/N)/2;
    L.push([P[i][0]-dy*w,P[i][1]+dx*w]); R.push([P[i][0]+dy*w,P[i][1]-dx*w]);
  }
  const f=p=>p[0].toFixed(1)+" "+p[1].toFixed(1);
  return {P, body:"M"+L.map(f).join("L")+"L"+R.reverse().map(f).join("L")+"Z", line:"M"+P.map(f).join("L")};
}

const strokeG=document.getElementById("threadStroke"), bodyEl=document.getElementById("threadBody");
const maskEl=document.getElementById("maskPath"), arrowEl=document.getElementById("threadArrow");
const beam=document.getElementById("beam"), nodesG=document.getElementById("threadNodes");
const nodeEls=NODES.map(()=>{
  const g=document.createElementNS(NS,"g");
  g.innerHTML='<circle class="ring" r="9" fill="none" stroke="var(--accent)" stroke-width="2" opacity="0"/>'+
    '<line stroke="var(--accent)" stroke-width="2.6" stroke-linecap="round"/>'+
    '<circle class="pearl" r="0" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="2.2"/>'+
    '<text class="wlbl" text-anchor="middle"><tspan class="h"></tspan><tspan class="m"></tspan></text>';
  nodesG.appendChild(g);
  return {ring:g.querySelector(".ring"),stem:g.querySelector("line"),pearl:g.querySelector(".pearl"),
          lbl:g.querySelector(".wlbl"),h:g.querySelector(".h"),m:g.querySelector(".m")};
});

let running=false, rafId=0, t0=0, typeTimer=null, maskOff=false, userTook=false, demoTyping=false;
function takeOver(){ if(!userTook){ userTook=true; if(demoTyping)wq.value=""; demoTyping=false; } }
wq.addEventListener("pointerdown",takeOver);
wq.addEventListener("keydown",takeOver);
document.getElementById("wform").onsubmit=e=>{
  e.preventDefault();
  const q=wq.value.trim(); if(!q)return;
  const s=document.getElementById("search");
  setView("read"); s.value=q; onSearch(q); s.focus();
};

function frame(now){
  const e=RM?99:(now-t0)/1000;
  const breathe=RM?0:clamp01((e-SCAN_END)/2);
  const {P,body,line}=geometry(e*Math.PI*2/9,4.5*breathe);
  bodyEl.setAttribute("d",body);

  const front=ease(clamp01((e-DRAW0)/DRAW));             // pen reveal through a mask, dropped once complete
  if(!maskOff){
    maskEl.setAttribute("d",line);
    const len=maskEl.getTotalLength();
    maskEl.style.strokeDasharray=len; maskEl.style.strokeDashoffset=len*(1-front);
    if(front>=1){ strokeG.removeAttribute("mask"); maskOff=true; }
  }

  const end=P[N], pre=P[N-6];                             // arrowhead, as in the logo
  const ang=Math.atan2(end[1]-pre[1],end[0]-pre[0]), s=13, sp=.62;
  arrowEl.setAttribute("d","M"+(end[0]-s*Math.cos(ang-sp))+" "+(end[1]-s*Math.sin(ang-sp))+"L"+end.join(" ")+
    "L"+(end[0]-s*Math.cos(ang+sp))+" "+(end[1]-s*Math.sin(ang+sp)));
  arrowEl.setAttribute("opacity",clamp01((e-DRAW0-DRAW*.92)/.25));

  const beamOn=!RM&&!userTook&&e>DRAW0&&e<DRAW0+DRAW+.35;  // the beam rides the pen tip
  beam.setAttribute("opacity",beamOn?Math.min(clamp01((e-DRAW0)/.2),clamp01((DRAW0+DRAW+.35-e)/.3)):0);
  beam.setAttribute("transform","translate("+P[Math.round(front*N)][0].toFixed(1)+" 0)");

  NODES.forEach((n,i)=>{
    const el=nodeEls[i], p=P[Math.round(n.t*N)], born=reach(n.t);
    const g=RM?1:clamp01((e-born)/SPROUT), dir=n.up?-1:1;
    const tipY=p[1]+dir*(width(n.t)/2+40*ease(g));
    el.stem.setAttribute("x1",p[0]); el.stem.setAttribute("y1",p[1]);
    el.stem.setAttribute("x2",p[0]); el.stem.setAttribute("y2",tipY);
    el.pearl.setAttribute("cx",p[0]); el.pearl.setAttribute("cy",tipY);
    el.pearl.setAttribute("r",g>0?8.5*Math.max(0,backOut(g)):0);
    const chars=RM?99:Math.floor(Math.max(0,e-born-SPROUT*.6)/TYPE_CH);
    el.h.textContent=chars>0?n.h+" ":"";
    el.m.textContent=n.m.slice(0,Math.max(0,chars-8));
    el.lbl.setAttribute("x",p[0]); el.lbl.setAttribute("y",tipY+dir*20+(n.up?0:4));
    const hit=i===HIT&&!RM&&!userTook&&e>born&&e<SCAN_END+.8;
    el.lbl.classList.toggle("hit",hit);
    el.ring.setAttribute("cx",p[0]); el.ring.setAttribute("cy",tipY);
    if(hit){ const q=((e-born)%.9)/.9; el.ring.setAttribute("r",9+16*q); el.ring.setAttribute("opacity",.7*(1-q)); }
    else el.ring.setAttribute("opacity",0);
  });

  if(!RM&&!userTook){                                     // demo query typed into the real field, then handed back
    if(e>QSTART&&e<SCAN_END){ demoTyping=true; wq.value=QUERY.slice(0,Math.floor((e-QSTART)/CH)); }
    else if(e>=SCAN_END&&demoTyping){
      const left=Math.ceil(QUERY.length*(1-clamp01((e-SCAN_END)/.45)));
      wq.value=QUERY.slice(0,left); if(!left)demoTyping=false;
    }
  }
  if(running&&!RM)rafId=requestAnimationFrame(frame);
}

function typeTitle(){
  let i=0;
  (function step(){
    titleEl.textContent=WORD.slice(0,++i);
    if(i<WORD.length)typeTimer=setTimeout(step,85);
    else typeTimer=setTimeout(()=>titleEl.classList.add("typed"),900);
  })();
}
function start(){
  // every visit replays the whole sequence: title, left column, then the thread
  userTook=false; demoTyping=false; wq.value=""; maskOff=false; strokeG.setAttribute("mask","url(#reveal)");
  document.querySelectorAll("#view-welcome .w-fade").forEach(el=>{ el.style.animationName="none"; void el.offsetWidth; el.style.animationName=""; });
  titleEl.textContent=""; titleEl.classList.remove("typed");
  clearTimeout(typeTimer);
  if(RM){ titleEl.textContent=WORD; titleEl.classList.add("typed"); frame(0); return; }
  typeTimer=setTimeout(typeTitle,300);
  t0=performance.now(); rafId=requestAnimationFrame(frame);
}
window.__heroSetActive=function(on){
  if(on&&!running){ running=true; start(); }
  else if(!on&&running){ running=false; cancelAnimationFrame(rafId); clearTimeout(typeTimer); }
};
})();

buildSidebar("");
buildDashboard();
initManualCopy();
restoreDir();
const hash=location.hash.slice(1);
if(hash && MANIFEST.some(m=>m.sid===hash)){openSession(hash);}
else{setView("welcome");}
</script>
</body>
</html>
"""

# ─────────────────────────── Main ───────────────────────────

def slugify_name(s):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:50] or "conversation"


def write_session_file(out_dir, sid, payload):
    """Write sessions/<sid>.js: stores the rendered HTML behind __loadSession (fetched on click)."""
    js = "window.__loadSession(" + json.dumps(sid) + "," + json.dumps(payload, ensure_ascii=False) + ");\n"
    with open(os.path.join(out_dir, "sessions", sid + ".js"), "w", encoding="utf-8") as f:
        f.write(js)


_TAG_RE = re.compile(r"<[^>]+>")

def plain_text(html_str):
    """Texte brut minuscule d'un HTML rendu (pour l'index de recherche plein-texte)."""
    t = _TAG_RE.sub(" ", html_str)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip().lower()


def write_search_index(out_dir, index):
    """Write searchindex.js: window.__SEARCH = {sid: "lowercased plain text"}."""
    js = "window.__SEARCH=" + json.dumps(index, ensure_ascii=False) + ";\n"
    with open(os.path.join(out_dir, "searchindex.js"), "w", encoding="utf-8") as f:
        f.write(js)


# Favicon: the rippling ribbon mark as an SVG data URI (vector, crisp from 16 to 512 px)
FAVICON_URI = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23F6EDDC'/%3E%3Cpath d='M4 16 C9 9, 14 23, 20 16 C23 12.5, 25 13.5, 27 15' stroke='%238E9C63' stroke-width='2.5' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='10' cy='8' r='3.2' fill='%23A4AC86' stroke='%236E7A4B' stroke-width='1.6'/%3E%3Ccircle cx='21' cy='24' r='3.2' fill='%23A4AC86' stroke='%236E7A4B' stroke-width='1.6'/%3E%3C/svg%3E"


def build_index(out_dir, manifest):
    out = INDEX_TEMPLATE
    out = out.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
    out = out.replace("__COUNT__", str(len(manifest)))
    out = out.replace("__FAVICON__", FAVICON_URI)
    for key, data in (("__FONT_DISPLAY__", FONT_DISPLAY), ("__FONT_TEXT__", FONT_TEXT), ("__FONT_MONO__", FONT_MONO)):
        out = out.replace(key, data)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)


def serve(out_dir, port=8137):
    # Serve export/ on localhost: the File System Access API requires a secure context
    # (unavailable on file://). Plain static file server, zero business logic.
    import http.server
    os.chdir(out_dir)
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
    url = f"http://localhost:{port}/index.html"
    print(f"\n  ▶ Memorium served at {url}   (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  ✓ Server stopped.")


# ─────────────────────────── Source archiving ───────────────────────────

def archive_sessions(dest_root=None):
    """Incremental gzip copy of the JSONL files. HTML can be read; only the raw file can be resumed."""
    dest_root = dest_root or ARCHIVE_DIR
    os.makedirs(dest_root, exist_ok=True)
    index_path = os.path.join(dest_root, "index.json")

    index = {}
    if os.path.isfile(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            index = {}          # unreadable index: re-archive everything rather than give up

    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    paths = [p for p in paths if "observer-sessions" not in os.path.dirname(p)]

    added = refreshed = unchanged = failed = 0
    raw_bytes = gz_bytes = 0
    for path in sorted(paths):
        sid = os.path.splitext(os.path.basename(path))[0]
        proj_dir = os.path.basename(os.path.dirname(path))
        try:
            st = os.stat(path)
        except OSError:
            failed += 1
            continue
        prev = index.get(sid)
        # A JSONL file only grows: same size + same mtime means nothing new to write.
        if prev and prev.get("size") == st.st_size and prev.get("mtime") == int(st.st_mtime):
            unchanged += 1
            continue
        out_dir = os.path.join(dest_root, proj_dir)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, sid + ".jsonl.gz")
        tmp = out + ".part"     # atomic write: an interruption never leaves a truncated archive
        try:
            with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, out)
        except OSError:
            failed += 1
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            continue
        raw_bytes += st.st_size
        gz_bytes += os.path.getsize(out)
        index[sid] = {
            "project": proj_dir,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "archived": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        if prev:
            refreshed += 1
        else:
            added += 1

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True)

    total = sum(os.path.getsize(os.path.join(r, n))
                for r, _, ns in os.walk(dest_root) for n in ns)
    print(f"\n  ✓ Archive : {dest_root}")
    print(f"  {added} new, {refreshed} updated, {unchanged} unchanged"
          + (f", {failed} failed" if failed else ""))
    if raw_bytes:
        print(f"  {raw_bytes/1048576:.1f} MB read → {gz_bytes/1048576:.1f} MB written "
              f"({raw_bytes/gz_bytes:.1f}x)")
    print(f"  {len(index)} sessions archived, {total/1048576:.1f} MB in total\n")
    return index


def load_archive_index(dest_root=None):
    """Archive index: {sid: {project, size, mtime, archived}}."""
    path = os.path.join(dest_root or ARCHIVE_DIR, "index.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def session_cwd(path):
    """A session working directory: read from the transcript, never guessed from the folder name."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("cwd"):
                    return o["cwd"]
    except OSError:
        pass
    return ""


def restore_session(prefix, dest_root=None, force=False):
    """Decompress an archived session into ~/.claude/projects so `claude --resume` works again."""
    index = load_archive_index(dest_root)
    if not index:
        print("  No archive found. Run this first: memorium archive")
        return 1

    hits = [sid for sid in index if sid.startswith(prefix)]
    if not hits:
        print("  No archived session starts with %r (%d archived)." % (prefix, len(index)))
        return 1
    if len(hits) > 1:
        print("  %d sessions start with %r — be more specific:" % (len(hits), prefix))
        for sid in sorted(hits)[:10]:
            print("    %s  (%s)" % (sid, index[sid]["project"]))
        return 1

    sid = hits[0]
    meta = index[sid]
    src = os.path.join(dest_root or ARCHIVE_DIR, meta["project"], sid + ".jsonl.gz")
    if not os.path.isfile(src):
        print("  Archive missing on disk: " + src)
        return 1

    out_dir = os.path.join(PROJECTS_DIR, meta["project"])
    dest = os.path.join(out_dir, sid + ".jsonl")
    # A live session may be newer than the archive: never overwrite it silently.
    if os.path.exists(dest) and not force:
        local = os.path.getsize(dest)
        print("")
        if local == meta["size"]:
            # Same size means the local session IS the archive: talking about overwriting makes no sense.
            print("  This session is still in place, identical to the archive.")
            print("  " + dest)
            print("  Nothing to restore — you can resume it right away:")
            cwd = session_cwd(dest)
            if cwd:
                print('    cd "%s"' % cwd)
            print("    claude --resume " + sid)
            print("")
            return 0
        print("  A session with this id already exists, and it differs from the archive:")
        print("  " + dest)
        print("  local: %d bytes | archive: %d bytes" % (local, meta["size"]))
        print("  The local copy is probably newer. Nothing was touched.")
        print("  To replace it with the archive: memorium restore <id> --force")
        print("")
        return 1

    os.makedirs(out_dir, exist_ok=True)
    tmp = dest + ".part"
    try:
        with gzip.open(src, "rb") as g, open(tmp, "wb") as out:
            shutil.copyfileobj(g, out)
        os.replace(tmp, dest)
    except OSError as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        print("  Restore failed: %s" % e)
        return 1

    cwd = session_cwd(dest)
    print("")
    print("  ✓ Restored: " + dest)
    print("  %d bytes, archived on %s" % (os.path.getsize(dest), meta.get("archived", "?")))
    print("")
    print("  To resume the conversation:")
    if cwd:
        print('    cd "%s"' % cwd)
    print("    claude --resume " + sid)
    print("")
    return 0


# ─────────────────────────── Setup (Claude Code settings) ───────────────────────────

SETTINGS_PATH = os.environ.get("MEMORIUM_SETTINGS") or os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
RETENTION_DAYS = 3650


def archive_command():
    """Command the hook will run: interpreter and script as absolute paths, no PATH lookup."""
    return '"%s" "%s" archive' % (sys.executable, os.path.abspath(__file__))


def init_settings(settings_path=None, assume_yes=False):
    """Raise retention and install the SessionEnd hook, after showing what will be written."""
    path = settings_path or SETTINGS_PATH
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            print("  %s is unreadable (invalid JSON). Nothing was touched." % path)
            return 1

    changes = []
    current = data.get("cleanupPeriodDays")
    # We raise, never lower: a retention window longer than ours is the user's own choice.
    raise_retention = not isinstance(current, int) or current < RETENTION_DAYS
    if raise_retention:
        changes.append(("cleanupPeriodDays", repr(current), str(RETENTION_DAYS)))

    cmd = archive_command()
    hooks = data.get("hooks") or {}
    session_end = hooks.get("SessionEnd") or []
    already = any("memorium" in h.get("command", "").lower() or "export.py" in h.get("command", "")
                  for group in session_end for h in (group.get("hooks") or []))
    if not already:
        changes.append(("hooks.SessionEnd", "(no Memorium hook)", cmd))

    if not changes:
        print("")
        print("  Nothing to do: retention is already long enough and the hook is installed.")
        print("")
        return 0

    print("")
    print("  Memorium is about to change your Claude Code settings:")
    print("    " + path)
    print("")
    for key, before, after in changes:
        print("    " + key)
        print("      before: " + before)
        print("      after:  " + after)
    print("")
    print("  Effect: your sessions will no longer be deleted after 30 days,")
    print("  and every session will be archived when it ends.")
    print("  A backup of the current file will be written next to it (.bak).")
    print("")

    if not assume_yes:
        try:
            answer = input("  Apply these changes? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("  Cancelled. No file was modified.")
            return 1

    backed_up = False
    if os.path.isfile(path):
        try:
            shutil.copy2(path, path + ".bak")
            backed_up = True
        except OSError as e:
            print("  Backup failed (%s) — stopping here, your settings are untouched." % e)
            return 1

    if raise_retention:
        data["cleanupPeriodDays"] = RETENTION_DAYS
    if not already:
        session_end.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 30}]})
        hooks["SessionEnd"] = session_end
        data["hooks"] = hooks

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print("  Write failed: %s" % e)
        return 1

    print("")
    print("  ✓ Settings updated (%s)" % path)
    if backed_up:
        print("  ✓ Backup: %s.bak" % path)
    print("  Sessions will be archived automatically from the next one on.")
    print("")
    return 0


def main():
    # Windows console defaults to cp1252: force UTF-8 for accents and symbols.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = sys.argv[1:]
    if "archive" in argv:
        archive_sessions()
        return
    if "restore" in argv:
        rest = [a for a in argv if a not in ("restore", "--force")]
        if not rest:
            print("  Usage: memorium restore <id or id prefix> [--force]")
            sys.exit(1)
        sys.exit(restore_session(rest[0], force="--force" in argv))
    if "init" in argv:
        sys.exit(init_settings(assume_yes="--yes" in argv))

    # Arguments: "serve" (serve on localhost) and/or an output directory
    serve_mode = "serve" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "serve"]
    out_dir = clean_input(args[0]) if args else os.path.join(os.getcwd(), "export")
    os.makedirs(os.path.join(out_dir, "sessions"), exist_ok=True)

    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    # Skip the memory-observer automatic sessions (noise: one prompt, not conversational)
    paths = [p for p in paths if "observer-sessions" not in os.path.dirname(p)]
    paths.sort(key=os.path.getmtime, reverse=True)
    print(f"\n  {len(paths)} sessions found. Parsing…\n")

    manifest = []
    search_index = {}
    ok = skipped = 0
    for i, path in enumerate(paths):
        sid = os.path.splitext(os.path.basename(path))[0]
        proj = pretty_project(os.path.basename(os.path.dirname(path)))
        try:
            data = parse(path)
        except Exception as e:
            skipped += 1
            continue
        if not data["sections"]:
            skipped += 1
            continue
        # Kept under the "mtime" key the JS sorts on; files without timestamps fall back to disk time
        mtime = data["ts"] or os.path.getmtime(path)
        date_str = datetime.datetime.fromtimestamp(mtime).strftime("%d %b %Y, %H:%M")
        inner = render_session_inner(data, date_str)
        write_session_file(out_dir, sid, {
            "title": data["title"], "toc": inner["toc"], "html": inner["html"],
        })
        search_index[sid] = plain_text(data["title"] + " " + inner["html"])
        manifest.append({
            "sid": sid, "project": proj, "title": data["title"], "cwd": data["cwd"],
            "prompts": inner["count"], "mtime": int(mtime),
            "date": datetime.datetime.fromtimestamp(mtime).strftime("%d %b %Y"),
        })
        ok += 1
        if ok % 25 == 0:
            print(f"  … {ok} sessions rendered")

    write_search_index(out_dir, search_index)
    build_index(out_dir, manifest)
    index = os.path.join(out_dir, "index.html")
    print(f"\n  ✓ {ok} sessions exported ({skipped} empty or skipped)")
    print(f"  ✓ Index: {index}")
    if serve_mode:
        serve(out_dir)
    else:
        try:
            webbrowser.open("file:///" + index.replace("\\", "/"))
            print("  → Opening in your browser…")
        except Exception:
            pass


# ─────────────────────────── Embedded fonts ───────────────────────────
# Latin woff2 subsets of Fraunces 600, Atkinson Hyperlegible Next 400-700 and JetBrains Mono 400-700,
# inlined so the export never touches the network. SIL Open Font License 1.1, see OFL.txt.
FONT_DISPLAY = "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"
FONT_TEXT = "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"
FONT_MONO = "d09GMgABAAAAAHrIABMAAAABMsAAAHpaAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGoE4G4caHIGacgZgP1NUQVSBICceAIUiL0wRCAqB3DiBrC4LhhYAMIGcXgE2AiQDjBoEIAWHRgejDAwHW1AWcULR644IupPc+VytavGqkJtnyu2QqOHfX9FshNWVxRY42f//n7Z0jIGD3gdgpmZV/TpBzdyUSPfCQt9Ir2775hmlsUTSxUbRuslCDFIc05TjtKQgBSlI4wIDtYvaxD16FRuC4oAZj+oVVHMeW2m0505HxcjigW7E9kRYWCFC63oPDI04NL4riamk4OKb+isfSWG2tC3eT1znEOMX6NlmyljL5xcpKkVQ/n1INxh2cd9nQcNOYdjBamiaU3HQBIvvPvBGptL/1dRtFX65ZsZF7iqpcg03XJu2vHyi/cRUF4GNyxhZJ2nfI8jNZjf5DeELISBiDCHG8BhjBIyIASNGjDTGiJimiIh8Ip+IlCICUsRIET++WuQrIvXwoxw+pZYiRYtIkXIWqSJFRI4ipUgpQgJP/2t93qoHDVTdn7sXGIbtcgBAx47NqIAlF6MAlcyJ3OR4+r+cVr9QpZKlkm3JiQN2Ew4AnGbpSNc5HPfdI1eyy7cjcGZeeggzQEl3u8E9iRtMkqySVTxPf2+e+/54hBKQ0ikaDVi01PFgBCq2DhZuBWOxqn93349u9QWYjDBfXK8oOmBPSi1KLdq7UkzWWGUIkCAv8khCUbfbD1LozJAM2lnlgIf+ey2n3wCNOaVQCs7bQ5kT29NJjuw4LaQFABUAWgR+PvHrdb6/axlm9kjG8PQfoQIsmpfkgmw9sPQwxI7UWK3cAgW5+gx1ALvmJ4D//8Sa6yMVQsVLp+Y3k5m/2G3qpkPapRJ9a27bgA3RnP0PPThI60BpkUBISJAAMdusZJONk8kmQRaSIIESIFiFCvwrpU57VagYtD2hYlDzM6N+9N59Tsyo4u5bBkFx5EEQQwYp1v///OVOaxP9feOtmyuiKNAsxcAT70e792VN2u34QXejDjWGRdNKYl6d9nZbA8qxZZAtyyDHcVpOSv8uRzB9mGiY/zLU1cGfRnz7n4GgdFTAHFHmnasvXezMYinhQqBp07RD237G59eYO0mOhETh7p0wpADllweKSJ3+5/sztX1jAZI4ylWT0vuaZchLOjQ8HLp9X4Y4CUHXRKnbCfB2v2klJidm3bVXVNf6UG7W0DrtbtgtFSrGidjrcb/npbnzx/P+Yl5RLKVoQjIzyUQmJkC7RMDfm2qV/ocGqaao2wKkNeBa8Cw0cwazGyTO+PzM7+4PdPdvgGg0QKoJUhIAUqsmJN00QEoCODNXhOMAILlLcpzGeY73DVAGHAu5HZ6xLhtz3mbrKr8gnPDCy8Kr3LsgSC8Jry5Iz/8ytUy7/3Rr36DZcud4hb2VNeHFDvI+9UH2G9OzADGD5Se6ycV5nvWh7CzOcQkZkCsPOctNArlUSaYokN+3VGnvyIxIfqHgcjOrsW93dUprff+My6k8noSZhxAnAFYYACsBMAQE/v+9+d75slQuPuN4O5MCSSjA8EMQBkNIMfi3pkn1rmqvB4ZhHwUpyni09k9pPIj1Av+2080mifUw99+AIg002pVz8P1ZRF4VP5ffhxLRxohoo621x/Wb7PuXc4auAPrea4eLtEsqVoKEYZqGEIIsQba/n+/v91V9E5DSLvukDSVYioKKzuzj/bfDWOv7QbOao7ftNrc5ZUpKSIKISAjIOO75///Lzagi/gIWxdbm4cQT9OoAQcDKl9zzMDYQVPrpCIdTROR05edU2ORUUjkDBTrVgp06OqdeuDNajDNWovOQQ87DUuW/24mYdSDZnwYEiURROA+gOGcqSnNkoKwjgQCwdzIg10PATCW5dBYgOkAuHHzidPgk8MxfpQXmR3uT44AJEQC4zPifhmGgAoEGESSvXxfOGycD1mcwiG6sCBw9AgB+A+v5J4+3ip/B/mQ/iuVrkRQslglYLcL88tVnVXaMKHmeYU8p+kwEEQCuMRF/5yVw8OWGjAB6quE1vYDxe/CxozHiueL4OYFa1Kybugqg747nBYDgC1mlAZIogMvuwWJBhBNPkKcTmCv90Hc3AGnJ0yQrBAxRXkeE5SHuanh51rZinYiPVogZhFbxBh0AKyoRgffTgfsV8ZknNAMMKPJ9WMUtTgMYi7ZToKcYKrOcyRQyGxhfxp/ZzKiZ7YyO0TNhzD5GFSLTozABCJkQDJoVIWsjVAD8QQ6ydn9hfwAJ06sS8GF3jQDsMQKbgEcSpuzvW670WHTw4iR9Cod+DGVevq58Qmia2/zvyRWe+mq54Dj7uZULFh1q8+eGAClgiODsWJwA7Z785t7YZJDP7TuYAF4fE//3F88CknmzLxZcFYFrJQbMAYjTsYR3EgAMz7/1QoIGE0vWbNiys5gjHr7lRFy5k1hjrXV8+NlEaatt1HbYaRedEKHCRIh2wEEJDjksVboMmbLlOMEg32lnnFeszEWXVKtVp95V19zUpNkd3/lBm3YPderS7ak+/X730it/GDFq3IS/TPvHnIUAcoiMaIiBmMgSWSM24iB7tARxEQ/xkQAJkQtyQ2IkQVLkgbyQDHkjHyRHfkiB/JESBSAVCkRqpEFaFISCkQ7pUQgKRWGogC1bi+2I7VNbn63DFmyzyd1hbbYeEeZvDVB+bOm1pFuqLTPNv5tRcxZ1h2qm6iiH6WNTr6ndFGSykN1uHDY+Mvob5ewX2UbeJA+TSYZ/DQsM1cTPxKdEX7JziEVEPf4n/rW4Yb0VX4wn4k9js9habCkcjsnoZEsojcpRgf6cfq/+WeRz717vcITeE7ob66pkBXVK7YeyNpm3tvBHXHNNcwpuWOMarvqMert6kmpU2Zu4KlxFsyylUfmtR56yWTGh+HRN3xqNAkcF8ufl22V/y77d46vTZc2N9T0imZV+KMmXtkserWqRWCSF4hviBv2z4jzxXPJw8RZ1C8PCfiUMU0PBkDzw3Fnu5L6oz3mM9yw3rCwf5YO6qW74E+9UnQ94JFyXa+TroHY+K8bb/javTWmtVhCbmaa8KWyyGyvh03rGVthcyxbM1nl1ykiaYfPIscv4G2mWHtLdOkSrLpfqV/ctlV4FyjH5TIZJrSwkVFPXBctiq5DFjeUySmM5WeaXaaWQyyzuFfWFplAUHo49eXOuzGUDj6/FklHRJdJF3DDFjXz8zmGezmOuFbA5Nmo9mCmOBL1PGziqj73rmj5yj9Q7ZHbCLtyMa81ijqNtA7rYy0ISWw67Cp6HuZA9bWYza5NuJDBgfd8uWM+gsfwvZ23eemMtjqjxYNfaabDU8npbvpnbyiy5FtetaDPXFFBiPvjueqebh5nTmZOvzFcY08Js5qq5DJc9Y37X78Ltw+AvajyLTsUiEf3ZalmBdDHtxWybSbRgGpk63mWd2UIqk3LvqDp8KHxyz2geEWQVaa4GK4GkIQHxzwwgzHrWd1zoQqcTvAgiggMefO3zl9BBw0YHQeQUAqWOwTAhczJYf5RhZwCB+zmEzu2qQpZuHxTIIxg+OtsjWBDuazegjwEpjrtgH2QBw3ykXJgrdckGyJAYUm8vBTOBeb3eh7J1XDn6XLk6oTzzNgfgD7SCR/D9C1O+DliNBvQeYPCgBQB5ogXny29hu2Cba4ewoLbr8l88ASWiJHQIJaPDKMXLnogkCSbChGAbdPfT/Q5g0EYZ2G+zgrcV4mDIQBvaloBSgJLH9t4wLK5pEIpK3G4E3kwep0KQ/XGm2PPETGX6HPghJ2V9iSrHbj8S3Lw5YjLMDG4hnLzpE4sov2BP3zx+KALwgpiYSZ4F6W/PpwFsbmFAqOb1ogm3feXy9J9IhFDxwmcxfdO8ifDCHc9N9Ho5n37SHpCObOd4PpjQd/UAHJ57ahoQ5lO4xQmYmygNL38PvgpkfZ2c5I+bNWLu7kaO9CrmcX7EMK9iTpe7c6nRzumRd8dsdvm9wFsFXgjV6i7da14Whf2l+unLUOVyqSQ12gI9+lKejX0xyBkV5K7R6UXMivzVWfuiksjVEddoeXLc1b7ymeFRJd9dS0GKtzMd/bMmdG6eT6OF00Aj8zymbZszb0StQ904dk0FrrZGj545ZGlfDnAO01h33V4/y9ZXp1SYZzFVyrWZ1Datlx53bURs/+FfUYFDUTxRco/EPMFk4DRc3DzpZMICvxjxlZkduNlKTArcKJMeYdLlR3H2kYqRkT41Wp0eN1JlZp8ffslG84rC4oEOsxnmQTk2jmMTkccTyv92EYu3n7KUCXeo7uIQ3cN4xsfZd5sHZFh8SyKy9OrRvgHjihHxoaOAIjq5WGzK4MVgNzUokpoa0249JOpi81H+L6MUPBGAnO2LSBbdLHaaHXTeTW53cNWhpwMu7PLUIEDgCUmH6qVR6EmYByRNBS4eDVAM7nIuXU2xsrsuBmYC14sLTkTmMj3aKWHJAYrB5Y7DaEDJ96w2AzhN2Me1zTRGDnMXaxywGOzap0nJfupQXAcNOGugToqLDsc0haYDTe5To6RaEYle6AmTqEoC4aJyBpgDZTjLpgaFyFuiaUA6MVOujdoAOdoMiXUMMAfycmRFFfIWT2AqEEtwwrVNS5Tj8IVY54BzwPY+VojVVCAPAOY7w6dCcg8z3liYAmjYx3DRv0QkTNWTMGpEX6PgD/AeMO70dRJIm9flRgAdovMdkaFZjtbpsNYB3gN6HGQSQYI9xBkCNAjoXdtMy+UwlVjbgO8BoX1gFvYQgIeRzTnVQiDcbcaregMA/n7W415E7aOx0r8/KbvvniPTmqw/pTEqhUjhUial4BCfzJsUEVeSsUTkygQsNE39+MUoINaUgl8ywIiGkOGuimjElXF/lWNG7I+evkq+Ss77z7hyZQZloNchi9Jjv84NM8VG5flh2G5Ujgw9qdEm68rQihLEDRKqFK63KFaK773RrhRX/LhrtBZytB+okA1bw4S0VbDXx0quVaAFLjdir9X5AhrkckUz3qeZAXtlbOkRikx7eVT4uK59uRT31qg2aexDX+b2VORx2dXvyYxHn7K5J3SZrkMdMeYkMvdBurJHRdrNaZum3PA/k0GSUIFkUEM87Uzuv2/rJfUu1OLSzbx7X8Ol3RxfKu9wTK96Me1Txing3RLd38IBUA9OclGBq+nSOd5N9qJutim6eoA1XPDu++wOztmeFB+Hc+Nk5aLRjHfPjMUF2Pf+ro2INE3oSdhLRTedWN6ARkwN/Y2HKIkMLuaUG14RUphxggM2wBhMSfvrOedBDR2qQr/UetTxmdaFDws4S+qv6yYuKVyYwfq8mxik5MIQjB9njSrPPOax4zAR6YyvzKfWjT0FOmA1NsN992w8W1QqCq7NjRlFNdY6YGNMhU3dvrs4BhGRNkX6N9gZRgWu5Z0ALyE2x6pENmGpQDmmkbwJ1AAVMvWY8RuZybHSzzYCRKRVgM7xhqUOe03zh4G7aHtlPUiFgqnwvO7UQMHHajN+3WwY2pLZugYRqc3VOV6fVZeFR3GH9fQIQfx5XDYepm30QqQch862oSFPaV1ha+2VOMOW7bQOL1phWK45mmE4uZnXo9fUptC6Yp02DbCF1aN+kFC9CWEMRKB3VkhIRlkz3g6SZbZwvmBfpMuBuiY6XaV/nvAEEzyVvORE8EcxZ09pMkMM0AYutXq6zTpoYe4fMAChbmDaBsG52RnliNijAIsGMdp9SQJXPs41OLRN50RLrZ/pcM5wBnH/fD6Xdi+u0cToiO8nuLdHcwlaXRMcrpGjglWBDBebcNTKHS6kRi5WhfGH002Dw1FyZMysyHTY0ArSOa6fuVOAUnSPJeM5VQKG6+oqOsJG6utCcwQ9/6o6Y1GHenwct0bQA+OQVOBqO3W1DvSglXrt4iJAl7FS7QA6wPoqdK6rqTSXdUkH7LSZQ4ewxQyqtm5UVHjFdC0luT+YTHOu4LgOFKEWKaLzHcX+mG3G+0azDzXAFl+hyKQeUeGDvhkd8VlCRaiivtepItrFqg7e36ls2pHV+TBexdkdpRPvuFg1tVIpxTPUgcP3BhCheyaK5QpFoASlggKKrbEVjJfjJkB3cSafISJ1Bp1j2a4uC9lSBaMQOi27ka0/AxO3li2dnJ5M74FRlgUJRxvFDTCi4/AUnta1fUOmwln6bdDPwE0zq1LcmM3YC3bb3SPXTxdFywca8bmIu5dUzZ6YfEhULqm6NtVWV1kJ83O8mHJ4erQQoKrzUXwYDygcdzUBmIYJHxLDjoswqqroLnkHnW87jQ5WjeMy9QAedCPiHNYQPfgXixg4aq/r6cGsLIIcjraFGzCLrl7awcWuS6ZLVIm0XeqYu8p7EFyFiwymAs1gGC2lolDTexl6E7De9r0SinQwq6ZmK7ZIujpwdnQACVqOuDQqFH+riC03U0tEMTXWNOObSTOFZkCcz2JFpuaRzrFz9ffyRDR4wYM7Ux74wvRgEv34xmC1Ae8wwOkBBtjrJ7HoplK0L5H/ph8mqteIe/An+kw0jnETAaOQhpGIbKT6o3Yel4GCMJgVEYGKxVWR/o/eIQdeDNCY9FQzPuCbtwgs2ENGJmCkjovfWW5z7vBL5K9mgNaeVfTgDfx5WEncRACUn7FcRz1AgViLRGSQoSsW8v5tEOFYsFYwxjuEFhgXrxX7ZeAlwxgNAiOMcAM6ymeXAJ7euVHZ99T5HgyhuwqVHTcRKGdEJSQilyN6nAptnaJvgHMoG5yZVIPxYt01L2AGURrEdKa2adN0xdwD9tk7oHcsrhpKpaPn6QVcv6fZ5gVK/iZNMBF1qFnj0NSGLVFXqLpqn1eMg5kvWbwAfrl4hAANwOl90Wyeo6hluCL3iiJ2jP4okQGsXlMQESyuivQb2pksNAzQM2mHzHjhY/4HIWIPGZkEh2qLFQkbsC/IgCDWCsvxcx8fA5bl/iBJW+a9EqBf0UZ43mp6wa8zXNFgj1ewk/yPsn6APixmK+K9AKAnvcLecswvsGQmW8BOnUkxU9cY9QgTRjza7mOnifXUUrzGQl3jlCs9QZPulb15gjpTFDUjQ5VwnZyl0jZUV+So/DGJCF4yUxLHUovHiu5ZhH2smGeYlvTAGIUpEpHNhB7NpN26zb0Cn8vNHqaRPZPe6MHPsAaYip4U9uCR2JQOx+0sAFMUmFSeXLsSPDQRVASpJtTxUcau2vE52GvxFWL7lKJDk5WDo2H0k9RTYjwNND9BT/NkZSpj0kNUBKFCdVgGxCX4FyR0g8NJ9AB1rseh5gG0QXCgaos2TVexj+iKrWXjuXQ1tD87YTroR9QO0BKHZqh1s6bWVI+GTCs0nRHUVZGQAVCzrIzuIakRKHw0qm8Cu9SamF+QIWYMhapjuBKKJd2HSYc6y0ooCPS9mAiHp/CckFx0y60Eo5Zw3iUCMA1TssbEyQXEql023lTbrBVb8IkdZfASD5sEaQnPVBSA7kEPIM1xRRrdFd3RjC8izF2kerYUGkWmNIAKH1vI34o5/PxlFHQuE1L67aQF9g3dhg4EqTeQbouWJRxnaW4j6XoTyYYAU3JPT8IkIZt45dKjysfi6wNJpiNS+i9Uuyc8818k+k1iYVNQh1q1B8S69dZVPHlhrcGHy3kjYbK4KtK3UJoczzFAjaJ8zPh4xNxC3MMeMjLF96kw60Jj1I3n3hM53Xh/+eg9vv4KcPqvUSyB/4M1+HCp2DlmaoAaILU+mjf/QTTG7PFBIOpnuCL3okfs1NpFwYd2ZYmozOU6+gbVoM+V5htEKZJ7pE7fRuhKLoALq27+sfJwSVRxRaX7JMheUVaSKFyjxbKS8COFI3OJzsLBut3Q3lxeFNsPjeYy7KoMVYnIdlHzcDYsh+xJ/rqgU7xWLGFsWwzKvmkcJX2NfUUwHzdIqUb2ZYfxA9fUYGCIsRcrMvVzOsdGdIf39oO4G/8XCrmShVHzJvQOEn07z7bPnqqwj5X7m7gXgErzVwKX78tNBXoDw3V/2w5sdOokFdFPuUusIniyaoeNlgqzulxDBm7VyT1fAlSOvcm+tzJfoQdmz3LKw/7OJGCyHZe9nUF04OLZWJZHqRik/Vb2WwLILsN+1k4dyIZJX2LP7P3WA2zi8UM5YLufka1KFru+IspdAyo8W0VYWqHWmsK8o9Bdy4zVHlp6IMGnk/QlRA2EHdxv23cwKVQpcjWimw5PUMlm7u3o7XG/KQFul8iweEQm3KhzHKW628o0dwnoMUek6ONMnEkXILpKHFvEUpGIOwnYYHMBEXWGFYhIpzM6DyzR9/Kv0ekAmA1+aRfYcGHk0zsaoHMQ/v60zpzFaRETRrTazuWCF13xy6dxJiNOVQ7lUSH4M49STCFOhYLCOxqVSNtw1mjcUnUVKe4+v6gRDP3GOvvnUpdHZ/CcPk3mOVQAHgAa6ngCfSHcI+GY6mm+AFcK9VrsUQMOdb5FBb7wMHJQ+YsxAyQ72IsrRo7q6VTJ7qked1REp4TVhfFH2eZUiebn9UfNRCQ0pswnlBxVlAF3SfV4C9Qo2VgqUB6YV876NEAnhfHN+KzV5AGlStbA/YdfA2x0xOQiRlUc6hxnBnUclcCgd8WSfIJeABCXnWyCQLmgNUAMl/k1n2+3CvefcQcwJTiiCu9lDm4AexnJ6HXhpt0OvGktxQ0XsMilnXQc1MGnd8xxwCRJ69bIbBI28PJIAPy0Gj2f5s1bfr9+cSqanK107apMVy0Okh2qpyyQPI9KTBYO1IJyE+2RRaQDecfFUMdqsY3QSZprg3SqtjD+5QFouLiHX7tGFyy6ZDDvcGERHQMe8GGOOYZ5jYRJicjz83p0GIZzHrX6sHowD3aJgzoaL9XdhA5zFHPpZjybttah5l8zOudr1vhZai8tUX807sBloNk4ePczZn0sror0GdBonrUyQGmCes34WYP5FLMy9pCRaZZHhVld47XjsLY58kwjgeaIuY6YyjFJ2ZSJduKukYiG0swRU3B+zgTMHr0wRGhkzp9shZ7YmriGuAuFL6Rz7Wgd19LNPjEXurCWkgHvIDz/PZscjxX/fJjt68B0syAtd7Ph6GOYedO7xgI2bcq78SuTWd14QOLTgYalmq5IzIjOTHY6jKAkbFogi3XJGCXKxnwzPukzicgUbKG0qnbTSKjwLuX6osPnXMD8KKV8+fsoycHwqi1uL1V9x0Xa10+0FO25uN7XCc+KFC8bmmZ84mrikJbNEg4iUpqnc5yQtYjFfz+N6lgjVbk1MsVi/YXvpk0sUqF0Q9QtqPhIOWfvmTml6mrX2lJEztpkGOpAO2BDrissYnSUzTAxpbVcGZsRZ2vQozv9m6FP66T8YWn9TmglpEisGXPHQoCiZG2sGd+BiYT5SDvOJNQ+brTrpJ+1n8Ze57jt0KL7Em2+RIE3Q2gGZq2BFWgf1kLQJvNkVaT9sqYkPE1CtBIKn61JJHiqpNmHJie3EkakJkFvJbSOXPRe5qUbfmvZAIDG/d7II1aA9qK7ich0Pp/2SlduxvtUsxf11H4YyXAiUj2oJ+HQmxonVjOgEXVFv+xLq2KP4qNsdc5Y7BlbmPFuzO5R4ftlmfuooTVK8/llntsuB9VeEGyseDzZFekTtLW5ZjJAemnPmvHVnPkY1Qh7yMhU9VJhVof96g47/xuFVFGCKXkRYCVoK+QeAHajbAVnfwF6Gnw8Twfs+xfdBEIduxV7xWFh6Gui1xiWXt6oED1te+OCbX3Od5j4kAFURIeCgmAGj8QErZSD745y2S0jXKzAzGoHMKE865RYaBXHy5TVqrjrZFCDbQiPk+7Ws1uvKFG9uHNROr5DE06wIljRdjQVaAJF5zPamePGwD0gHdGKSsPqNcmhz5pPt4+Ez2yH2DDwHamr1NzUhwj1bNSqe8Fq7VLrfwEARbpogipQIBrIqtcAfST1uBmvShOIQj5TKWr7xl3nWHnqmJf0WO+TqJTIGx3UtJNRUqFO89I3VR2/dTdY2vCrQ33/msYdp6+Pa9YI314BQc7DYdUsjYnT9IOHssGSkno0bJKWn0MSgd+X6+Bkg9kgXb24mMhF7IQcQQGoGpCzndBpi1RlzXhmzBZkE8HjIUWmbIAad9rv7yOajDSj9Rv4DLL8dyBXpkzyR8UECX1ixJD/Zjm7ZzNaBmjzrByaGY+P8UcmvQd3QtS+G64+RMAeS3Xxi+qLnxk+iral8ssJr+h7BbWPFCh18GKXttJGKf3N+LTBbISn8hRqmSKTm6HCK52HddwAv40Lrp3FNWQPfFEMZHeTTA/kUnSY2XDlG7ABzsAesjZuUqkwq8ufDYGFYA01eIxh2ck0QOtRaLwTGm84NnM/pQ4INXlM7ih7OiOLhsDpg9+kcrPnoC0OBcmQ93kkRraiZdd1VoOMcOrcZaD4DLdSI7ayuCqSF/LYHBsYIE/Jg834WGY8EHPZQ0ammEyFWR3+dtTB6eCXqsA12xy9NEBSZMM+CsxqRBYTRrQ6R2Rn/u3Qx1SwRgfyrBFusrkq0ipkSTmUM0BiyULM+GAwKxFS2ENGphBOhVndKhc1S/42MiC4ugDkhnTC+wnjhkA94weKCj8rvlMRGvhR7++pq8k/W2M47dCLXhTPyB3FSt19owxxQgTChzhvKFy0yqYal9LLtiOabpq/o/lj9Rp51qyxPLUnLdbEVszspnXFcyv525oGp16tmvdpnGS0dI1XaLCGhGVSoX0KH1ohSX4CVmSEpUudsBGRXIQyY6IX3G4+LP4PJ2Yn1AUSIHHYciS5A3LeTKgUqpda4wydZkRnK9pDMksdun1ImZK0WxQ+RnHo8eaSKH2jDD3vREJOiNleuMYJmnBamIgJjNDQDYxZ1VxhAuuoF/y2yqy84aQCLUNkNqoViLsZ5mrOy5xZChlhho/IJL1UmPU9rEr60sng9qts9zEceDqZl1JUFNGSMuTYHOMASeI5amFjhOiZMKKHbRUvWPxzDm8ij4315QYnco5P9gjYO6uX2r86qu+6uLnJ4rtPfSnWqs1pC3Hgi7xetRxVIVqWGtNCPcxgU9y/ojLw/1Cpcla6COqRMlYncRHEhud5DIYNuX6e1OLaUlV2nrAd+17nnFLZYh13ysPOHQh+DSZR6giHSDa2ZXHhIHQnmcSCCx51W8vCCiMNkenTRbCLB6iAsBwKsoKOeyTGCkwyo1/Y14M3HLxBxe+lVcqg6vm+qWzKiOvnSKCj4F7C3spPk4vGPGlT6WaqG1dtAllcqwqsOKUj85m6f/29MOaGG7hrDKfsqSqJ9fOllzp6rRwCBmxILM/hoyKZQeogQSxWRWJUl+ptSOQyJC2aSfrMeBEZOoTFjoxMAlSEIbv3gzWGbbn8QFdw0wWiQhiZOwgQJf5FgbvUwpDBWfbwEZnIpMYXy/wNJDf+6z1REvH9yXnPCRcraWprc+Xl0KqxZoQSdRFAqMTKejWoytOTUQcFlpp9Q3ZzefnHNCBqMCqEBODVJ9Bunf89egUkkQNggMaX12aCSIhRUvmfBUwDwH1m/z2E8u4FhH/kygX07W8BwEdwPwBcJFBeVX9r8r/jw59ERiUjAPiXRZTI/JujaMm5cminLTTusgAo4BbsBA4iKuzA3wPuBpQQuOgpvsUtjUdR/Nyej46ApqPoV3CIaaLoFfYCMtFJPxOAPVqwHhSD88o5yMX3EK4rYXUStYD/IUT6cQVmrLRWBhPCaBXage9Fe/THgBGBSKARnLgsrj3XkcvjCriruTKu4uDqft6yM/vPHbDi2RhNpgFmcInJVJxeoekGgiNQP8+ay+E6vC4Jdy39o2J/kgIAE7JHAuN+/6ms0iJ7UcA483kWABhcH4wZfDl4ZFAxGPDyz5cpLw+/ePtiGP0NAsAHgM4QAEBf8UVB5y4n+kznwb8vvXPGTSXeh4zocMtXyhWb84Vq+UoVOB0AIxN8GRog6CzCDHNYbLDZcrCEo6W4eERcuXG3ktgaHjx5kalUpipEuJxEWGcjBX9K2wT6iNouu+l8TC9EhCjRYsQ6KMkhyQ5LdSnWUBEMk8762z+mzYaFGCgBmcGnGkKAmjBRCrJACHKdSBIioUVwIUfgc2muMTjplPPIYIhQIUGBBgNrFixZWYzDjj0mywg44RNyNm85qVUkVlvLxQEbeFvPlw85P5uobBFgqyAaO2ht9okwe4QKt9eCfRLFiZcgRaQjVtif/eggXPUfdep94woEgAUMgoUIva9QIZ6+vtgKCORDFyoQSeiGwRKRe2kYVYgSpaH6q+T/ViMaWn2whPWDwjmAYwuwYhSAtAeA8BWwzMX/f4nSvl0Qr7JTyNcYypSc3s895Gs7mr5lyAmKc0hjA36wbM6y3/UThtAGfCdZydfOVkJPYImixJNEdkAueCOrE17S5MOEKrbzRN/6lx0CJ4lc3Lch83XuTqT8mBuawa5d1Lrkvki+RRg5p4WJX5e1cGx/odMxo3GcQRtpFNk9M4wAGyCHfEDRioolbWZevBFTOAWuYZVMzPo3iBH2429DNqtkG2RrN/MmO7Bjt8ucxKylpEi6qWLVDhrsCSXZG4Jb83dse13P671uy+TKhvZOJRx2KiC3JujRWkIeO3N3dpaTbCSqSyopWbFfbPJOx3ZRvaUfK9YUyuDasGBZRTYWWpVfM5i+bM6Hh0RoNzZYBQqBvafNQxzz91J9hUUv8iXFS51jUl9lK+VWPmoxYa8a1cQ99PSi8np+6LfcbRXruIppfEY0bn2n2SW6PMAjkJYNiDTrRiY+dKfcdVd66D3empRqQvKWtpjP51O1Xb/DzZY6gTJ5fPUjlp9Z8cYFUakTV/CdEzH6KhXJ4IZKmGhjtkZNCNLwRUz6Ki8iTsbAKX4buCB6JpwYFSzscHVUZgyMGdOcp37dZxr9cbu++WN8H34ZxHTOiIHAOQ5olO1ngn8JCcKVfY+v4xIWxIgKYC7NTH/UYckjIZDkQC4fvSdHUEBvguX4mc3/Te8qNpsdF5TOWBjkhtn8jZx0+anDVs7XGdkI2p2yZJxnRUZ69hVbvHP1cgSEp3ICASe2Ib0YdaLLDtgjP5VFIeySotyEuORKbWVhEJb9y8FBXhzJgVEQ9kjyoHtRhHzIYb28sDAF8JvSAm5Y5LlXjp64/IzX2tvcr4LFQ+tL6+xE4bS2ML04lw8vTpBPCqk+lZYiSVckI0HHZleIWdvSJR7ML4b0sbFbCTu0hXVww0J+sUFyTmNxhSCtsFlB1zI0mzMA454nV0dieYpAhSpHAIIGZuFUjZgmgBAERapjEw1rMvwM5pcDBeSB8I6p7iKaFyd+thgDzC8WWZiQQzCiR5RLSzvdyrwsTqlhpk8McvMIWtATsxizlZYvuplanWFwCo7RmhNoVRmgcQ68z2WPNjzSWaVGCDjtHxGVkDcaVFi2dtkscZNsYdnB+CJkdRUgaeZlicfMfoA2EOqWkdaiiinnuqyQsXErpZQhubJccwsqoR0t9qqQFEAKr6S4guAhdOGsIBYis5hLCkw5EUZ0n9Jz8/gCfJlH+QQMIy8srwPSjg5rjsbQ8xjF1uALS0r/8RjEDhv6DApUb+gLcDF1NNqeGnSjPDjCoJlETSSBvkTTTKMkW3n5XiQpfASG21S3fvFi2UMM6lMROQ2MNOnlQIhEeVRwRBo9JuRVjqQmSwIqJ4lt6bCIXcAr76Q8ZFUagKvJx8b0eTUVeFUouiLxfdiw2UR8iovMW8uVuNCbVqt2OaSJIogVLESnocliUBmb8sXr3GzldfbbThTIITDCriJIGgEe7xpMHmRTg9KqawMx9DLNsEVFW4qwnx75WmOucERYzHpPQS1GQUuJdt/U7oCD3Ip/JyqBPet2+lq8YCcsyyvFYJF5aU05ps9wvjJgB+gqnGWH2IdBKKXtorRhbOtoxbHOcjAZzhJOzjek9wBU9kRp036TCnRTweXmkfG5I+s7NFxjB3TbYn6VpdZ1OauemTXpldAtdExCsYXZBrYPn1J4Xf6UYTFBeQwQbz8koJbYXsUtwZzOLq9jhTRG0+gSzhpfPnd2V0kQcNsB89TX0XcKBvA33w5J5yYL4DzZ51KvHOfznZTtTxwq2rbOfGXYmFDsQqj/vuixYMuN/1h4/G2fHyA/X8Nwrto6RjkURK/btOMBGWKkYgq0ftMlShUf8FfsFIawiZN92Og32J/qs+yX0fpeaBQT0FEG9xVqDr3sn60ODvqbLJEqdmnkljQxqaxdFKUof24ZVFaG3FsoBvDXg1qV6fYlEiVwmGks6b2qgp2rpCWek+zY0mKQrwrazEzNAZq0eAi5La2y3YhwcZ5MOsffs2/KYRBvawFBYPk2KNTu+0c2dlafsFuuRdcWY/XVdI/eOuXGEYO6zCi3eZQfWK1WV/abpk2D8ojXykytnqt/kcNfE9l1QnWSsGLlIHtcnaATxjgdKsohmf4e1Uz8K0vzUhBX/TubIbbSIaO9XNVra20wlxZNci93tX78CHNvEtSZB7kJxBuNedJJVL1JtueflBjSqQvlg1Ho9cWRwrq0UIpPcumQFg1IfjficIV0SIUBfJeXhcDoZWOhtLs88rI10MolaYqNjXXDIAghCXikwSpyOSbAg11C0xc7gtmYkyHEaKfMFQfFY84q16h5WF3nBlccB3tbZ4eaIwwvm+ocC/X5Tm1NJfkcRCv4OiabPS5XbinayYmG4SNBPq6M5PDwVqDmYiih2C555Ba92E+tYEw92dvyOwoVJIASmjp4vMgvk3n5eswOI0vEIXWkoUZLNsoU5PUtrpxPFpXVsSyWFZcUwiC+10tVqKz2JzmN1iy9+ENMzqqqGYav6yxdwYg96mD+eEUdoypkQHVZSkg1ORWDKU5LRwn5n61cImePcdZ5DNE/ky4qD2/wRBOXpNJm0ivhYRtkRvkqs5QzqrHceCYH4WuWsyo/MBrCmfQlao65uIU2Hsky4TmtBLsoiuAioaiMOIWwZp0d4lvRoiEXiCicnnVFMGWMqnyU5aMWQ16xQLssfmjtICwhu79CcfP8dk6g8WNbFM5IRVyjg9KYnqATc0IqxFIbm9URags73ciFd5iaXOb6Ha6UUKzQfm5QOE3fWDDxFnNlv/kBO1rULjv1UOorzW59/UR6H3anmrDQdTgd9ZoLlG6fcXY/heXmqYmfzBK6XZbzkvmSZNBj+3Ooj9CHTyMC8X8DpSJbjPO1URPIMvIHhLHztb4ePSEDIAl8BEkXrAXh28MFSr315UaERClvIsXMpbXFtleTR2sagLZ3BRzRdZYHu43J7rJmt7Y7lZ4zeatVq65uJfxlVVl1oh6K6CFT+0pADUJzCAZaCY7YHrGjpTnrcYBBlmrseHl+WJukZXYrmBGgKa2pNX1OzteFQbfgJow7ZqSHDtF64OWzKUXHgjPYcGaq/zRiEn9dSCGlWel/K14OydzSGBmQkA8j1jfdTYVy+Q2CwuivL2Q+C/Zq6O6oGA/oYSg0j0IsBAW93fDb52jpOVvuhQkjPtQa1mcsQte1a529iJhHptd4zRUjf4/j4NOituMvvl6Dsw7Oqmf2thzOrueODhLhie7tjyRdvZy9/eoGV/MNlG4j5lqyCt1NeNkyFCI+Il2pQmZZMK+DOKJQrCg/3ChcXJMcawSbQSzkIdiiYQkc7wrv1dL0oM1lsRY54ZSO2WOGUmKGnKKkDYGhTXALTdYzDPVndzWDoFxdQjFwkJvXGbebZ8X2jV+jo/aDZeeXV8/alycN+Ys6Lh/YGzcSUNPi8u0VVMj1Hz2S14pTK+UNS8hVqJyLC6DEIBSHkCcHKbMXuaaiYq/karhYnv3l8QB1hENU2+MROszr13N2mFnWTlPa+fNM0wv4igUa66114uT1Aetp7Yjb+Yup5MGrU/xGYZMv2NqPV0v/HIx6tKDXqy0Qdik+zEsTdgSWhFXLPaVgQnIKB6e5akrmw8gHaKrYKjOOcoM6IJrPpwF3tTamAH7zTKHW9PQfcsyDLzS2zg0OBY19zZojfI4CZj4tm7MTTVmohaIiVH+tiPerGgIj1U8JbG7MQ8kS8QE6zIco9pP4m26WssQoOpVHLLsR75FcEpVk5X2HNCKnvw9QFfoApKt9+6sTipX1EcdYrCUU0gMJjkAMFWiMzATqiYY3FGqg2nXEFfJoWozWx5tzWtXWj1J082y+PThRVB1oVVb6KB9D0RRWriQ7WmpWl1OVvxY5FlLykeAj1CkHaU1Xc19tyP/O+QcveLL9MA1rZtA+t+cb6bPFL+/RWwKHbjiPXJ1wY8iZZ9pUR3QvcrQYGxPLB/lQ0RAo6olWyiWBA8IiCBPuNpmEYyBRlLUngShaeLfXmsgv1uJhntBRI10xxdGwhLa7Bb/gcUoDoxyESlC96UZ81r+vqA5ADmn0sjPQ0xcOHsyrM7Ry8mBqbVg8vI13FGZnd006St0IJCZaecVLFL9ADHGCwkjEuHwCi4NKQUpj2h6tAqHUpYT70Nn/pZAVkGay6/Hx/v9+5DqFSDnyqvKW++neGVsMPH7LV0p6vpVHIfr0oDU/2zwDpDpgZFxX2V7L5uenr0aZa1sCPdi9OSy0pZwnv2EukBy8doV+5RNRekytgqyuBT7lkWumY9qDnAqN99n+Pu1ViePn6Ek30EsWShdqMWrfwK+vvDeiqnbL0rpsvtZo4CkS6eFkmtsG9mjf7sVQuhIucc7756Oixb6lmk5SKWPAQCdnoiqpCCQKDCN+4FtStZ7fARh4XPLVc320XP3iyn/du2989Nn/Hb5B7/3y6vv5VbPWWCtv/j5Y+JpdUI2bkC8tTX92TYD0ze/30uc+3UfvA+foqW9+kf3GGptn2tXAP/ZuKKGmTJprdWTDGZu9BYU+HSorD4rJVbATsxi0iMWGdRYt0hr0CgWh13aAlSxDqEQeIGwGub9SYjAEpTI/waC4QaczEKgdNUS8MFAGlwXyf3ADo2+o/JOenzYrWsoNaBxb53tSuIRYGXejuHXF2FVznUJdRfJIdTXXMVe7agw2q4FH+Nu/BtD2rXPtLxdl/34ZPnaYtn+csz3nKbsG7Hl8gi7+BaG1xnoAicjh7fT3x8gw+8cYr6+9dK/F1F9DiKb7lH306jtrYHYIH1lYx5CUBcf1Rzl57iTNiqtLXQj0Y8zapkF19Ujg2tJ5Q+4wDQ6NMXk2GqjGFm395mS6GJ/YbP56UX8pLZ/UQltAb/Bc2BRc1ESx6ijtpLZVLKAbW/yilRNtwZC/u2Rs8e+ttk/N4513AyHLECKREu6U+gPcJ4ZqJzaYsgioQw2oDfUxnRZ7x0gDy1M/YWv15Gtkn2PZyhJvKsjyadt1WV7+Bot5H5TwYiCNvvH5TnrnxGmaBUnc0g9YLtHfsnbZ33lLa1Ax9o1xn1wZNBDKSvs9Z9yS37VFR1n0KGXWbQHMGFO0y1ApUZQh5QubanaNRO/SErhKbcA0u8Bzww2n+qJvUMkwuaKyoBsUQY6Txio5tWLASU2NZcGNj/7SUVY9jlIWHeIWFNcnI0A/9jFx/P+i+zGbI0wk7MQaehULlWvWqklKqyNN6rVAxLJEiFRl0xhPG0ZilF6VX0hcqU63JcFGq05eHBQTlfMbQDCjxBGdFldJC0TE2zVLLUmuiA1RlYUk4Mmnr9FjdMc6IGTVHWitimfiEa1Jj+hMSDwTX93QAiYuv+//7Ifbs4KVypmlt5e+K7/3Poh43i/2SJ2N5k5nryjik6l+fSq/yDptWm7gbHUq7Xu/bT0/ya6YMsW0TmDgg5zRxPZ5Ua0PlwsYm2B5+8Oa9qpfOiI7v1gusNv5y7u+BE/axx3+QuGFO3NsP27eKPQ/fm0W+BhLgZE+wFgKPDT4wPrbV+pw1pT4jqhkr3itInIMTBx1yFMSjFR5l5aGZ3zvrTcpKdyNSYteLJAICz+FIRaqtOMISlsomy3/D15aJtuHWRxItuYwJ5pznsVXOHy2LR+GnZ3pHDfbh7kcBD95JSea0xvNdnC6+GFLRnw6ws48F7XOLnBkZiACYGcZt1uwxPgwM8ZURHWyElHaSnpqIsHBQE2k1akhSLwfJ0iNCjeh/ShuAitZhjCJIkD0GxT+aokhOL92UokuC1erdSYDugM1kTo1ruExk0oO1YLu3gSxJqfw133RgQ6PBAaDNRGP0W1FExOjOiuYMSY6PhFzW/hONUYQ9j1qEr+XgYhljFSpa0lSXh1QYuzRfxuna6ZHcOagHXaozNX117cZYVjsbL0u34hTmvo6jcmFO1xEPhf/ROgTJUKtgQuLCJXJA1g/LveDEnuGH68qVjx+jBr18l9DTJwxGWYb+ARuQvWEiWDHgIF9A/RAkHDbLIP0YMZU9p6P6Ff7wMAwfTKvNoeKqrGajnxkIn1VzYEFgaqIj3SZkOmzosu0NsbnzsqqARNGqBrGPa+C/s+oMZyQRTgpk6eqtrSdqdehuFKNm/AA2EnfXnGEjr53mwbaYU83ajToz35Oy2OrjrTVgj1jTHHDgY/nNcdczggf+LXxN9AhNvOzTaLvbtMJGLeYc5IGfgKXIf6hOynPXUKiz+VlF/Lz5GonftBziPuyw+JPeTWWQ7xPHRxGuIdBhYCpgvy9ATLpEmvYo4hhqy3ISuTqNmKDYxbh0hEEvhX3MZ3WADCODdzONZafbzyRoMieo9++w+RCDEZ9v9FJBFEd7dcnLYJbLHJbsfVvm9NH5uRuB9H3nNwc0nztbP0fhgRIRopL49tnt6hzclfOjAbnc9mODETwdzbMqRiNigtPgNPInO0gKB8WlvukLUZPdVNwEMsvhxI16RGl5OI/NLjgYB/TqIgA404JgoY+Y173HZCLaOVwnm631eip3BysaqLPnzX6h6DaUIQebLygNDw7DS9zMI6SuiZ/JpNMoEathsc/pexj1RruDLuugsjPU2izeF/2WRdBsyIlZfPDgMAVtO5nyiZyBbLPBLxnnrdOKTWDWy2OyuJqoE9CZRUBKY4H8+Q0Is1zFgkNhR8teoU/KCcMtBoxUVqtkdDnF+WpUZNC++SCiiC0SaDeXGQ3kXvoPXuYPR/QA+QGpJ8GIlYwsbUqwARQzKhS40b03UrV/Fbw7jy/YknbQJoYT9u8qK08aQ6WlfChchW/eubMFXxkVTEC24q/QNSrHOX8Gu0y3Y6MjA67fhmQ3LJvmtwQO9Twumr56zEHh8Lnpty0i1k43PCWxevbtJRhcHoY3n/zqdAxT+U1DLsGOzxV098HhpEtyae3MJolJhokv6IibJiTl56iyJSaXpes+X/Rj2eiC0Kys7sktTm1Ka/asJPTsoczphQs2iMCwivyr1aqRV9JX0c8l8jYUAk4XSBk3bwkYmOulhsciMXRqXpL8Ec5uu8Xo7TFIHX4tkyLAjr6Ff2Dc6HDS5etPUgDfcWH326QBfL1BpMWt5gxJorwqgzJSEPlQOVuUGKJzTl80UkDLssQpv0sGL3RqEdJs1buv9dI1M6MnMyhe5hbV6aEDrdVyjGlv0YOdg/YNnKOVedcz/nxDHMY5LLKEstZTp1tQoWhJGpGP36SZO+8yRC3LadzJ/2ML4psCqwUhDXXmKvaWoEri4rSasKUKj08fk4QescuERLsXKREJaqlKE24Tku5cKeXopxe/G1RcDt6TWITc+3V0vluunxPFipKdATld2RVFo6IyFzxFx4eX4qFhWRPZYEryxwwlC2qD7n0FKoQFpgOBwtdAnMZppa4y4UYpphMasxcSueUjtuTb8xTUhi4jJ/VlY6rM5k19fO0JvJW1ZvnneqvzM+Ksu14T96SYlclFQzX4+Dr7SWDi4MbkgS7ebzdAmoS8ZETNzodBC/XbHsmMIbD4vVGlx038Xbxeet4vMW8dLKi9Dz+o2tBc5rBaUZxlx3oQu9j4e8DHd5h5OUksaXswYQkGPVKtX4DpRlv5lbIJg/2JUWkqduli75VsobuU5sQX0KjdPz8WdXstEOokp7YTp2EXSdMq6/MnF+GjLZCVI//ss7apDVpREiyMyPdmfw6NHa6BqJ7pQ89Y+h4FakRiUyaxZuKLFvIEvzsvPxtgeDt5c5vwuEDLuWKbiUQ7uI/4vEeOQtew66vOVfvRWHodG62JnNXJucIJ1G5qS74ipX0XTKVfDVpzvHH3OPbEs/5QhaY8mMZFjcsfo4UPra3TGn0dPY4Z8engN+HuxJX4/h004XOY0tSUzF2IhtNTZOwwZ23RqS4jdLpbBTR5eui6uTqoEEjYryK9jAyWsLdeobMdVJVANcUlnhlK8JMoW5Cp7UZ5umuQJkt/FVOknIFadBlGAwKuYEEjX6zROG8JZc/Fiz7dfLtrgw2rXHqtd3fHC95asGcYGlwzoJTNPnP+teo4DwmK+XkXzPVN3L+aaXzEkZrpyMF7li3W+Qm8GS9utO3ivdqmG/zJPm3tOKl8Ne4mDrA69OBLZIkOxbUdeD45kfbpx1fcPe93TdSbdm/OlKe5Jvwe2VdrhGntCtAT4D2jjJ8ul8j7XKOuGRdfu+EryJpVcSyv3qkb9/b4H5knVcgtPFYh7ICRDNsJgJZVhVaM2ZdASwQpaQk2U/hmxx4DObvjLjDK0lU2gi0kXTchbGYXTGgBageZ83qwJudPKFtcfBW5OpvcM/UPRBciLyZfzsGejMhDumjJ7qwWx4shWBdjX3cDjKxUZRAB+L3jATfSn2Ay8/x3j7fy3747rUfGh5/9/ZP9IcKYgvJyTe+D38J0UEYVNRhN67u5+/e+Qn0Zb/S8Jc9gabjU0Z1e5nJozZvKkRt6vf1V/VP7gP5mfAA3KkqYE6pqzAIYMNXdem7rQ2fC94Ioq9+zyvNDOBAzQ9v9REjMHo7DsFDnQyZuVgkbz1cL19iBPHchPMcPBe5+PUD+nE7wZek8ZplUwfWcV81vgwfvDZxTX2QVggD/+pJP8GflFsoSfzZIqh5LPLnZb6hNovh7stIacrr3ZAnMvIEeD7iQzg+TmqTtrcTrGvk1z0fzseluxs5xhwDxsixbpg9NVt9MtETaLb1C5eDppTrWf5aeSF2ZBEb37AbxNW+k2jz/3YSJYodOVPbstLiFCXM8QehHW1cZTE34dD+x2qFdC/h2gUFUEZ0MPHvGv4dPcSd7yqXES/QXXS3/84/N46aASqS+LzSDp+vOzAN+raeYFhfCIdInwmNOWugl5T0LqWeL+r8jcr0eiu1dym5W8cy6oUVv//GBHrfylq7UJcDz/8Am6F084+Va2UfZpZYG29lyb8RLPuNdZuSy6ZV7EFWd5V2sYa/fcdVJQs2NWtrxMwDFHdeFbfON1CwF0hPu+7zbeGDI9137Wu5Slw4o/KfrQ+llON4QPzy2uaJuN5JmIjBi6vt0L51f1ae1debqHl5yWK8Dca2mNMat4D14tPkkdmnRHuQ2S7HuTibJ+TvWaaAK9SpefSBypDU0TBYV60ve5xSabV2J6ezl48X1YqUMSKKUq7h00nHAj9tsFFutTYqtNWkvtDvUW0LMSelhNlqIIpsJsplxpCneT8l+nahOnbnfbZPWqqEnaiFThh8QUZoMi71lIhOurnCL3VyFnhSp8agp2bG5efLvwiazjJFhYLzB+FWgGZmHDD554hVQXNZztpSgxzgugmb4fm//FmGv/daoyclNSphWDr86NzxObt2MLI+8+qBUHQrDt3FyjLnG47I5G6OOCTLKjw/2D8eRbR3xRDkwHd/gN1w/da4AUE97Y16WFYJ1N7KH3UzUIcOHle+MhqWhR9DE0PHHzBr+nMmt2VNnjs0GjMCdg3BVigF5zdyIIEmyH37gxPQj4dvXRXLUFbZweIau9oObHtRBOV1j5t8z020BhX7m9MzmiNe7//oHb+z9rlz//D5vi/ZFfGnYxMez1u05os/MjLivtBqwQb+wVSBIPUgn39GiIMQngHxvT3+2IaoKJ9Ocrz46Rf8bgcFq+d5YlciPXRsTSTYzogmvzLb6XFrQNc3lsT13IstF3vp9jyss047FyOUYtQFgNlVIzluwh3SQI59tV+ov1Bd3MT4jIgKMZmxnOeI50Iw5WBGs16V0X1M0x7ebwnYQ+2JOclC9PYTtUAjo4P5BgN/K8m8U4ISQqfS/qnIQN/8IcFPLORasrra4ZtidT0E66bZB+0v++wJOXSMvx3jySsX1+H126UvBtizhrdBzBu5Gzds3sxzeJRki1sENQL7ZvvoqDKYm7BokwO8u+AJePwHf5pAi+uCMC5f+DHx2NGAoi59hLPFjy5RQD5B6cyUlq+hHvyVOGdZKuVbDFJmXZJN6v2eB/feWtEB37oDXJ4zq7ynVrlboua35a69d7w9CuweHmPCETWOYhocCTGhynBjUITPJV3RUUvDZ0x+Hkz//2MbMkNEbKsZI1b+93TUSBgIkqDTMYy7MnCO//07hAO4BBcAU8p4T528/5UL+8Lc3zm/hS/uo4+aJqe5JsUBESvpvqyYPBWRmKpVrmn/VAWKYV8XZVxR6mXw7sUmryOi1UYcJ8yqNJ5gv5gwFjkWO4xFhHh/9j0OzUTSODst2UqmZn69N9TQ7qfs0bkrquQtosbcnE7RYlEneM2i9SXOLoXSUhj+KnVzljjfDAVKrTe3wK5g2EvOZqT2eFOSrblHv6UyAq2vLKioQYutlQEX99tdZ9/49IvMoznfwsTw174ovwM66yt88FnmWfgsSNulLBWojV3B5Y/yHDXXsrMKk8vjjdM8wVqN1R2KTS37P4WtvjiQ7mpysfA9TT3Z6bJeAafw/d1W1HT3Tr3W4kJib7w7KSuKMNXT6V/fXMvjjQis0+8/WsUVPJ8EhBFHuw5rtPRbsMZ2ncPZ9gu1NZMgZYUSn6ZfI/GWiRBEceADyVfIwr1qIypfIfe6Wk3GHCIVpKSC1BVqpIBUQr4uGeXtN60X26k37Py/srKXlpQF2pVfisVfenhsuKZM+cB++B7zAO1typA0WhquXFMPM2ud69nsBientqhe9T9rjnyKpydnSZ2MMy9ndMfirJyE/4Hw5kaNiyC0nzS6A8Y9GaW5CXnChNycp4Sip0CNtVDQatAmIsPV9ImbnbrOMpEfBFY1LaQ9+vUVaMsGdlsSFrbotyKyfxL1wv/pOZaeDM4fnR/x3i/2HFdOfhjCfXZaW2F0/FfcjtUxVrjpGRQzrzk7I7vs/nq4UzQLfrJrNp8MW0+z1A0oDz/tCnwMIdhLMp2H4KEO4DfOC2Se6FVgdYlOGbe+zk8YETjfnpXml0oDtLv9RHQRxwjPdP2KHsdlvALbO6hhhARem32G/5Qm0T3SbSG6qf7KNH/4tu11FVdmwUG0PKDKE9iVkoGpvIGtNHEIHhqgwJN6DK/5p2H1NeAQ/2fNQw8C2TbkdfzFMwCAAsE0M7Xn73srf7HPIZ2K0P4HjKA/99+2blbcz9LXxu5baQ9Hc0lbVfzYDOSc4H3+cd+p0fm//QR/+q30ZV48QVPlcHbBTFi3/EeEOT+YSLzCwFeyjP18+w19j5YVhKX85zOiwAvGjqTYs66ZkAD4ZQD/qHH/WxLQaPfqt87lt/cF7nIbVpdyAKzgsDRRMRPAtY/mQuaGPNjwsCEM81Kh5YnFsN/ZB4uL2/nZbdzw6hrIR13fOh+GZp9FX3XCul/MGBnaD+/UXdLAiHXqGIT3nQhsnf0LtzTfiMEwML7HS+l8W3Mag/KwsMsHT7Qm7mO+eiHms1BK5euYr+pLq8ZDe28wL8oUvJkx57HlMeHVA6IlDSYbXnxwEIwZRF1AMjPPuFcnfoY/vzbRtydAiqn4fAzC+KtyxEP3xJOz+NxqPKRlcKlsqfzi10A17pRiCnCAPVCJ8N7iYnJZ25OW1A6Pgtcn1pE3JvKSz83FleBR2rmKIXh4InPwtYnw5/yRKsoD2T5/5QV+TxHEyGXPWi9+DSZgJtwMr0Om1S4ofxq2WOjzYzHDL+VcuWej8Hn34rmL5/2krDsd6JhX0xSi2xiybF7rlVkVddu3hfzgiLdvKnaUcS3lRJNCX+OVGbrwbdtrdVdmz7NpIfWWFrr5DM496ESh7/Bhb96VWZGupcxSQPj03Gyxo1K+I+zXTPaUOW0/nqlIrIp9wZrkbrCZ7i/eZH8NEXFs+sYSnbPdjajullYhq3iuuQZGzpTLimX9uLwN+Yvjx8U28hvaGxNhWP/6hOyG6T0I66K4FmHVB8pt/p0JzV5VkgX7ymo8Tdwnn3RcYrvvJ1Di7uuaqo9a+lFbF28BAUYnhIcefO004DkFkKJTYl+Gt+v0sB8TrxR/RlrG+zVxjy8jvaMb4EUEHPN/q3g+F31+2UmrOa5aDzIHwqvDX/DfqAF6ANCB37IGkSIgfPNdaODc4f602TmkaZRIlsR5r1mOzt1N4HvmAjeX9qPO2fv1x2ekaC/5j3lrj5JSLQb1i2jgDfULsLAhWY6/zajdtPZvjQ9sej91ZDk7r/Vo/4dl8/ZbjxVzMzt27Y+RbToT7mxk/g91KSJLcRl91y9wXlGzWxurJ+/ddj784R9g+DlXsk8JFpFGg+CjD98nJ5di/4x5QNLPJMuruhF1ScUf1Sy7KWW3lQho71dr89tHbDvnOI/k4pfKBwL8LFpd9DRpr94+btvygy6hsfYXCS/v8HL41uvF72z6aTCOnwupuux4HZ/V/PSO+p30QM3j3PT1uqbUjFoO4kPyBbiJV5i3Yb2wKYVTkh4Trx7lbMoCC5Xu1IFILflKq6iksFRttebL+F8F9Q3Rdw3lo4Tkw+fbhVZhsbSYtAaEhJNWI2BcN7jLPfvpQsHFHe/G0yjDTsDXKB/Qga192kpmNmtzfDmpvUmJKfdXSUKZhUXX8gXR6S33ZtOf/k4gvGcHBD7jcz1wl/uds7X4YXX8nDMi24OCuLwKvZgguYgIjGW/3ya//fwd8lp8Zderfu3uaSfhZ4sGtx1PSnq7etk4710SgpaN1e617Ex2OpfN5qbHyn9iAwjXZrrZRWdm0k3dQLiyvTcU6QV3V4Z7q5rD4S5aub7X09tb43a6nMAlqCRQmYxAlXfZ3lSQlhaVujiqOQgAEtele/csmDUNWKUT6QWzcoLO6T/+cUSBU/vsuB2gd/ods0QuJMKCz524MfPB5R7mqTt/eHOBQPKwhPDB+G15lR2c1oiis71jX6Lo0+q2s7LERZiznzS5M7I3E/M0cPmvBtO2PFF0UL/K6GTB47zzdKpdZyegaCc3LY73Z+YWPgIIALKkOcaWDvyMqfbpkB0FZcjc0iT2GYup9dOZ3ZaSfcewpC1tM9cm886RRx555FFMMcUUZy9o3SPK6WnRJO63yDcWQ9IBKS3K0aKB+2vkkLZbhPkS5dbY9hpumMfWyL+2YJadWe+FIRHSMRAhF+hAPnguDnYM8GA+HOvKDR0+diSQr6Ma7s+70JMDdGkIR6h+oC9YMDfKJzjy/oUoi9QPOBGWq+z+cN4NF/tgudadC55M/jm2bRJ27Fdg66kBk19HW4wovd1tEUwdnMNCRY5/j5VGAwXd2f1IwTmi7eSOEC1RGKyhTGsO8HSuJJ5OY81QBMlkipT+oM0UrIuz1Q0JmJLPB0+bMKcHznSHZeWZ2zXnesyXyD/rLLjDf5k4tz0PFbepVk56BTKBj8DV//VZWSpQ6hw/eNQusgnQDVACfx/MbjsiuPd0swI9edXAb52xbnuKWC8Mj5iMDDB/E626CxTp4ijg6WTqPQKmhV83rZvnRLOAO0zw4gFO0YX/impPgf2wJ+zYT5Q7DUPaSfhgophoJoY5wMQy8Uwq8ymTxnzGHGUy0LGv84aNvydDNjr37qd+Xxj1jwAQt6n5wPdf5EiFoP5efYDx2QfiEjDBXW4XkAB8hNA01boRhob2IBzSfE4Eip4f/YXQDYBO/m93B9cZDIUoUiTSschYgBPZ7QTwZca3liVq0dCmcDBgJ4sJhVBBQxzgkzuhCFGKHCWqSh0gvS+1fTZ/rUVjkJFDLUuGaDzas8KxDJjs30JEx1D9Y0eHaRulsuC4LPLDQ51Kgq9rqI5AsZF0q9kUqMgvvV8EqvH7WZfsY3z6DFoJFzungcWQ0L0J7zBmNc6N/ntefSGgsft+jAuL5x+G4SXHVXBLVkQab0FOd7jEE44YVYdqldKMD/6A1v2SBwTdBbbJ4RP6ru7fTEgxI+CFf9NZnD4LfyMCa/RhtXbMW6MLpEXwW39OqvEPWEtY494Ou1s7yS3HcKRRROT5X+yYP8i3EG5xT9q8s+kLf3gv9N1+wNFyUs9Ng6byufJsCb3h4AMuwRF4tVRjCUU1VkB+MajTjA9U/rWKtzyua5Rh72izCj33DZoaEcpzE70fcAmOwCtzYzGqxkN32GYlruEIlL7/DIV/4rjfADA9Ffy3JrA54BFyJyUjt1u2Wi/NhYOWIze0Gq1FG9A2dAhl64yK9ZWqdUU/6KleyIRjvBj74s14G96Bd+OsOBFN8STGbUuQEvYTEgknfdoX/KUrfdPP/W8yiSyiEzGQqCXqiKHECGIsMYnYkm+KTpKTokmX6mE9qV/reb2qD8ONvJkcSNaT95FjyInkVPIxcu4oG1fHD+PPg0Ixo0gpMoqc4k9RUZIpaZTvjjfNoq6n7qKe6u/6af+//+p/emESaCyaJ20nLYoWR0umHZ8Ns3P+u+h0F/p2ehj91Lqx7q+F03HRykWBi+IXHVlUf3ae05vMYDJcGX6MRMapXbsf7/d7/qLS8nsdPdOaqMSq3gY7RHRIVjRGtXrtOq1G4FhKi9MftpXkHoiFW2duf+fff87uurBNO2pn7ardsZnZDFqJBey4Xa3b+mzAhmznPB8ttRRYullKLaOi+KglaosWYlWcFt+O/07kybGkL92UJqSl6a/p83Q8nc8sbWxtRDZim3U2G3//9orKLmXPoBmUsbVQB6NhPMyGtfAmbIf9cBxZ2NrbuthuQUfv/oHdgmYwwlocj/NxM27DnbgPv8CjRGh3mjrZ76UHaQ5tpK20g/bRdwyY7WLBYgXbzcrZLdbFJriNg4+Dkmt5HM/jDfxn/prPCbsl3CWCJbIlOhEqksUX4py4LO6KTvG7mBDv82WOiXln/iqfLSyXSpemFu3FbMnhbip3l/FldllaXi9/Kl+Vb8rZilmxlumqG5LH8+LpZbb8n1ripFM/qn41pOY0TVvy7fh8vgc/SO/Ryfq4/kpX6yv6mv5W39U/6J/0Y/1a/6nfGzCWzk7OG43KhJp4c9h8ZrLMCfOFOWdKTenNt4gFAFRw2uYgId0d9J+2XJCN2GIwRp5uzDUElNrf//XT21++4OCA8GeotRUj0Ra/BghvEUrHqNZvZ9f4YGbzljYbk/DL3Npj3HffgfyPOjQK6F9e5kkj6EF8bhVjivDS7Uyti0mVRIS4grk22KKv1+jaK3mUCjvlkw6pIIk+UyHDFicfFE1kSL4yWiBrB8YkjOlnovmEYbaifRGTG7ePpGa/PzR5gzNXphKTsITdciQfVAWtB4o2w3IMhHDygg+49BsR3RAAKEMGi20/9KHlnA5TecvZuBYHfblPo33ipjDaUXV8ZbCXUNM8laHH7EaRXJ8ZE6tS0E1s4J7ehtBCkFdYOw+cxBkwaRQtWc3LQRyvsUQ/A3cJRv/uK1wBkE6rji4nTWOZFkPsKGdNlWDD7Knvm8AG+zy38QTnkXWE0UWcDpjjNGbEfnHuf5CONs4b5zlCmYDUjh6rspHdJB46XFdtYYIflRs7a2trO0vkCAs3M35RGCBSiJHwwM/g0wfqg4R6zQOyYc5+fdBGmB4UqX0gCKSANOu57M7MxPLbQMAiB2uwN5Sd7PmvCx3w2SguzirokJrgBK6hN7ORmUYwcdsNHxI24KkaIVlT6BXFO7JAlRmGZjfviyevLYu5exvUN0BXgmOvrmGmo+3vnl5d8nH6W71ePfobJeLnQAph4RF010ZdI5tZzz/nV5g9c7mytbnHX/rjye27Tf3Qxl0AO206HGHphjJMbXUFUDsSNXT7RTCuMDDELoOWK7TjDhwfHwoFNl+HEepHJuAO/70nHORVnLW6bgEUlFpb4zACa2GMEMX9BCFHVonVvTCl9J7ZFp2M7cjQstObYJerpiJ3PJWh1+GXC5uPcVzUh5N0ywFPEoUYrYj6NaT8/TeMIFy7GS7OQG16K7//Xyfs6UmJRBDlD4VTUtCqOLRT2sUlvwnFAZwzjgPS8dTsh1iVK3cg4B/Bko5Lh+ncVyPYVmfMUAaFJaZlWMs3hAbRWn5AuOnfXR6EDy3D0EicTY4ZM6NL2ypE6ggHVj/rr9SiBElPe5e6CEFi7lAUe+hF5IEp/M87EjnQKmPeZjW3un+YGWv2RaDIVVRyWeE2sNty+loylRRpY9CsI4Hp5GS0RE4qlrx/20bC20iY5PYkGtii9hNUSLnW21lpttxvNqATlp7cBmCntJeeNVAxLRJLV4PIQqNEPo5N+p7qDL81+mIFy+MYZjmjzUGbu/NuJWsPTpAItd7BTSEGCQTupiiCvdleIfebnOB+K1gvUFaJ46detRdh/GvaagyMyl5rpA0dGMXThXWOIAQL4x6z1nfO250KR289FPVIb3GMji32ZuvPPTC1/e4X/Up7/1eZBemBd1DRflQUFBaU/v1uDv8cFtYRRs7y0jIRT3TnX5nvLtYEzWM/rFZKlcpZMeAt6fqQPvZIE8xc+WWekPNo4vkLS6ZVdhnWmki7qB4RQmi36cd/LV6dHB76Enz81B0KjLRXtDDZidJDieT8T5zlffeeb70QbXBsdzcm/kq40YRz+8v5+FKIheuaw0ZDIkFPG0VG9LT3SIbmBSrkGhPRHaJ2W8bF1G3Y93tkvI7eNgbYVAZ0FFzwHHK91tOgJW5VObMN5LDMvu4MyHGW9rEjATE4vJxJlmsh6nFMGaO4Z6k0CGGELFNK3uupxqVjdEQfShG5HuIgEpisrkRvpDzAFtNoAcXx5DByR93NkxAN0NbJj9e7fKSOLQdoDzzYIjKBMabZHJYcpM8larp9LttQLNdnZRN8RvpGqiLItlLrr+J1yR3l49ANy+e+9UghI2lKcyE2S8SbFFIHwwlQgucQ3oze48amU8UwUKcWxzgptoKy+ACmdDxi/QeJSrY5CNVkUVwNA24TZMQ3qMu1wfEDMLWn3Et1CqZNiNxKgvqjGHaQ3N6FfcZ8JltBtCwJbckI0joYbVvR7J/umpWmpdEl4g40NMndRoL8zjMbupDSr7twuToyOMBiMjuiJB0tfUIurMfYNeTjCxQqhZBNNKILeGDr4EhmzhKjj9wmIkS8TTJeF5YM0lfC0lu6zXlOHJkqm3YuFHRdvNSCd6hKB4j5qn1xaDHluhBcTDkh0ewA7D2PhgNOVfPUkiF/8984ze099Smpl8IbL33qDfRpsy7/vwvZs4qQglXMeUmZcauKa7zuWc54+QgwdExOwWu09RDxODWHQdhMp5AxDo7ZdXT/VCA0goOtTPELcKXtbZqN5+MJiULmUemLo8dgsBaM5VVzKhIx7J4CXyGCAGQdge0VNAzjI+Z15k2PfQceAyLU8Z1ok7DUFBm8JAzFOTQP3E2G4smHQM23D1lw6RYx1vhamYe+XJxzfweRI7bpPNscLftqcIrNyrGkBCjtmbdefXc26mem5sTHB1C3NqFpjIoty3UsM1hSmg70IALadYULGqK+a6RBazFW0XE72pQX+JYFBCciYz2EzTgyJcEOLufbpqobtH/2RuBYMb0Hm/qwI9YsCE66HRn+Wu/yQB03gB6o3BcrcikE/sySc4dUL6bjlbpboasMp2J92sB7O+M2Vjb/eRgdkmfUvAQZ4nl1UAYL2FwbDAs4upAQDMZ0OsB4UkChjPsuHKZXI9pZ1aLLzeqENnSA3T2alhBTtY+gK1Fp9GUsoGSvr3c4VfaXTKNIcjawdc2YqkqB2BAeZT/dkOQ5g8PBMaYmr+7NpCwfYkF6wIIkTlX/c8thuz5pmudVQlTqGvzuf7BVhGhMtk4xy7sSHrVueMgQMplqFWK+pImiXJPlXugp8IjnS60htBoqiQdB0gQC1qOOnKkUinYt6qXtUBRNfFVf9qs2nuRlIRzH6Y3XBQeMcb3QKkbjvtPrD2CP9eyg7vDNvEmLkR+Q203JwffZZpIdPRmtKXZQaQNuBT7Enbpamc1WbtpAiFCp5xlk1jW2E5lQuqeFjgQLI8GypkLTRecqM9UoSeRB/DmNE0/sqFItEy9PWiw9nO6nhdh6PI1xNw98YK4f3AlMGhUgB+0sPD0CU9o87/LVRYMY17yS6uug7XZt8SQg5NRylL2hJzmkcBPYh6KSIKLfsfSD4pz5Gk8zo31Cuy4TjTfQao8F1G4XNAEvCg529dkVXBRwjy3Mxnpu4s+e1ctUQiAaFab+HmEAizdhTh7P0bpR/t7Foh91nrBvXsj6tauQP/kq0x2tiwqcJl9neuK/3/ulv+InmKwZhYb0B14MdXND/M1SHPyXgUV/AqcIYc9njs12Zo/1EPniH1f5S75aAkdzdKig3QeyNg1msr9ROTS/nV9nEGnvndDq2uWbrg9peGznT0LhzM/gmwccJc0Mt1YxOE34UCc9RIojleBpyXAIZPRkFd4zT9PVtcwYGxrvlNhMIYwpKlyEevWo8scASZMhg5ANuRIhFmOqCqmzEbNcyhJpoVjoPqTpWYuOOj9wnVAMaZ6yUhjC4mDa9yIRcbT7TJmyB8OTTTgD/6iVWQKV7SFmkLilKIY1K4I5PDvThmx5rthT8jnqcBP93jFPD8OpuQPFBz49HwcxALDW1G0YwbWmNKQ3YVnuaDQB5vdtTcznLIth9GABpnzzBFK324dfpLJyPq0jfQYb6w5MV31tPzLqiWlOi0Xc4D/B5Lw/QntSQXBNlU/8eILcDU5M/vca/7LOqm9bc5tcMDk0YJ9t9gRCmtsIBaz7ZBjcVB+pg6bnXTMfGwxnbvdIMTXhz+58j/Zo2Um39ZUgSWmSRGjrrWmfhIRUHXcksHt/NfMoTeCDM1YhMPkqzMzGm36nXTiM70kTYvfdGg2aQnfvrW83oI+R7/t+f5M5usRoXW9HtR2LQWtjA3Ab/ebDJKiPqmTrjKrZY9K3D09OWPLhJoAkAx/NSixuA13fReSmZtjyUYLhMmByy6167wk9DxKDXErc9LgdpU0t5lepIzyPSjIuhKp8l4lGQCVmTFBwfyKzhXlLpp3g0lubccHbYKJpPZ1knqNo447qiWJWOxkbU33doTpuR8/Ufb9h0V/WSaA8h1syNtFAeZxeZjcjRKqC4bgnAY/3jBuIAawxdQadN0rGuYkMrUc92l5JW3dX1WWWlRxXbqhIisaH4nu8sgbUaeeKb6sReobzWHKYB/2ea8Z9f6RWbxjsTbXvxIy5TZfi0MrLMjryM0ejDgmAOpoOx6KX2WESPjHND/HU8Iis3Fy10TMdCE+Jw6en1SyIknNtMYIW1rnoaGZZd9mo20WkkjmjZklCqjTkLnP5KsZI1Fpq1jZB2JxOYAq8RzJ4V/WVYoThrhkJOo1DZNYtsV5qL3C2VIiMKdtnDQhAnDDJA+X4RnFF6eGmONIwNUQwhD8EHmlLuyXhpnF29jHLoDuJ1wCSl2kRpHECskxCdz4W+BGZO8KYAIvjKPNatBSon6Vor7wIF1iAobZJGPW0ASkgWB3HVt7AlYJTny1BfACa9aHp5SnNRQlZ5arWrbEHeFDAiWo0lLOXDJ0wR0kv3yzQkTHRTwU7k1MjLXKt9lyVnMjQPuQSKKVSmKbXu/cRAS655paqroYBl7sulXC2pLAZ6txgob9ZayZDKOuBSzQFclMhP69qKy78DEyXHKlcUjKN2fENucgdHroz40XPOz5XcMayaRgKTL5OXokakMlajDxdnbFZpbqtaGYmHos+09GCC3G33rtub7FCKjyDH5TGcNwWEN7ES+Ei2RvveA4P81BEpTt0Qjc0uRbzHZjNJFewIT2BSwXtFyVuxvUmzNi8F08i64i0CRrIyUQOEeFI/u4RQRsiF0IRpZzXCMxmo/TWYSV30OjoiRl10eRT3ie+sRbbUtDij1aTU0KDNnkyv9jyZcrdpdaY05/THkMYVSbrmB45dOfelkZJzRZS4kiU8mZe1cmZ3BEpE02SOnv9TJkxcYVg89PfWub16c1li+fY2XggKL0Rryj1D27FuyygQgdCs1XCkJzPJXIMZR9OMoPXSA1AEs84xVuVFUzB0apMuJ1CgYrJp/2g7gZAWkBSriHL9nYQJZDfhbK2FiVSxORBe/ImpoJYYg2S7TeeOsCecDt5ikEnvZOtoiOY/UR4y2qDIf78yQnLeTW7//xNjLHRSADQjkxAQtQhhSdToAY2N6fBrATrokgwsjF1FJCz0Kn1PGgMVttAG17uoO0xzS7FedGM8maEZDObOY7KAdqRGMhwDxIFuUlKIOrXQvSNbUb8LJbmygemBGGZvWU9kzkqQtbPSpHfF6+91PStj5egje2M0Skmt2ipud/VBnSvRxyES6+BkEWaNJAFxaXgmc95cORD0t8kNzggBBsWI7ow3NENRXtFIUlSh+1kKFIQMX7BApnCg+rvZlbq44f5KPJjsLgn8/3YPvMoeN/76N6Jl9npNBv7UND+2GPg4aMfvJMvbzEkdg2sGIHZeP5t+O3sOU3XYMIylf4RntJM3Sj6PQCH+iYbhgdV+W+zOee55b5/J3+ENgkYze9QupXE9hUmkgYM7bjDmAnMAYvoae8wL4SlB+jeywnhNx/dvXf3b/Pe7XsP6kZflWGOBXEUMsJLBknA4Hj1WAsUZT+oW+sD1kRXvun85I8nONhxkLQ5sP/27fPnzn7AQu5C8bfzYv58RuPdO3e//eZh9vHXHo+ds/fJe3gWaJtlYG9J/+qPD+aPHbqLnZBeRwnrrVlbbPi7trD8V8mq5JMP2Ky8hIn/Lu90PZK8QgSKjKW//zw48Nd2/JtrVDdWDa8gRJkTAz5DDzXmChnjroZVvxSvuz4SCUatJ3XXcEN1wjwIVmeEGlke1B4ohsnliXNpwiQzxzoigciGxJEAIxiqkyKhzOuVrGnIve16aiSeLsGl15adxYGaB+lkQz36cY6Sl5EleObzvbpK5tVqG4HGPBGUy/5RpSUpUGVzeyKLVJG2mZ3IM699vv+2enwq+X+tBZSrCQ5GZ44fJJ+CyAZ5vrbynNLKA+hzAkGbmNpDKLIoKT2tXnRK5dM4nJVmaEw1lDIPMgmjVFf4m/ACyy4/FpPci7hmUMHyrjuuXOQfCF9k9t7NIQoIVf7ysXiH1UMaEgUTo4Vx5dy8fdNkyZABPHQL0olDjB0Sk7HZyP77A3wdD4p9H5vKvpgoWvTrBccdXA8Y9o9YuFNi6XRjdboGea4GssJQtj++QdVaKg68BULkB9ab4ZbMwiRndbZ7x5U2Kwnrd1S5ipQbsp6UmVkjcVylcAYaTDjgGaf4iUuzY82ZqEC8JMCmxPTPkRL2NAleDzUTIBs2zzy+EIZqbOOlwa543Wsd2PNNnHpiR6+VfeUOyzHwfj1TkxVRqzz1Okx0fIdJN5iAvNWDzX4nHKLxYk/ZvvMCB64UE3wMRk/PrjqNi5YV0WwowybJ85ZtlDs4NSY9YKjJu0fNdsW2qtoc/yAmEJs4WPyAreHF6nQKzvAoTU7bu0oyTOBCzRIbOzb2YkUxdINoxAzpHNJVqgb37RiYSongxKo0Ito6fSqQTL0SYJfbjEjLaOETqhnvYAX3H1vYlmn6bZfSS/uv9XQBHNbwsQ6oJbCarUBcnpkZGTzsqlStaOmQqG4sV4JitSDmJ8T+/rRiS+oqJFwuuh4n1G00gLBYi7oqM+sNeoXFlGf5nPnwzyvRGTq9WzYCUg5gfBeLKzCT6ByWAxRW/ahU4WFO3zOezms1rRFr8sh4vYxgXo8kyvfgCrmbl+ZDQ0KGEi28am+2NL7Fh5obtS5ae7IjpPhBwUrUBhoEIQi6ixMg8gFqa5aYyyUgnxAKorKrVLxq+TvUTgHIByIHoe/hccdSRlFS8upmGzz5eX8+KYHg5Er6YgBPvTUqWZpEm1sj6ARwPZV34su+FB9VqzWY1JqIQc3+GhWvGQTO8NDO60mM2ZkDoYct4SH9YkmlRlS5DAfb10rqSLPZtrhA/LRXkSW+5DGVtX0rZSOpLmTmmsEFDqsjX9X/zzqUyfdOhb7c4W2AcRb31Sdh2ic74sruRu6tBCtkN998JwBOAh6YrmnhcpC+0jspzgBJJRRuktt5Kz9VFUutlZkzrJKLsztuh0f2BMhuQJeA2IHB7j5XXm0sxBPKneID0w2Yih+S918l20MJzhv4XZGhVFk7v2BmrbkEEGFlKms9RIwRE6x6/gIxxBjBvPDTdKMyQjn8Oxpj9MFphhR3JB1US65KqqweUQqvBaN0gHR3rtHoCGacVoTkAkgQkiE6eyUaGoUUqR6EHzNJOmLGLDaSCWEmk6SAGoe7/+D7v4+F8aCGGiOEhJTdudmVnzXF0i27jF1crM9nspZsoh6aijRZlve/3G7rWX2/LQAQZiyMZCFJNMl2TSQ9Bf9Ik9zQUqitIapKoGK9puEh3GwmhvYREdJ84Ydx/AwEc09WQNg4EQq/aBCfUDsLtLVNSKAwEbabDtYKvteDtZYAU218W3YXR1bEFWfNKh4pPbjW8qton+lmEDbkCoTgKu3Ka0o7JaRadmhdRW9Ku70eNwzBPfLBpfbhz4+oJgJYgZ8h5bviReZqPvJX46tbalfobwqdG0MotKQVMMu3ynhl6MeNuXR/TJsfmCLZfgJuDNQPR6SAPFKvjvKtAqxBg7o63dskK4Q4foaiy7Z65uuxQrPPntVQQ8nxzcIKLG1gR+KnSkfq1srG9HzaDgUnwFCVlfngdDMtJmdARpE3967LqcQ5tuu3iyK81R2zbx/SfeCE4JxdKXBt4eH6qH2ELELgnicYq0DQxQY6zA2DQgyksdZvVbSN+7AByJObxI5K4tKp4wXRZxxkzDq9Ovg+CQbFG7PdWhnKW/CvQWFdQNkZ1ohP+TrP4itTfILNEaOQF3p+Vl9zLqQnGXNViFglYtoVE3VriPSFzmohZqnYSchZQ5n1AWqOLo126lxg1TF0nYwViELoFFjmpLVmStctgr3zGaRKKIbvUKURjSRE/RVmhfqgEfgYiNLSAC/knHHSK4jDR30vHi8GDI0GqmYoa8Zp1EnBjdAtapuxrhC86AA1zRACU7AVLBzV4PGeEhIszWyGkt4FvbclwO0BCk548S1PBhP0WyWRQUtxQFRCs6yEChr3mvfGWBAFSVE8ki6OQlph6anu0435JbN/QwN2V+t769K6z7yJqv6i4/3V0t2pSeNCVJbSvpJDb4oIK+IDNSkS+wM481DjeYF170yrwqUw59IwzrA4bi7yCcqty1GT96RlKFk/WbE7sVUte0iWZUAh2xqaUyS+oydBnTTh5tMozj0yhKI49bQGvLHmhx7+bF5reJrOZtClLVyIVRdhV9vNcKe87fd6zjqm09kMmmsmkRdNUWgDj38D6nrNZYRi61tstRjayjKMEFER2mQj90Tgn+ShkHn5aRRj3wAsJzKlUpYQb1j+NgwpgNldIpW732qD52A0K1Em/VIqVUdkSoiUBOgGblkAY1oZg0iaSAEAfaQCrJKJY0zUtKCUP5OvdJKnomp8Wy10OI8q6zyhSNMII5HoomvEpmDXyP8/+u0vt/qW5FW/RP7Xwf4M1V9Fn2BKChEoEszFdNqluG8SqMJtNC/b793zgY93qkC/z+2Pf3wu9PX04jL3Dvbc9uK5tT+/ZfoxNFwe7c5I1xjSrDsSYAyborwa7y4ual6DVJNwsSc3wXNm7szJQPwLQT83V1Ra5kNHavIxWJDOieFzzjBFiZDP99Bjv9aTz6IOOJRoy4uFONRwtaM5vvwyDjVWJn3++amyfyEASkUxbU12uyL327TdXZA6J8+yT/jOqOoHB1IK2pWHoWFnpCoTVAn/4kqrwHPLrzXYa6MVbs73F9/kOm8EnRvgnMpieKUzQi36q/FykFVszFlh48+z3iQ+3cmYGCGyNimTIyR8dmukrtPF3xj0zOdGfSULhvuzCCHumT1Y0dMu69/IvniK0MTEloogVPXH2THT2gFPzwmCLfRfIK22NxJS/U2+wyMThiVkA8627Fd3jz4EYVXdlEVBkPFD7ABVLTZhCFCGhgGwmjpmK++v1U+X5ZPCPf3kmPPNe++9e70qHmIVHKdXoj0BtAsf/27y1e03vhhwZK5kwfo8zwplb4H9GLi5jQMBGLwH/967i2iPtto9Weq2BKtvcOgtm8iDz/3xH4Bp0tTeLdo74wAlXdYj6vbonLUyyvkrf5VKA3FGPhLWPHN4H5sGfvfpo9JFOKCuGcqvsFJlw6/IimiuOQhvFR/St8IODN9KXCFPE8BVmaXq2kpqlzcWKU500FQUpgSmJPLvay1i3IQIQj5orOMxvEmyNtdOgDgWHdsEcUNi3sEDN1bAI403ohOdDQPCQRvVlPE0DXZCPVUfFOmmbPrfSptgJ5NW/jx5wG8HDHtKjQC4pth7n/6DwtTm4yTw0UTSrm5PaE9nT5Feh7cp2L9dRIH5AF3dvXvnRLzGuI3O4LvfYBU/v9AkFqxydHz4nv7m3TP+6j659AdhRPrfVFFVkHJFCk9Wgas5R+eUBrvq1b+2NQchuP/Eyl1vPvA+Affh529/9OFH7zz9wfoeZZPTDInRatpGCBSs+SV2Df5RqElJd/Bi9fW93rFv6MUz0KKokgTy6YFqT98A9xvnnTf22xh39v/5AJP7eYSAgKz/lCx5P/q8S5mABK/tzJ+XxYuyl1jN833aRppq+hZ1qqyeoog3ApZSJppURdpd5vuuTb4FSV2lLPL5QmCFYo3Pk7SrGqpdNjEnJTpSiETofA8GLD1Ayn846Rl8XsZ075TPYp/q+Js09hJHDNE/3vDMR4gf5t1iWOgqxdT38jUAe4PxrItOV+1jFWyGjoLoocMBe/EoZipRauDwVC2uyTheQ4dcWRdaf+RPdrJtdQeJUzHt/4BaGSDLcBgpk/2huU1Y/SfMmARj+zXW9Yqg7BD42ooBIYSSlstbh5cNNco17NcrMb2MnbQwqtEE3T1gjSBAXit6SM8VUECER0VlHUwidzYJHqWzGxJRuRMs9uh5qXxbmQBouFEu5w4adfz9D9Of74oXsOBv+PbpkUM9gEo+35OFem5RkhvsEsIzapdfDr+ZlwLcCxwsLaunvwhTj4nwoHoE2rsg5PNsQGRrLwr9PI/Aeqmvkf4Rue1wbKDKfpzm8MFWGSg3zKBvTBd6YXolHFri16BljB0scW9OJsYSzp+SJDhmsW+aAWltkkl2LyvywqLVBNlaHwxqIonBy73na94nZUnPBwHYirN5Tf/94Z9lebjaURJddeY1yyTH4y6Xnc5Ks4F0t6J9Zx3n51OT27UPwPmM3iFGzXIczDQ/N9psd19qIq1pPC9essPOSWI6EUs9m4Gsm4SPZ5InzJ4bH+tnvxEnL9MTETrEQOs4COsOwuKdZr0Wld//u1Kq2NPLw6k1cca5wqT5DuuW36iyhhjzhK+s05mDVQnmRL5oFwBF/U5ouwnGTDMr1fVwW5WlyYZYEtNfcmsXNcc3sHFeQJwxbvZOr13SW8vc50lIGCaMPfB0e+boHTjRlEGBN3tP/fV0pdefON/zHDpOmdAV6zaJ78+nHo/6Zky4/6KYxH45oaf3QYCAyX26CCMu/uW3X04L6DqvSDqISoN8AX/3/Hgnu6TuwI+w8pOfmudfbufA9TN5S+Wf/se3+f//yTw2V6bl3DzTQ2MRujtLPEaJRuQgYz14N2ZTAg0cVfS15pjwW/slUxdVx2t64wm8H4ATzTNNjQVuZ0tJr7ybp/oCS6Z4X/tBjq1c3nhgHJ3pTI5OjLdoDIELc9AKXpsa6rV4QXH8ACLslq87UUVdX4UwIK2JzomLTrcbeHJ1336+aPCWenD5oeNudCwjNus06Um7Fi5eGdouhuUClVogDIKvLcYmufD8phcRoGcNQ6liHxoOKmCoiokaKu4JyNkbz0SlYdxd0JIg0lJ7yotcZzstx6lbtY8IGEJZP6FNT+d4BP3WG890Z+cWT5zh8c4gMsHL3Ml22SwoIZlvhnwfhci1HdcB0uHkzShaqLY2SWpvsQDzV1TjRHg+WBXmNlxE0pRgoreAE1qNT01P39KzjpcImbZAhK5doReFZw5qOM50FIRKFxSj16MB3Td7RcNeLQYGhBfA/9okEPvjE1ZnKkHJVOfe33nC0Lo/5s0r9wT52Q/n+qQVnQeuSRKQBY/XCz5oiDmzx88Ch3nj/OwOU5WPAZnPlVnEtuYw5rPiPfXjLOGqFeJ4nMEs5SmaMcWabo1ln9OkuRrp1lypjoJKCFVTlWNj94a548paoFTDDUIUYDBFkefaVmZnXo02oMS7Jp+wNcAoWqX3mtm7TBLWnSCVmChg4phkaIRKQ+ZXa0vDELg35oaSmuBZEyVwWSffPH/13uddfAtq1PHGEkOv7scEYxKogi6qrWz6tOjaFggkh38EQ0Lrkq/XLsqiN6ic5ASw4HDnjm2Z3YtsrqGXAWK+0ItgLANcz+1VxwYSLdbyrh/ivDk6IUi92zoiRUmxZymMIjBZe5ytqcTg3jgsyK5TbuKp0GajP6fD8LQSg1RIV0014EYb1OKxJk1c2gko8p2ET5qUhwxo7be33v3okx/++3//4PCkVGu0Wo16o6mt1ptdGSsKVommGbafGa2MLwZBx1pwHbpQveC149i7uyqSoGut2+6KYrfBVy8bV7VKpXx+/P9f7yPwjnN3XUp02/ZzKwXFY7Uada7b7FTTjKVtgWsp4vfaJ5eO8Lrck14geh3SYmyQxMp4bO389OzsvOYeF/cTI+GcmudZOCSPmKgZOfuKdcMWy2kUWsPTl8IenO3zKhVNmWWIiJ/al3awL/kEeScC5j7FLbwR3HdL3ZD8rxYkXIGIpBDDiMxw290VlcX1l3PHTpwAPE1IKmtdXvixTQYmBG/SGTes7tFq42dGVO/E0nHSGl53kOyoScLzvFnBC4Z5PuobyPqICwFgvVSZhGGNUQPQ4nKhBcJafY80HAphGLgMZZ5A1ZcCmAp/RswJO+qfaK/iJiMfe3HZHB0zIr4Pf70cTzwjf30nAH/98NfmBk8P84+rgmiQeMtcRbj659dk6hcTYgi561RDuhRjFAg+KxWJWxEEhf7zQZS9oKzo2xFF4OljkScHD4An4oMzv7lnxjPvQ0cYBQB4fqe27O3Qx7qn5mtNXZyXPgCQAQYAAAjA/5EoLuGDT+jGwoHubM/Qm8yeqfNXJIojn4yD0UNvCmePgmM/iobcznIIlXRoCpbTKmE8OqkF5cDzIUEiuzNyAju39ZVTytc7gX/TTrxPgmOfCPbgiN9I/zBGTq0P7bIgvuq2j80XT5x6ybNVdFzjttQhyrE/O6XgWM2XFCsvVo6CPb/p65V6vPUKnFDc/uvGstet3AUmO6X6eH0oZx5T1OUzCMjbhp3aujLyusKPzilFNuORFg5YrUfvXvjFr3kWS9hod/3NlxTgY0+LTltCSRZ6+6XoYSq+3DLXEo2yQJU9cM86K5a5wF7b5fyy+igX2OZne3vOM915i4WHur99RsqO7W/nnHNYJw8bvgaoAxyVBheUWLMVJ3TyLSvRr8LLfrcnXvu3B0zRFTd5HrNNbsD8dVF9ehOu/Gupxz/m5S89ukv7w1Vz+R+u/6nvGGvjVqap/fmHRonv1jKY+STj1q2y50n0JKwihF7qYEn3/id3GLFnb6NyYqfRSlxHys9gJCl6r+X4+MnZ2GvTpFSvN6fWe7thY3jp35fkwm8ZbZJFqtM0a8t4A+Do5DVN9HKExnG+eo4OeoajNNHLkfClsYekAKcUz/w0CZVq/vTGnAxtaMUZ1ONGfmn8EcAAWBtDAHkaANwr3CcIh0IEw5QZCHjnDkRcUwAJ+yRDRpSAzsQjrAEBMIRaiOkWIj015iEDlWkoYGWIRSDVAAMk0jEDd4EwwQu7OtmcNrps8fnkB9CId9BecbZItlesaPsE2C+ZnyR7RYtzyDbx4sTHGm42SX0hPGS/h7MgVviZyYzEp7y/9WiHezKiQSNHlhy7T5HkUFzzksIufKUiRUlWrjwTHCKzkrtH9kkSfcjJQ3af/dFiLcc/yJG/LudH/Kl8FXYjB21ykpnoES4J9ymvqiLnQ9zpuXAnnpDXjbPKzzrz/KB0Q3nj1BN8+iwWhveqiKWfS3vA/jI896sl3XLM5443EL+P94SvwwLeWzqy1Vko7BORok2PHRY2+33Z2m2lD72ujG9c8ZJ4Bkz7pXXG1U9EKwD4HgbDT8L8xz4NCjkTCLfcW0L7dXjkZyuIuHDV6bEuT6QX/v5VIkh0+0Wkp8645rrV/iSVa3i3Xr+K0mct2eNeb/+3noL9Bz4vRpyDKmziL95mY5QSxBETZQZ+VuV/kqU4wluHbQtRGuLdtkul8anPpM8/VumoG3aYoLVTkJN2CZYhU5ZjchPfGHfHJ3Ibl9ZSGbNEw/8vFnMICZERBe6Z9t4MS+Ys4egbRKrxVPnRCWaQsbIhVESDRTAIEcqHnEXocNMtfto80OhbTf7rinotvvf9xBRbhBjIDDGRObKI4iTNIMKEMi7yoqyk0qa2Tes63w+L5Wq9ORunCxcvXb5y9dr1GzcRDEhGpgj8mVnKcVWsLX2uP7aYNbgER6XwlqpqEOxRqThzMusNq/XKJ4aABuEq6MtAbjQPL59iJUqVYZSr4BcQVKlKtRohterMUy+s4evnO7QMD+n4gvoPWKZT13fU/3sAGByBRKExWByeQCSRKVQancFksTlcHl8gFIklUlkUyEa/aztejiqUKrVGq9MbjCazxWqzO1Y0iWCu1zrfr41MOPr+fMquJv1Gy6fH5UZ9Ntw62Ha0ERFDJVQQy/Gri++/DdEDh5nt7uoa93bTHoI6ZP2lfte3cANcxXY6paD+MDgY+Gg3Yt7BIREehrnaJWhRyxJgmH/CX4T9/rI/inR/Pw70NpHrSkI8Lyj+7lAvh6ud8pZJUrcE2ahj4m2bO49KtRPZzEIJWMBBHQgftgxwFSGuRzprqIOCRsOIFGXByUTqpMsM7TlBGNswLWUdIuhBB4dUMkVl7sea+RCQWNyUCz3uk3Jmvof1Nhz5Y3WgdkDX1PE2QaRQk4YNEjISCsZayMZJsZa619r3m0LrNKOz6pLZCMO4lgRoLUbv0MvqJD8EpUnsPO/oIOVSadc6Lm/JfJRhtBIAErAqpxjfij1Xi2vH07ltY08btuVaJiLGMKBGs1dCM9fdT+ia+DxJZCVbPCRTD6p8kdKGsKSesMlC2htxTbVZ3QtjX6DGpmKjKt01uecbiavPy/Xbm9vblALT8uGlVsvG9XkWWv48c2Gzmubba1F7cJLRx0fIKKXjvdhfOntO+uNpuOYXSnWmZf2RbTxK04ivgx8X1P0VgSoVodGAVvXAblRDqbkFFsX5Aem2OB3EGaAuMO30VyDr5BZtDEAMPitSXHMm1+ZHoXZszLdIDiBkCNwjtbkMgqbXc1VYTIFX7dZJFImB19tIH6Tq45Qpar6PhAnSpoND4jJaN0aGRwyyQBCHxgHbSpDNeEg4rDCKO4YXw+4GXkBIKJDfUwygYYqvO1W5QCdrWSLyApxTcfg9lPOH5d0gxtqv3caxM1CWUDURIsQIswFTIFQ5IJJWefFHbWnKzddHXzGWylPyW3RkAEBOTFAloHol6VgF0M0BYc1rqaTOAEIrA5X09VRujWJhB15hHaDqc2wnl4J+d9o3UBkHQPqIqghUaaASHsxf4GpYcm8kFD3aAeh+RLf8RhWnpwnEnziT9dWptsmOrfRw72l/rUMzDvJ+tV+eHRzdbH+V9wHAl8OlZ8zQDwzXhVJklkpZScZLLj377COprPjAt+SNtGPJL5jVJYNVWQkCwIOhxIERsyYKkRA7y1Ff/5YN6vhCePiSh4XbAQit8RP+CC3ymnehsy94TdNDxfH/K6f+x5Jf71f/T1pObgQAAA=="


if __name__ == "__main__":
    main()
