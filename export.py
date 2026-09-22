#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memorium — the memory of your Claude Code sessions, as a standalone static HTML site.

- Reads the JSONL files in ~/.claude/projects (override with MEMORIUM_PROJECTS_DIR).
- Writes index.html (dashboard, WebGL hero, full-text search) + sessions/*.js + searchindex.js.
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
        "sections": [s for s in sections if s["blocks"] or s["prompt"]],
    }

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
    --fs-1:11px; --fs-2:12px; --fs-3:13px; --fs-4:14px; --fs-5:15px; --fs-6:20px; --fs-7:30px; --fs-display:40px;
    --r-1:4px; --r-2:8px; --r-3:14px; --r-pill:999px;
    --nav-h:54px;
    --mono:ui-monospace,"Cascadia Code","JetBrains Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--mono);
    font-size:var(--fs-5);line-height:1.78;-webkit-font-smoothing:antialiased;font-feature-settings:"liga" 0;}
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
  .nav-brand{display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:700;font-size:var(--fs-5);letter-spacing:-.01em;}
  /* Memorium mark: rippling ribbon (curve + arrow + 2 alternating nodes), SVG */
  .brand-svg{display:inline-block;vertical-align:middle;}
  .nav-brand .brand-svg{width:34px;height:auto;}
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
  .dash-hero h1{font-size:var(--fs-display);margin:0 0 10px;letter-spacing:-.02em;font-weight:800;}
  .dash-hero .brand-svg{width:52px;height:auto;margin-right:14px;}
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
  .ed-head h1{font-size:var(--fs-7);margin:0 0 8px;font-weight:800;letter-spacing:-.01em;}
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
  .dochead h1{font-size:var(--fs-7);line-height:1.18;margin:0 0 12px;font-weight:700;letter-spacing:-.01em;}
  .dochead .sub{color:var(--muted);font-size:var(--fs-2);}
  .dochead .accent-rule{height:3px;width:60px;background:var(--accent);margin-top:18px;border-radius:var(--r-1);}
  button{font-family:inherit;}
  .man{max-width:760px;margin:0 auto;padding:34px 26px 100px;}
  .man h1{font-size:var(--fs-7);margin:0 0 10px;letter-spacing:-.01em;}
  .man .lede{color:var(--muted);font-size:var(--fs-3);line-height:1.7;margin:0 0 6px;}
  .man .accent-rule{height:3px;width:60px;background:var(--accent);margin:18px 0 34px;border-radius:var(--r-1);}
  .man h2{font-size:var(--fs-6);margin:38px 0 12px;padding-top:22px;border-top:1px solid var(--line);}
  .man h3{font-size:var(--fs-3);margin:22px 0 8px;color:var(--accent-d);text-transform:uppercase;letter-spacing:.08em;}
  .man p{font-size:var(--fs-3);line-height:1.75;margin:0 0 13px;}
  .man li{font-size:var(--fs-3);line-height:1.7;margin-bottom:7px;}
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
  .exchange-n{grid-column:1;text-align:right;font-size:var(--fs-7);font-weight:700;color:var(--accent);
    line-height:1;padding-top:2px;position:sticky;top:calc(var(--nav-h) + 18px);align-self:start;letter-spacing:-.02em;}
  .col-read{grid-column:2;min-width:0;}
  .gutter{grid-column:3;position:relative;min-width:0;}

  .prompt{background:var(--white);border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:var(--r-2);padding:13px 17px;margin:0 0 26px;box-shadow:0 1px 2px rgba(0,0,0,.03);}
  .prompt-tag{font-size:var(--fs-1);letter-spacing:.16em;text-transform:uppercase;color:var(--accent-d);font-weight:700;margin-bottom:5px;}
  .prompt-body{font-size:var(--fs-4);line-height:1.75;}
  .prompt-body p{margin:.35em 0;}

  .prose p{margin:.95em 0;}
  .prose h3{font-size:var(--fs-6);margin:1.5em 0 .5em;font-weight:700;color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:4px;}
  .prose h4,.prose h5,.prose h6{font-size:var(--fs-4);margin:1.3em 0 .4em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-d);}
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
  pre.code code{background:none;padding:0;border:none;font-size:var(--fs-3);line-height:1.55;color:var(--term-ink);}

  .tool{display:inline-flex;align-items:center;gap:7px;font-size:var(--fs-2);color:var(--muted);background:var(--white);border:1px solid var(--line);border-radius:var(--r-2);padding:3px 11px;margin:4px 5px 4px 0;}
  .tool-icon{color:var(--accent2);}

  .bash{margin:16px 0;border-radius:var(--r-2);overflow:hidden;border:1px solid var(--term-edge);box-shadow:0 2px 12px rgba(0,0,0,.13);font-size:var(--fs-3);}
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
  details.agent .prose{padding:0 16px 12px;font-size:var(--fs-4);}
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
  #recap header h2{font-size:var(--fs-3);letter-spacing:.12em;text-transform:uppercase;margin:0;color:var(--accent-d);}
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
  body.welcome #topnav{display:none;}

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

  /* ═══════ Welcome / WebGL memory-ribbon hero ═══════ */
  #view-welcome{padding-top:0;}
  #view-welcome.active{display:flex;align-items:center;justify-content:center;min-height:100vh;}
  /* welcome background: light grid + depth vignette */
  body.welcome::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background:
      radial-gradient(130% 100% at 50% 40%,transparent 0 48%,rgba(86,99,64,.13) 100%),
      repeating-linear-gradient(0deg,rgba(86,99,64,.05) 0 1px,transparent 1px 38px),
      repeating-linear-gradient(90deg,rgba(86,99,64,.05) 0 1px,transparent 1px 38px);}
  .welcome-inner{position:relative;z-index:1;text-align:center;width:100%;padding:20px;}
  /* wide 3D ribbon on top, title typed underneath */
  .hero-stage{position:relative;width:min(1460px,98vw);height:300px;margin:0 auto -6px;}
  #gl{position:absolute;inset:0;width:100%;height:100%;display:block;}
  #glfail{display:none;color:var(--muted);font-size:var(--fs-3);padding:40px;}
  /* signature move: a search bar types a query, a node answers */
  .searchbar{position:absolute;left:50%;top:2%;transform:translateX(-50%);z-index:2;
    display:flex;align-items:center;gap:10px;min-width:240px;
    background:rgba(246,237,220,.7);backdrop-filter:blur(9px);
    border:1px solid var(--line);border-radius:var(--r-3);padding:10px 16px;
    font:var(--fs-4) var(--mono);color:var(--ink);box-shadow:0 10px 28px rgba(86,99,64,.14);
    opacity:0;transition:opacity .5s;pointer-events:none;}
  .searchbar .mag{color:var(--accent);font-size:var(--fs-5);line-height:1;}
  .searchbar .q{white-space:nowrap;}
  .searchbar .scaret{display:inline-block;width:.5ch;height:1.05em;background:var(--accent);
    vertical-align:-.15em;animation:caret 1s step-end infinite;}
  /* commit labels projected onto the nodes: the ribbon becomes a living git log */
  .nlabel{position:absolute;left:0;top:0;z-index:2;font:var(--fs-1)/1.3 var(--mono);
    color:var(--ink);white-space:nowrap;pointer-events:none;opacity:0;
    transform:translate(-50%,-50%);
    /* paper-coloured halo (map casing): readable even on top of the ribbon */
    text-shadow:0 0 3px var(--paper),0 0 3px var(--paper),0 0 5px var(--paper),0 0 8px var(--paper);}
  .nlabel .h{color:var(--accent-d);opacity:.85;letter-spacing:.02em;}
  .nlabel.hit{font-weight:700;
    text-shadow:0 0 3px var(--paper),0 0 3px var(--paper),0 0 5px var(--paper),0 0 9px rgba(142,156,99,.85);}
  .nlabel.hit .h{color:var(--accent);opacity:1;}
  #wTitle{font-size:clamp(44px,7vw,88px);font-weight:800;letter-spacing:-.04em;margin:0;color:var(--olive-dd);
    white-space:nowrap;min-height:1.1em;line-height:1;}
  #wTitle:after{content:"";display:inline-block;width:.055em;height:.9em;margin-left:.05em;vertical-align:-.1em;
    background:var(--accent);animation:caret 1s step-end infinite;}
  #wTitle.typed:after{animation:none;opacity:0;}
  @keyframes caret{50%{opacity:0}}
  .welcome-inner .wsub{color:var(--muted);font-size:var(--fs-5);
    opacity:0;animation:upFade .8s 3.1s forwards;}
  .enter-btn{margin-top:28px;font-size:var(--fs-4);padding:13px 26px;border-radius:var(--r-3);letter-spacing:.01em;
    box-shadow:0 6px 20px rgba(142,156,99,.36);transition:transform .14s,box-shadow .14s,background .14s;
    opacity:0;animation:upFade .8s 3.25s forwards;}
  .enter-btn:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(110,122,75,.44);}
  .enter-btn:active{transform:translateY(0);}
  .welcome-inner .wnote{margin-top:22px;font-size:var(--fs-2);letter-spacing:.14em;text-transform:uppercase;
    color:var(--muted);opacity:0;animation:upFade .8s 3.4s forwards;}
  @keyframes upFade{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){
    #wTitle:after{animation:none;opacity:0;}
    .welcome-inner .wsub,.enter-btn,.welcome-inner .wnote{animation:none!important;opacity:1;transform:none;}
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
  .prose{font-size:var(--fs-4);}
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
  <div class="welcome-inner">
    <div class="hero-stage">
      <canvas id="gl"></canvas><div id="glfail">WebGL indisponible sur ce navigateur.</div>
      <div class="searchbar" id="sbar"><span class="mag">⌕</span><span class="q" id="sq"></span><span class="scaret"></span></div>
    </div>
    <h1 id="wTitle"></h1>
    <p class="wsub">The living memory of your Claude&nbsp;Code sessions &mdash; reread, search, annotate.</p>
    <button id="enterBtn" class="enter-btn">Open the journal →</button>
    <div class="wnote">__COUNT__ conversations indexed</div>
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
  document.body.classList.toggle("welcome",v==="welcome");
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

/* ═════════ Welcome hero: WebGL memory ribbon (zero dependency) ═════════
   Gating perf : la boucle rAF ne tourne que quand la vue welcome est visible,
   driven by setView through window.__heroSetActive. */
(function(){
const canvas=document.getElementById("gl");
const titleEl=document.getElementById("wTitle");
const WORD="Memorium";
const RM=matchMedia("(prefers-reduced-motion: reduce)").matches;
const gl=canvas.getContext("webgl",{antialias:true,alpha:true,depth:true,premultipliedAlpha:false})
       ||canvas.getContext("experimental-webgl",{antialias:true,alpha:true,depth:true});
if(!gl){canvas.style.display="none";document.getElementById("glfail").style.display="block";
  titleEl.textContent=WORD;titleEl.classList.add("typed");
  window.__heroSetActive=function(){};return;}

/* ── tiny mat4 algebra (column-major, gl-matrix style) ── */
function perspective(fovy,aspect,near,far){
  const f=1/Math.tan(fovy/2), nf=1/(near-far);
  return new Float32Array([f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0]);
}
function lookAt(eye,ctr,up){
  let z0=eye[0]-ctr[0],z1=eye[1]-ctr[1],z2=eye[2]-ctr[2];
  let rl=1/Math.hypot(z0,z1,z2); z0*=rl;z1*=rl;z2*=rl;
  let x0=up[1]*z2-up[2]*z1, x1=up[2]*z0-up[0]*z2, x2=up[0]*z1-up[1]*z0;
  rl=1/Math.hypot(x0,x1,x2)||0; x0*=rl;x1*=rl;x2*=rl;
  let y0=z1*x2-z2*x1, y1=z2*x0-z0*x2, y2=z0*x1-z1*x0;
  return new Float32Array([x0,y0,z0,0, x1,y1,z1,0, x2,y2,z2,0,
    -(x0*eye[0]+x1*eye[1]+x2*eye[2]), -(y0*eye[0]+y1*eye[1]+y2*eye[2]), -(z0*eye[0]+z1*eye[1]+z2*eye[2]), 1]);
}
function mul(a,b){
  const o=new Float32Array(16);
  for(let c=0;c<4;c++)for(let r=0;r<4;r++){
    o[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3];
  }
  return o;
}

/* ── shaders ── */
const VS=`
attribute vec3 aPos; attribute vec3 aNor; attribute float aS; attribute float aV;
uniform mat4 uMVP;
varying vec3 vN; varying vec3 vW; varying float vS; varying float vV;
void main(){ gl_Position=uMVP*vec4(aPos,1.0); vN=aNor; vW=aPos; vS=aS; vV=aV; }`;
const FS=`
precision highp float;
varying vec3 vN; varying vec3 vW; varying float vS; varying float vV;
uniform vec3 uCam; uniform float uScan;
const vec3 GREEN=vec3(.557,.612,.388);
const vec3 BROWN=vec3(.412,.376,.306);
const vec3 INK=vec3(.337,.388,.251);
const vec3 CREAM=vec3(.965,.929,.863);
void main(){
  vec3 N=normalize(vN); if(!gl_FrontFacing) N=-N;
  vec3 L=normalize(vec3(.35,.85,.55));
  vec3 V=normalize(uCam-vW);
  vec3 H=normalize(L+V);
  float diff=max(dot(N,L),0.0);
  float spec=pow(max(dot(N,H),0.0),26.0);
  float rim=pow(1.0-max(dot(N,V),0.0),2.6);
  vec3 base = gl_FrontFacing ? mix(BROWN,GREEN,0.7) : mix(INK,BROWN,0.55);
  vec3 col = base*(0.32+0.74*diff) + spec*CREAM*0.55 + rim*GREEN*0.30;
  if(uScan>=0.0){
    float scan=smoothstep(0.05,0.0,abs(vS-uScan));
    col += scan*mix(GREEN,CREAM,0.55)*0.65*(1.0-abs(vV)*0.5);
  }
  gl_FragColor=vec4(col,1.0);
}`;
const VS_TK=`
attribute vec3 aPos; attribute float aB;
uniform mat4 uMVP; varying float vB;
void main(){ gl_Position=uMVP*vec4(aPos,1.0); vB=aB; }`;
const FS_TK=`
precision highp float; varying float vB;
const vec3 GREEN=vec3(.557,.612,.388);
const vec3 DK=vec3(.337,.388,.251);
const vec3 CREAM=vec3(.973,.957,.902);
void main(){
  vec3 col=mix(mix(DK,GREEN,0.6), CREAM, vB);
  gl_FragColor=vec4(col,1.0);
}`;
const VS_PG=`
attribute vec3 aPos; attribute float aA;
uniform mat4 uMVP; varying float vA;
void main(){ gl_Position=uMVP*vec4(aPos,1.0); vA=aA; }`;
const FS_PG=`
precision highp float; varying float vA;
const vec3 CREAM=vec3(.973,.957,.902);
const vec3 GREEN=vec3(.557,.612,.388);
void main(){ gl_FragColor=vec4(mix(GREEN,CREAM,0.55), vA); }`;
function sh(type,src){const o=gl.createShader(type);gl.shaderSource(o,src);gl.compileShader(o);
  if(!gl.getShaderParameter(o,gl.COMPILE_STATUS))console.error(gl.getShaderInfoLog(o));return o;}
function makeProg(vs,fs,binds){
  const p=gl.createProgram();
  gl.attachShader(p,sh(gl.VERTEX_SHADER,vs));
  gl.attachShader(p,sh(gl.FRAGMENT_SHADER,fs));
  binds.forEach(function(n,i){gl.bindAttribLocation(p,i,n);});
  gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS))console.error(gl.getProgramInfoLog(p));
  return p;
}
const prog=makeProg(VS,FS,["aPos","aNor","aS","aV"]);
const loc={uMVP:gl.getUniformLocation(prog,"uMVP"),uCam:gl.getUniformLocation(prog,"uCam"),uScan:gl.getUniformLocation(prog,"uScan")};
const ptProg=makeProg(VS_TK,FS_TK,["aPos","aB"]);
const ploc={uMVP:gl.getUniformLocation(ptProg,"uMVP")};
const pgProg=makeProg(VS_PG,FS_PG,["aPos","aA"]);
const pgloc={uMVP:gl.getUniformLocation(pgProg,"uMVP")};

/* ── ribbon geometry (rebuilt each frame for the rotation) ── */
const NSEG=300, HW=0.60, SPAN=26.0, TWIST=1.4;
const bPos=gl.createBuffer(), bNor=gl.createBuffer(), bS=gl.createBuffer(), bV=gl.createBuffer();
const posA=new Float32Array((NSEG+1)*6), norA=new Float32Array((NSEG+1)*6),
      sA=new Float32Array((NSEG+1)*2), vA=new Float32Array((NSEG+1)*2);
// positions + sizes + face (±1): nodes on BOTH sides of the ribbon, spaced out for the labels
const NODES=[0.06,0.21,0.37,0.55,0.72,0.88];
const NSIZE=[0.95, 1.15, 0.8, 1.2, 0.85, 1.05];
const NFACE=[1,  -1,   1,   -1,  -1,   1];
const ROT=0.25;                                            // rotation de la bande sur son axe (rad/s)
const STEM_LEN=0.52, STEM_W=0.018, NODE_R=0.155, NSEGC=18;
const VPM=6 + NSEGC*3;                                     // sommets par marqueur (tige + perle)
const bTk=gl.createBuffer();
const PSEG=44, bPg=gl.createBuffer();
const pgA=new Float32Array(PSEG*6*4);                      // anneau ping : (xyz + alpha) par sommet
const tkA=new Float32Array(NODES.length*VPM*4);            // (xyz + brightness) par sommet

function curve(s,out){
  // FROZEN shape (static): nodes stay put, the ripple causes no churn
  const env=Math.sin(Math.PI*Math.min(1,Math.max(0,s)));
  out[0]=(s-0.5)*SPAN;
  out[1]=env*(1.02*Math.sin(s*4.2+0.6) + 0.30*Math.sin(s*9.0+0.9));
  out[2]=env*(0.40*Math.sin(s*3.0+1.1));
}
const _a=[0,0,0],_b=[0,0,0];
// local frame (spine point + width W + normal N) — spine is frozen, W/N rotate over time
const _fp=[0,0,0],_fW=[0,0,0],_fN=[0,0,0];
function computeFrame(s,t){
  curve(s,_fp);
  curve(Math.max(0,s-1/NSEG),_a);
  curve(Math.min(1,s+1/NSEG),_b);
  let Tx=_b[0]-_a[0],Ty=_b[1]-_a[1],Tz=_b[2]-_a[2];
  let tl=1/(Math.hypot(Tx,Ty,Tz)||1); Tx*=tl;Ty*=tl;Tz*=tl;
  let ux=0,uy=1,uz=0;
  if(Math.abs(Tx*ux+Ty*uy+Tz*uz)>0.9){ux=1;uy=0;uz=0;}
  let sx=Ty*uz-Tz*uy, sy=Tz*ux-Tx*uz, sz=Tx*uy-Ty*ux;
  let sl=1/(Math.hypot(sx,sy,sz)||1); sx*=sl;sy*=sl;sz*=sl;
  let nx=sy*Tz-sz*Ty, ny=sz*Tx-sx*Tz, nz=sx*Ty-sy*Tx;
  const th=TWIST*s + t*ROT, c=Math.cos(th), sn=Math.sin(th);
  _fW[0]=sx*c+nx*sn; _fW[1]=sy*c+ny*sn; _fW[2]=sz*c+nz*sn;
  _fN[0]=-sx*sn+nx*c; _fN[1]=-sy*sn+ny*c; _fN[2]=-sz*sn+nz*c;
}
function buildRibbon(t){
  for(let i=0;i<=NSEG;i++){
    const s=i/NSEG;
    computeFrame(s,t);
    const Wx=_fW[0],Wy=_fW[1],Wz=_fW[2], Nx=_fN[0],Ny=_fN[1],Nz=_fN[2];
    const k=i*6;
    posA[k]=_fp[0]-Wx*HW; posA[k+1]=_fp[1]-Wy*HW; posA[k+2]=_fp[2]-Wz*HW;
    posA[k+3]=_fp[0]+Wx*HW; posA[k+4]=_fp[1]+Wy*HW; posA[k+5]=_fp[2]+Wz*HW;
    norA[k]=Nx;norA[k+1]=Ny;norA[k+2]=Nz; norA[k+3]=Nx;norA[k+4]=Ny;norA[k+5]=Nz;
    sA[i*2]=s; sA[i*2+1]=s; vA[i*2]=-1; vA[i*2+1]=1;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,bPos);gl.bufferData(gl.ARRAY_BUFFER,posA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,3,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ARRAY_BUFFER,bNor);gl.bufferData(gl.ARRAY_BUFFER,norA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,3,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ARRAY_BUFFER,bS);gl.bufferData(gl.ARRAY_BUFFER,sA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(2);gl.vertexAttribPointer(2,1,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ARRAY_BUFFER,bV);gl.bufferData(gl.ARRAY_BUFFER,vA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(3);gl.vertexAttribPointer(3,1,gl.FLOAT,false,0,0);
}

function pushVert(k,x,y,z,b){ tkA[k]=x;tkA[k+1]=y;tkA[k+2]=z;tkA[k+3]=b;return k+4; }
function buildTicks(progress,activeNode,intensity,t){
  let k=0;
  for(let i=0;i<NODES.length;i++){
    const s=NODES[i];
    const reveal=Math.min(1,Math.max(0,(progress-s)/0.045));   // germe quand le front passe, PUIS persiste
    if(reveal<=0){ for(let z=0;z<VPM;z++) k=pushVert(k,0,0,0,0); continue; }
    const sc=easeOutCubic(reveal);
    computeFrame(s,t);
    const f=NFACE[i];
    const px=_fp[0], py=_fp[1], pz=_fp[2];
    const nx=_fN[0]*f, ny=_fN[1]*f, nz=_fN[2]*f;
    const len=STEM_LEN*sc;
    const tx=px+nx*len, ty=py+ny*len, tz=pz+nz*len;
    const wx=_fW[0]*STEM_W, wy=_fW[1]*STEM_W, wz=_fW[2]*STEM_W;
    const lock=(i===activeNode)?intensity:0, rim=0.12+0.55*lock;
    k=pushVert(k, px-wx,py-wy,pz-wz, .12); k=pushVert(k, px+wx,py+wy,pz+wz, .12); k=pushVert(k, tx+wx,ty+wy,tz+wz, .5);
    k=pushVert(k, px-wx,py-wy,pz-wz, .12); k=pushVert(k, tx+wx,ty+wy,tz+wz, .5); k=pushVert(k, tx-wx,ty-wy,tz-wz, .5);
    const R=NODE_R*NSIZE[i]*sc*(1+0.45*lock);
    for(let j=0;j<NSEGC;j++){
      const a0=(j/NSEGC)*6.283185, a1=((j+1)/NSEGC)*6.283185;
      k=pushVert(k, tx,ty,tz, 1.0);
      k=pushVert(k, tx+Math.cos(a0)*R, ty+Math.sin(a0)*R, tz, rim);
      k=pushVert(k, tx+Math.cos(a1)*R, ty+Math.sin(a1)*R, tz, rim);
    }
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,bTk);gl.bufferData(gl.ARRAY_BUFFER,tkA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,3,gl.FLOAT,false,16,0);
  gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,1,gl.FLOAT,false,16,12);
}
function pushPg(k,x,y,z,a){ pgA[k]=x;pgA[k+1]=y;pgA[k+2]=z;pgA[k+3]=a;return k+4; }
// ping ring bursting out of the matched node (pingT 0→1 = young→faded)
function buildPing(active,pingT,intensity,t){
  computeFrame(NODES[active],t);
  const f=NFACE[active], len=STEM_LEN;
  const cx=_fp[0]+_fN[0]*f*len, cy=_fp[1]+_fN[1]*f*len, cz=_fp[2]+_fN[2]*f*len;
  const baseR=NODE_R*NSIZE[active];
  const R=baseR + easeOutCubic(pingT)*0.62;
  const hw=0.02+pingT*0.02;
  const a=Math.pow(1-pingT,1.7)*intensity*0.95;
  let k=0;
  for(let j=0;j<PSEG;j++){
    const a0=j/PSEG*6.283185, a1=(j+1)/PSEG*6.283185;
    const c0=Math.cos(a0),s0=Math.sin(a0),c1=Math.cos(a1),s1=Math.sin(a1);
    k=pushPg(k, cx+c0*(R-hw),cy+s0*(R-hw),cz, a); k=pushPg(k, cx+c0*(R+hw),cy+s0*(R+hw),cz, a); k=pushPg(k, cx+c1*(R+hw),cy+s1*(R+hw),cz, a);
    k=pushPg(k, cx+c0*(R-hw),cy+s0*(R-hw),cz, a); k=pushPg(k, cx+c1*(R+hw),cy+s1*(R+hw),cz, a); k=pushPg(k, cx+c1*(R-hw),cy+s1*(R-hw),cz, a);
  }
  gl.bindBuffer(gl.ARRAY_BUFFER,bPg);gl.bufferData(gl.ARRAY_BUFFER,pgA,gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,3,gl.FLOAT,false,16,0);
  gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,1,gl.FLOAT,false,16,12);
}

let projM=null;
function resize(){
  const dpr=Math.min(2,window.devicePixelRatio||1);
  const w=canvas.clientWidth,h=canvas.clientHeight;
  if(!w||!h)return;
  canvas.width=w*dpr;canvas.height=h*dpr;
  gl.viewport(0,0,canvas.width,canvas.height);
  projM=perspective(45*Math.PI/180, w/h, 0.1, 100);
}

const CAM=[0,0.10,9.8];
const DRAW_DUR=2.4;                                   // left-to-right draw (slow enough to watch the nodes appear one by one)
function easeOutCubic(x){return 1-Math.pow(1-x,3);}

function render(elapsed,sig){
  if(!projM)return;
  const progress=Math.min(1,elapsed/DRAW_DUR);
  const active = sig?sig.activeNode:-1, inten = sig?sig.intensity:0;
  gl.clearColor(0,0,0,0);
  gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  const view=lookAt(CAM,[0,0,0],[0,1,0]);
  const mvp=mul(projM,view);
  curMVP=mvp;                                         // shared with the label projection

  // pass 1: ribbon, revealed up to the draw front
  gl.useProgram(prog);
  gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.disable(gl.CULL_FACE);
  buildRibbon(elapsed);
  gl.uniformMatrix4fv(loc.uMVP,false,mvp);
  gl.uniform3fv(loc.uCam,CAM);
  gl.uniform1f(loc.uScan, active>=0 ? NODES[active] : -1);
  const pairs=Math.max(1,Math.floor(NSEG*progress));
  gl.drawArrays(gl.TRIANGLE_STRIP,0,(pairs+1)*2);

  // pass 2: commit nodes on both faces (sprout one by one, orbit, persist)
  gl.useProgram(ptProg);
  gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  buildTicks(progress,active,inten,elapsed);
  gl.uniformMatrix4fv(ploc.uMVP,false,mvp);
  gl.drawArrays(gl.TRIANGLES,0,NODES.length*VPM);

  // pass 3: radar ping on the matched node (transparent, does not write depth)
  const pingT = sig?sig.pingT:-1;
  if(active>=0 && pingT>=0 && inten>0){
    gl.useProgram(pgProg);
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false);
    buildPing(active,pingT,inten,elapsed);
    gl.uniformMatrix4fv(pgloc.uMVP,false,mvp);
    gl.drawArrays(gl.TRIANGLES,0,PSEG*6);
    gl.depthMask(true); gl.disable(gl.BLEND);
  }
}

/* signature: typed queries, each one lighting up its own node */
const QUERIES=[{q:"auth bug",node:3},{q:"docker port",node:1},{q:"playwright test",node:5}];
const SIG_START=DRAW_DUR+1.6, CYCLE=4.0;
function computeSig(elapsed){
  if(elapsed<SIG_START) return {visible:false,text:"",activeNode:-1,intensity:0,pingT:-1};
  const e=elapsed-SIG_START, qi=Math.floor(e/CYCLE)%QUERIES.length, ct=e%CYCLE, Q=QUERIES[qi];
  let text="",activeNode=-1,intensity=0,pingT=-1;
  if(ct<1.0){ text=Q.q.slice(0,Math.floor((ct/1.0)*Q.q.length)); }
  else if(ct<3.1){ text=Q.q;
    if(ct>1.25){ activeNode=Q.node;
      intensity=Math.min(1,(ct-1.25)/0.25);
      if(ct>2.7) intensity*=Math.max(0,1-(ct-2.7)/0.4);
      pingT=((ct-1.25)%0.9)/0.9; } }                                     // repeating radar pings
  else { const er=Math.max(0,1-(ct-3.1)/0.5); text=Q.q.slice(0,Math.ceil(Q.q.length*er)); }
  return {visible:true,text,activeNode,intensity,pingT};
}
const sbar=document.getElementById("sbar"), sq=document.getElementById("sq");
function updateSearchBar(sig){ sbar.style.opacity=sig.visible?"1":"0"; sq.textContent=sig.text; }

/* commit labels: hash stamped when the node sprouts, message typed letter by letter.
   The search bar queries match these messages → the scene tells a single story. */
const LABELS=[
  {h:"3e1f0aa",m:"feat: session export"},
  {h:"9b01e44",m:"fix: docker port map"},
  {h:"f24d80c",m:"feat: dark mode"},
  {h:"a3f2c1d",m:"fix: auth token bug"},
  {h:"c98d517",m:"perf: lazy render"},
  {h:"7d40b2e",m:"test: playwright e2e"}
];
const stage=document.querySelector(".hero-stage");
const labelEls=LABELS.map(function(){
  const d=document.createElement("div"); d.className="nlabel";
  d.innerHTML='<span class="h"></span> <span class="m"></span>';
  stage.appendChild(d);
  return {root:d,h:d.firstChild,m:d.lastChild};
});
const TYPE_CPS=26;                                   // vitesse de frappe des messages de commit
let curMVP=null;
const _pt=[0,0],_pb=[0,0];
function project(x,y,z,out){
  const m=curMVP;
  const w=m[3]*x+m[7]*y+m[11]*z+m[15];
  out[0]=((m[0]*x+m[4]*y+m[8]*z+m[12])/w*0.5+0.5)*canvas.clientWidth;
  out[1]=(0.5-(m[1]*x+m[5]*y+m[9]*z+m[13])/w*0.5)*canvas.clientHeight;
}
function updateLabels(elapsed,sig){
  if(!curMVP)return;
  const progress=Math.min(1,elapsed/DRAW_DUR);
  const pxu=canvas.clientWidth/SPAN;                 // ≈ pixels per world unit
  for(let i=0;i<LABELS.length;i++){
    const L=labelEls[i], s=NODES[i];
    const reveal=Math.min(1,Math.max(0,(progress-s)/0.045));
    if(reveal<=0){ L.root.style.opacity="0"; continue; }
    computeFrame(s,elapsed);
    const f=NFACE[i];
    const tx=_fp[0]+_fN[0]*f*STEM_LEN, ty=_fp[1]+_fN[1]*f*STEM_LEN, tz=_fp[2]+_fN[2]*f*STEM_LEN;
    project(_fp[0],_fp[1],_fp[2],_pb); project(tx,ty,tz,_pt);
    let dx=_pt[0]-_pb[0], dy=_pt[1]-_pb[1];
    const dl=Math.hypot(dx,dy)||1; dx/=dl; dy/=dl;
    const off=14+NODE_R*NSIZE[i]*pxu;                // placed past the bead, along the stem axis
    L.root.style.left=Math.round(_pt[0]+dx*off)+"px";
    L.root.style.top =Math.round(_pt[1]+dy*off)+"px";
    L.h.textContent=LABELS[i].h;
    const chars=RM?99:Math.max(0,Math.floor((elapsed-s*DRAW_DUR)*TYPE_CPS));
    L.m.textContent=LABELS[i].m.slice(0,chars);
    // front face = readable, back face = muted (follows the ribbon rotation)
    const facing=Math.max(0,Math.min(1,_fN[2]*f*1.6+0.55));
    const hit=sig&&sig.activeNode===i&&sig.intensity>0.3;
    L.root.style.opacity=((hit?1:0.30+0.55*facing)*reveal).toFixed(3);
    L.root.classList.toggle("hit",hit);
  }
}

/* title typed letter by letter, once the ribbon is drawn */
function typeTitle(){
  let i=0;
  (function step(){
    titleEl.textContent=WORD.slice(0,i);
    if(i<WORD.length){ i++; typeTimer=setTimeout(step,95); }
    else titleEl.classList.add("typed");
  })();
}

/* ── lifecycle: start/stop driven by the view visibility ── */
let running=false, rafId=0, t0=0, typeTimer=null;
function loop(now){
  if(!running)return;
  const el=(now-t0)/1000, sig=computeSig(el);
  updateSearchBar(sig); render(el,sig); updateLabels(el,sig);
  rafId=requestAnimationFrame(loop);
}
function start(){
  titleEl.textContent=""; titleEl.classList.remove("typed");
  clearTimeout(typeTimer); typeTimer=setTimeout(typeTitle,(DRAW_DUR+0.25)*1000);
  t0=performance.now(); rafId=requestAnimationFrame(loop);
}
function staticFrame(){                              // reduced-motion: final frame, frozen
  const sig={visible:true,text:"auth bug",activeNode:3,intensity:1,pingT:-1};
  render(DRAW_DUR,sig); updateSearchBar(sig); updateLabels(DRAW_DUR,sig);
  titleEl.textContent=WORD; titleEl.classList.add("typed");
}
window.addEventListener("resize",function(){
  if(!document.body.classList.contains("welcome"))return;
  resize(); if(RM)staticFrame();
});
window.__heroSetActive=function(on){
  if(on){ resize(); if(RM)staticFrame(); else if(!running){running=true;start();} }
  else if(running){ running=false; cancelAnimationFrame(rafId); clearTimeout(typeTimer); }
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
        mtime = os.path.getmtime(path)
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


if __name__ == "__main__":
    main()
