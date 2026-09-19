#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memorium — la mémoire de tes sessions Claude Code, en site HTML statique autonome.

- Lit les JSONL de ~/.claude/projects (surchargeable via MEMORIUM_PROJECTS_DIR).
- Génère index.html (dashboard, hero WebGL, recherche plein-texte) + sessions/*.js + searchindex.js.
- Surlignage, annotations, carnet éditable ; organisation logique des dossiers/sessions
  via data/metadata.json — la source JSONL n'est JAMAIS modifiée.
- Zéro dépendance : stdlib uniquement. Sortie offline-first, aucun appel réseau.

Usage :
    memorium                # exporte ~/.claude/projects → ./export, ouvre le navigateur
    memorium <dossier>      # dossier de sortie personnalisé
    memorium serve          # sert ./export sur http://localhost:8137 (débloque l'écriture)
    python export.py        # équivalent sans installation
"""

import sys, os, json, re, html, glob, webbrowser, datetime

# Source des sessions : ~/.claude/projects par défaut, surchargeable (démo, tests, CI)
PROJECTS_DIR = os.environ.get("MEMORIUM_PROJECTS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "projects")

# ─────────────────────────── Découverte des sessions ───────────────────────────

def scan_meta(path):
    """Extrait (titre, nb prompts) d'un transcript via préfiltre sous-chaîne."""
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
    """Retire espaces + BOM/zero-width (artefacts de pipe) en début/fin."""
    return re.sub(r'^[\s﻿​]+|[\s﻿​]+$', '', s)


def discover_sessions(deep=50):
    """Liste les sessions triées par date ; deep-scan (titre + nb prompts) des `deep` plus récentes."""
    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    paths.sort(key=os.path.getmtime, reverse=True)  # tri cheap d'abord
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
            "title": title or "(sans titre)",
            "prompts": prompts,
            "mtime": os.path.getmtime(path),
        })
    return sessions


def pretty_project(encoded):
    """C--Users-Rayane-Desktop-...-portfolio -> portfolio (dernier segment)."""
    parts = encoded.replace("C--", "").split("-")
    parts = [p for p in parts if p]
    return parts[-1] if parts else encoded


def choose_session(sessions):
    if not sessions:
        print("Aucune session trouvée dans", PROJECTS_DIR)
        sys.exit(1)
    print("\n  Conversations disponibles :\n")
    show = sessions[:40]
    for i, s in enumerate(show, 1):
        d = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%d %b %H:%M")
        p = f"{s['prompts']:>3}p" if s["prompts"] is not None else "  ·"
        print(f"  [{i:>2}] {s['project']:<14} {d:<13} {p}  {s['title'][:50]}")
    print()
    while True:
        raw = clean_input(input("  Numéro à exporter (q pour quitter) : "))
        if raw.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        if raw.isdigit() and 1 <= int(raw) <= len(show):
            return show[int(raw) - 1]
        print("  Choix invalide.")


def resolve_arg(arg):
    """Argument = chemin .jsonl OU id de session (cherché dans les projets)."""
    if os.path.isfile(arg):
        return arg
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{arg}*.jsonl"))
    if matches:
        return matches[0]
    print(f"Session introuvable : {arg}")
    sys.exit(1)

# ─────────────────────────── Parsing du transcript ───────────────────────────

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
    # Invocation de commande : le prompt n'est que des tags, la commande tapée est le seul contenu réel.
    m = CMD_NAME.search(text)
    if m:
        cmd = m.group(1).strip()
        a = CMD_ARGS.search(text)
        args = a.group(1).strip() if a else ""
        if cmd:
            return (cmd + " " + args).strip()
    return TAG_STRIP.sub("", text).strip()


def prompt_title(cleaned):
    """Titre de section = 1re ligne signifiante du prompt, nettoyée et tronquée."""
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
    """Extrait le texte d'un tool_result (content str ou liste de blocs text)."""
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

    # titre de page = ai-title (stable sur toute la session ; ne titre PAS les sections).
    page_title = None
    for o in raw:
        if o.get("type") == "ai-title" and o.get("aiTitle"):
            page_title = o["aiTitle"]

    # map tool_use_id -> texte de résultat
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
            # les user-list = tool_result, déjà consommés via results
        elif t == "assistant":
            if cur is None:
                # réponse avant tout prompt (rare) — section d'amorce
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
                # thinking : volontairement exclu
                elif bt == "tool_use":
                    cur["blocks"].append(render_tool(b, results))

    return {
        "title": page_title or "Conversation Claude Code",
        "sections": [s for s in sections if s["blocks"] or s["prompt"]],
    }

# ─────────────────────────── Formatage des outils ───────────────────────────

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

    # Édition de code : EN-TÊTE SEUL, jamais le contenu
    if name in ("Edit", "MultiEdit"):
        return {"kind": "tool", "icon": "✎", "label": f"a édité {basename(inp.get('file_path'))}"}
    if name == "Write":
        return {"kind": "tool", "icon": "✚", "label": f"a créé {basename(inp.get('file_path'))}"}
    if name == "NotebookEdit":
        return {"kind": "tool", "icon": "✎", "label": f"a édité le notebook {basename(inp.get('notebook_path'))}"}

    # Lecture / recherche : en-tête seul (renvoient du code)
    if name == "Read":
        return {"kind": "tool", "icon": "▤", "label": f"a lu {basename(inp.get('file_path'))}"}
    if name == "Grep":
        n = count_lines(rtext)
        pat = inp.get("pattern", "")
        return {"kind": "tool", "icon": "⌕", "label": f"a cherché «{pat}» — {n} résultat{'s' if n != 1 else ''}"}
    if name == "Glob":
        return {"kind": "tool", "icon": "⌕", "label": f"a listé {inp.get('pattern','')}"}

    # Bash : commande + sortie (repliée si longue)
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        desc = (inp.get("description") or "").strip()
        return {"kind": "bash", "cmd": cmd, "desc": desc, "out": rtext, "err": err}

    # Agent / délégation
    if name in ("Agent", "Task"):
        sub = inp.get("subagent_type", "agent")
        desc = inp.get("description", "")
        return {"kind": "agent", "label": f"a délégué à l'agent « {sub} » : {desc}", "out": rtext}

    # Plan
    if name == "ExitPlanMode":
        return {"kind": "plan", "text": inp.get("plan", "")}

    # Questions à l'utilisateur
    if name == "AskUserQuestion":
        qs = inp.get("questions", [])
        return {"kind": "ask", "questions": qs, "answer": rtext}

    # Doc / web / MCP : en-tête générique
    if name.startswith("mcp__") or name in ("WebFetch", "WebSearch"):
        q = inp.get("query") or inp.get("url") or inp.get("libraryName") or ""
        short = name.split("__")[-1]
        return {"kind": "tool", "icon": "◷", "label": f"{short} {q}".strip()}

    # Fallback générique
    return {"kind": "tool", "icon": "•", "label": f"a utilisé {name}"}

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
    """Convertisseur Markdown minimal mais robuste (titres, listes, code, citations)."""
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
        # bloc de code ```
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
        # tableau GFM : ligne à pipes suivie d'une ligne séparatrice (|---|---|)
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
        # titres
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            close_lists()
            lvl = min(len(m.group(1)) + 2, 6)  # décale: # devient h3 (h1/h2 réservés à la page)
            out.append(f"<h{lvl}>{inline_md(m.group(2).strip())}</h{lvl}>")
            i += 1; continue
        # citation
        if ln.startswith(">"):
            close_lists()
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i][1:].lstrip()); i += 1
            out.append(f"<blockquote>{inline_md(chr(10).join(buf))}</blockquote>")
            continue
        # liste ordonnée
        m = re.match(r"^\s*\d+\.\s+(.*)$", ln)
        if m:
            if not in_ol: close_lists(); out.append("<ol>"); in_ol = True
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1; continue
        # liste à puces
        m = re.match(r"^\s*[-*+]\s+(.*)$", ln)
        if m:
            if not in_ul: close_lists(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1; continue
        # séparateur
        if re.match(r"^\s*---+\s*$", ln):
            close_lists(); out.append("<hr>"); i += 1; continue
        # ligne vide
        if not ln.strip():
            close_lists(); i += 1; continue
        # paragraphe (fusionne lignes consécutives)
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

# ─────────────────────────── Rendu HTML ───────────────────────────

def slug(i):
    return f"prompt-{i}"

def render_blocks(blocks, sidx):
    """Rend les blocs d'une section. Les conteneurs de texte reçoivent data-bid
    (annotables : surlignage / commentaires côté client)."""
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
            nlines = count_lines(out)
            cls = "term-out err" if b.get("err") else "term-out"
            if out and nlines > 15:
                preview = "\n".join(out.splitlines()[:15])
                body = (
                    f'<div class="{cls} anno" data-bid="{bid()}">{esc(preview)}</div>'
                    f'<details class="more"><summary>+ {nlines - 15} lignes</summary>'
                    f'<div class="{cls} anno" data-bid="{bid()}">{esc(out)}</div></details>'
                )
            elif out:
                body = f'<div class="{cls} anno" data-bid="{bid()}">{esc(out)}</div>'
            else:
                body = ""
            parts.append(
                f'<div class="bash"><div class="term-cmd"><span class="prompt">$</span> {head}</div>{body}</div>'
            )
        elif k == "agent":
            inner = md_to_html(b["out"]) if b.get("out") else ""
            parts.append(
                f'<details class="agent"><summary>{esc(b["label"])}</summary>'
                f'<div class="prose anno" data-bid="{bid()}">{inner}</div></details>'
            )
        elif k == "plan":
            parts.append(
                f'<div class="plan"><div class="plan-head">Plan proposé</div>'
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
                '<details class="ask" open><summary>Questions posées</summary>'
                + "".join(qhtml)
                + (f'<div class="q-answer anno" data-bid="{bid()}">{ans}</div>' if ans else "")
                + "</details>"
            )
    return "\n".join(parts)


def render_session_inner(data, date_str):
    """Rend le CORPS d'une session : entête + TOC interne + sections.
    Retourné comme une string HTML (montée dynamiquement dans le viewer)."""
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

# ─────────────────────────── Template (CSS Anthropic inline) ───────────────────────────

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
    --nav-h:54px;
    --mono:ui-monospace,"Cascadia Code","JetBrains Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--mono);
    font-size:15px;line-height:1.78;-webkit-font-smoothing:antialiased;font-feature-settings:"liga" 0;}
  ::selection{background:rgba(142,156,99,.24);}

  /* ───── Navbar ───── */
  nav#topnav{position:fixed;top:0;left:0;right:0;height:var(--nav-h);z-index:80;
    display:flex;align-items:center;gap:20px;padding:0 22px;
    background:rgba(246,237,220,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);}
  .nav-brand{display:flex;align-items:center;gap:10px;cursor:pointer;font-weight:700;font-size:15px;letter-spacing:-.01em;}
  /* marque Memorium : ruban ondulé (courbe + flèche + 2 nœuds alternés), SVG */
  .brand-svg{display:inline-block;vertical-align:middle;}
  .nav-brand .brand-svg{width:34px;height:auto;}
  .nav-brand:hover{color:var(--accent-d);}
  .nav-tabs{display:flex;gap:4px;margin-left:auto;}
  .nav-tabs button{font-size:13.5px;border:1px solid transparent;background:none;color:var(--muted);
    border-radius:8px;padding:6px 14px;cursor:pointer;letter-spacing:.02em;}
  .nav-tabs button:hover{background:var(--panel);color:var(--ink);}
  .nav-tabs button.active{background:#fff;border-color:var(--line);color:var(--accent-d);font-weight:700;}
  .dirpill{font-size:11.5px;margin-left:12px;padding:4px 10px;border-radius:20px;
    border:1px solid var(--line);color:var(--muted);cursor:pointer;white-space:nowrap;}
  .dirpill:hover{color:var(--ink);border-color:var(--muted);}
  .dirpill.on{color:var(--accent-d);border-color:var(--accent);cursor:default;}
  .dirpill.off{opacity:.6;cursor:not-allowed;}

  .view{display:none;padding-top:var(--nav-h);}
  .view.active{display:block;}

  /* ───── Dashboard ───── */
  .dash-hero{text-align:center;padding:64px 24px 30px;}
  .dash-hero h1{font-size:40px;margin:0 0 10px;letter-spacing:-.02em;font-weight:800;}
  .dash-hero .brand-svg{width:52px;height:auto;margin-right:14px;}
  .dash-hero p{color:var(--muted);font-size:13.5px;margin:0;}
  .dash-hero .accent-rule{height:3px;width:64px;background:var(--accent);margin:22px auto 0;border-radius:2px;}
  .dash-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(258px,1fr));gap:18px;
    max-width:1080px;margin:0 auto;padding:18px 24px 90px;}
  .pcard{background:#fff;border:1px solid var(--line);border-radius:13px;padding:18px 18px 16px;cursor:pointer;
    transition:transform .13s,box-shadow .13s,border-color .13s;position:relative;overflow:hidden;}
  .pcard:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent);opacity:.85;}
  .pcard:hover{transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.09);border-color:var(--accent);}
  .pcard .pname{font-weight:700;font-size:15.5px;margin:0 0 12px;padding-left:8px;line-height:1.3;word-break:break-word;}
  .pcard .pstats{display:flex;gap:16px;padding-left:8px;font-size:12px;color:var(--muted);}
  .pcard .pstats b{color:var(--ink);font-size:18px;font-weight:700;display:block;}
  .pcard .pnotes{position:absolute;top:14px;right:14px;background:var(--accent2);color:#fff;font-size:11px;
    border-radius:20px;padding:2px 9px;font-weight:700;}

  /* ───── Carnet éditable ───── */
  .cn-layout{display:flex;height:calc(100vh - var(--nav-h));}
  .cn-source{flex:0 0 380px;max-width:44%;overflow-y:auto;padding:20px 18px 60px;border-right:1px solid var(--line);background:var(--panel);}
  .cn-src-hint{font-size:11.5px;color:var(--muted);font-style:italic;line-height:1.5;padding:2px 2px 10px;border-bottom:1px dashed var(--line);margin-bottom:6px;}
  .cn-editor{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--paper);}
  .cn-etop{display:flex;align-items:center;gap:12px;padding:16px 26px 12px;border-bottom:1px solid var(--line);}
  .cn-etop .cn-title{font-size:15px;font-weight:800;letter-spacing:-.01em;}
  .cn-status{margin-left:auto;font-size:11.5px;color:var(--muted);}
  .cn-status.ok{color:var(--accent2);} .cn-status.err{color:var(--accent-d);}
  #cn-text{flex:1;width:100%;border:none;outline:none;resize:none;background:transparent;color:var(--ink);
    font-family:var(--mono);font-size:14px;line-height:1.7;padding:20px 26px 90px;}
  #cn-text::placeholder{color:var(--muted);}

  /* ───── Edition ───── */
  .ed-wrap{max-width:880px;margin:0 auto;padding:34px 24px 110px;}
  .ed-head h1{font-size:30px;margin:0 0 8px;font-weight:800;letter-spacing:-.01em;}
  .ed-head p{color:var(--muted);font-size:13px;margin:0 0 8px;max-width:640px;line-height:1.55;}
  #ed-toolbar{margin:10px 0 22px;}
  #ed-newfolder{font-size:13px;font-weight:700;color:#fff;background:var(--accent);border:none;border-radius:8px;padding:8px 14px;cursor:pointer;}
  #ed-newfolder:hover{background:var(--accent-d);}
  .ed-empty{color:var(--muted);font-size:14px;line-height:1.7;padding:36px 0;text-align:center;}
  .ed-folder{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:0 0 16px;padding:12px 14px;transition:border-color .12s,background .12s,box-shadow .12s;}
  .ed-folder.drop{border-color:var(--accent);background:#F0E7D0;box-shadow:0 0 0 2px rgba(142,156,99,.18);}
  .ed-fhead{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
  .ed-fname{font-size:14px;font-weight:700;color:var(--accent-d);background:#fff;border:1px solid var(--line);border-radius:7px;padding:5px 9px;min-width:180px;}
  .ed-fname:focus{outline:none;border-color:var(--accent);}
  .ed-fcount{font-size:11px;color:var(--muted);}
  .ed-del{margin-left:auto;background:none;border:1px solid var(--line);color:var(--muted);border-radius:6px;padding:2px 9px;cursor:pointer;font-size:12px;}
  .ed-del:hover{color:#fff;background:var(--accent-d);border-color:var(--accent-d);}
  .ed-sess{display:flex;flex-direction:column;gap:4px;}
  .ed-row{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:7px;padding:6px 9px;cursor:grab;}
  .ed-row.dragging{opacity:.4;}
  .ed-grip{color:var(--muted);font-size:13px;}
  .ed-title{flex:1;font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .ed-move{font-size:11.5px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--muted);padding:2px 5px;max-width:140px;cursor:pointer;}
  .ed-move:hover{border-color:var(--accent);color:var(--ink);}
  .ed-ren{background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:2px 6px;border-radius:5px;}
  .ed-ren:hover{color:var(--accent-d);background:var(--panel);}
  .ed-drop-hint{font-size:12px;color:var(--muted);font-style:italic;padding:9px;text-align:center;border:1px dashed var(--line);border-radius:7px;}
  .cn-empty{color:var(--muted);text-align:center;padding:70px 20px;line-height:1.8;}
  .cn-proj{margin:30px 0 0;}
  .cn-proj > h2{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent-d);font-weight:700;
    margin:0 0 4px;display:flex;align-items:baseline;gap:10px;}
  .cn-proj > h2 .c{font-size:11px;color:var(--muted);font-weight:400;letter-spacing:0;text-transform:none;}
  .cn-sess{margin:14px 0 0;}
  .cn-sess > h3{font-size:13.5px;font-weight:700;margin:0 0 7px;color:var(--ink);cursor:pointer;}
  .cn-sess > h3:hover{color:var(--accent-d);}
  .cn-item{display:flex;gap:11px;background:#fff;border:1px solid var(--line);border-radius:9px;
    padding:10px 13px;margin:0 0 8px;cursor:pointer;transition:.12s;}
  .cn-item:hover{border-color:var(--accent2);transform:translateX(-2px);box-shadow:0 2px 10px rgba(0,0,0,.05);}
  .cn-dot{flex:none;width:11px;height:11px;border-radius:3px;margin-top:5px;}
  .cn-body{min-width:0;}
  .cn-quote{font-size:12.5px;color:var(--muted);border-left:2px solid var(--line);padding-left:9px;
    font-style:italic;word-break:break-word;}
  .cn-note{font-size:13.5px;color:var(--ink);margin-top:5px;}
  .cn-source .cn-sess>h3,.cn-source .cn-item{cursor:grab;}
  .cn-source .cn-sess>h3:active,.cn-source .cn-item:active{cursor:grabbing;}

  .layout{display:grid;grid-template-columns:var(--side-w,300px) 1fr;}

  /* ───── Sidebar : projets → sessions ───── */
  aside#sidebar{position:sticky;top:var(--nav-h);align-self:start;height:calc(100vh - var(--nav-h));overflow:hidden;
    background:var(--panel);border-right:1px solid var(--line);}
  #side-inner{height:100%;overflow-y:auto;padding:18px 16px 60px;}
  #side-resizer{position:absolute;top:0;right:0;width:6px;height:100%;cursor:col-resize;z-index:5;transition:background .12s;}
  #side-resizer:hover,#side-resizer.drag{background:var(--accent);opacity:.4;}
  #search{width:100%;font-size:13.5px;border:1px solid var(--line);border-radius:8px;
    padding:8px 11px;background:#fff;color:var(--ink);margin-bottom:14px;}
  #search:focus{outline:none;border-color:var(--accent);}

  .proj{margin-bottom:4px;}
  .proj-head{display:flex;align-items:center;gap:7px;padding:6px 8px;cursor:pointer;border-radius:7px;
    font-size:13px;letter-spacing:.04em;color:var(--accent-d);font-weight:700;text-transform:uppercase;}
  .proj-head:hover{background:#F2EAD8;}
  .proj-head .caret{transition:transform .15s;color:var(--muted);font-size:10px;}
  .proj.collapsed .caret{transform:rotate(-90deg);}
  .proj-head .pc{margin-left:auto;font-size:10.5px;color:var(--muted);font-weight:400;}
  .proj-sessions{padding:2px 0 6px;}
  .proj.collapsed .proj-sessions{display:none;}

  .sess{padding:7px 10px;border-radius:7px;cursor:pointer;border-left:2px solid transparent;margin:1px 0;}
  .sess:hover{background:#F2EAD8;}
  .sess.active{background:#fff;border-left-color:var(--accent);}
  .sess .st{font-size:13.5px;color:var(--ink);line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .sess.active .st{color:var(--ink);font-weight:600;}
  .sess .sm{font-size:11.5px;color:var(--muted);margin-top:3px;display:flex;gap:8px;}
  .sess .sm .pr{color:var(--accent2);}
  .sess-toc{list-style:none;margin:4px 0 2px;padding:0 0 0 6px;border-left:1px dashed var(--line);}
  .sess-toc a{display:flex;gap:8px;align-items:baseline;padding:3px 8px;border-radius:6px;color:var(--muted);
    text-decoration:none;font-size:11.5px;}
  .sess-toc a .n{color:var(--accent);font-size:10px;min-width:18px;}
  .sess-toc a:hover{background:#F2EAD8;color:var(--ink);}
  .sess-toc a.active{color:var(--ink);font-weight:600;}

  /* ───── Zone principale (lecture) ───── */
  main{padding:0 0 200px;min-height:calc(100vh - var(--nav-h));position:relative;}
  #emptyread{display:flex;flex-direction:column;align-items:center;justify-content:center;height:calc(100vh - var(--nav-h));
    text-align:center;color:var(--muted);padding:40px;}
  #emptyread .big{font-size:20px;color:var(--ink);margin-bottom:10px;font-weight:700;}
  #emptyread kbd{background:#fff;border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:12px;}
  #viewer{padding:40px 0 0;}
  #viewer:empty{display:none;}

  .gridrow{display:grid;grid-template-columns:74px minmax(0,720px) 286px;column-gap:30px;justify-content:center;position:relative;}
  .dochead{margin:0 0 56px;}
  .dochead .col-read{grid-column:2;}
  .dochead h1{font-size:30px;line-height:1.18;margin:0 0 12px;font-weight:700;letter-spacing:-.01em;}
  .dochead .sub{color:var(--muted);font-size:12.5px;}
  .dochead .accent-rule{height:3px;width:60px;background:var(--accent);margin-top:18px;border-radius:2px;}

  .exchange{display:grid;grid-template-columns:74px minmax(0,720px) 286px;column-gap:30px;justify-content:center;position:relative;margin:0 0 78px;}
  .exchange-n{grid-column:1;text-align:right;font-size:30px;font-weight:700;color:var(--accent);
    line-height:1;padding-top:2px;position:sticky;top:calc(var(--nav-h) + 18px);align-self:start;letter-spacing:-.02em;}
  .col-read{grid-column:2;min-width:0;}
  .gutter{grid-column:3;position:relative;min-width:0;}

  .prompt{background:#fff;border:1px solid var(--line);border-left:3px solid var(--accent);
    border-radius:9px;padding:13px 17px;margin:0 0 26px;box-shadow:0 1px 2px rgba(0,0,0,.03);}
  .prompt-tag{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent-d);font-weight:700;margin-bottom:5px;}
  .prompt-body{font-size:14px;line-height:1.75;}
  .prompt-body p{margin:.35em 0;}

  .prose p{margin:.95em 0;}
  .prose h3{font-size:17px;margin:1.5em 0 .5em;font-weight:700;color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:4px;}
  .prose h4,.prose h5,.prose h6{font-size:14px;margin:1.3em 0 .4em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-d);}
  .prose ul,.prose ol{padding-left:1.3em;margin:.7em 0;}
  .prose li{margin:.28em 0;}
  .prose li::marker{color:var(--accent2);}
  .prose blockquote{border-left:3px solid var(--accent2-l);margin:1em 0;padding:.1em 1em;color:var(--muted);}
  .prose hr{border:none;border-top:1px solid var(--line);margin:1.7em 0;}
  .prose a{color:var(--accent-d);text-underline-offset:2px;}
  .prose strong{color:var(--accent-d);font-weight:700;}
  code{font-family:var(--mono);font-size:.92em;background:var(--code-bg);padding:.08em .35em;border-radius:4px;border:1px solid #e4ddcc;}
  .prose strong code{color:var(--accent2);}
  pre.code{background:var(--term-bg);color:var(--term-ink);border-radius:9px;padding:13px 15px;overflow:auto;margin:1.1em 0;max-height:460px;}
  pre.code code{background:none;padding:0;border:none;font-size:12.8px;line-height:1.55;color:var(--term-ink);}

  .tool{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:6px;padding:3px 11px;margin:4px 5px 4px 0;}
  .tool-icon{color:var(--accent2);}

  .bash{margin:16px 0;border-radius:9px;overflow:hidden;border:1px solid #171B10;box-shadow:0 2px 12px rgba(0,0,0,.13);font-size:12.6px;}
  .term-cmd{background:#171B10;color:var(--term-ink);padding:8px 13px;font-family:var(--mono);}
  .term-cmd .prompt{color:var(--term-green);margin-right:7px;font-weight:700;}
  .term-out{background:var(--term-bg);color:var(--term-ink);padding:10px 13px;white-space:pre-wrap;word-break:break-word;line-height:1.5;font-family:var(--mono);}
  .term-out.err{color:var(--term-amber);}
  details.more summary{background:var(--term-bg);color:var(--accent2-l);padding:6px 13px;cursor:pointer;font-size:11.5px;border-top:1px solid #343D28;list-style:none;}
  details.more summary::-webkit-details-marker{display:none;}
  details.more summary:before{content:"▸ ";}
  details.more[open] summary:before{content:"▾ ";}
  details.more .term-out{border-top:1px solid #343D28;}

  details.agent{margin:13px 0;border:1px dashed var(--line);border-radius:9px;background:#FBFAF6;}
  details.agent summary{cursor:pointer;padding:9px 14px;font-size:12.5px;color:var(--muted);}
  details.agent[open] summary{color:var(--accent-d);}
  details.agent .prose{padding:0 16px 12px;font-size:14px;}
  .plan{margin:16px 0;border:1px solid var(--line);border-radius:9px;background:#fff;overflow:hidden;}
  .plan-head{background:var(--panel);padding:7px 15px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent-d);font-weight:700;}
  .plan .prose{padding:2px 16px 12px;}
  details.ask{margin:16px 0;border:1px solid var(--line);border-radius:9px;background:#fff;overflow:hidden;}
  details.ask summary{cursor:pointer;padding:9px 14px;background:var(--panel);font-size:12.5px;color:var(--accent-d);font-weight:700;}
  .ask .q{padding:7px 16px;border-top:1px solid var(--line);}
  .ask .q-text{font-weight:700;margin:6px 0;}
  .ask .q-opts{font-size:13px;color:var(--muted);}
  .ask .q-answer{padding:9px 16px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);white-space:pre-wrap;}
  .interrupt{font-size:12px;color:var(--accent-d);background:#FBEFEA;border-radius:6px;padding:5px 11px;margin:10px 0;display:inline-block;}

  mark.hl{background:var(--c,var(--hl1));color:inherit;border-radius:2px;padding:.02em 0;box-decoration-break:clone;-webkit-box-decoration-break:clone;cursor:pointer;}
  mark.hl.has-note{border-bottom:2px solid var(--accent);}
  mark.hl.flash{animation:flash 1.1s ease;}
  @keyframes flash{0%,100%{box-shadow:none}30%{box-shadow:0 0 0 3px var(--accent2-l)}}
  .gutter .cmt-card{position:absolute;left:0;right:6px;background:#fff;border:1px solid var(--line);border-left:3px solid var(--accent2);border-radius:7px;padding:8px 10px;font-size:11.5px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:transform .12s,box-shadow .12s;}
  .gutter .cmt-card:hover{transform:translateX(-3px);box-shadow:0 3px 10px rgba(0,0,0,.10);}
  .cmt-quote{color:var(--muted);margin-bottom:3px;border-left:2px solid var(--line);padding-left:6px;}
  .cmt-note{color:var(--ink);}

  #seltools{position:fixed;z-index:90;display:none;gap:3px;align-items:center;background:#566340;border-radius:9px;padding:5px;box-shadow:0 6px 24px rgba(0,0,0,.28);transform:translate(-50%,-118%);}
  #seltools .sw{width:17px;height:17px;border-radius:5px;cursor:pointer;border:1px solid rgba(255,255,255,.18);}
  #seltools .sw:hover{transform:scale(1.12);}
  #seltools .sep{width:1px;height:18px;background:#6B7A50;margin:0 3px;}
  #seltools .cmt{background:none;border:none;color:#eee;font-size:12px;padding:4px 8px;border-radius:6px;cursor:pointer;}
  #seltools .cmt:hover{background:#475434;}
  #notepop{position:fixed;z-index:95;display:none;width:266px;background:#fff;border:1px solid var(--line);border-radius:11px;box-shadow:0 10px 30px rgba(0,0,0,.20);padding:12px;}
  #notepop .swatches{display:flex;gap:7px;margin-bottom:10px;}
  #notepop .swatches .sw{width:20px;height:20px;border-radius:6px;cursor:pointer;border:2px solid transparent;}
  #notepop .swatches .sw.on{border-color:var(--ink);box-shadow:0 0 0 1px #fff inset;}
  #notepop textarea{width:100%;min-height:70px;resize:vertical;font-size:13px;border:1px solid var(--line);border-radius:7px;padding:8px;background:var(--paper);color:var(--ink);}
  #notepop .row{display:flex;justify-content:space-between;margin-top:9px;}
  #notepop button{font-size:12px;border-radius:6px;padding:5px 11px;cursor:pointer;border:1px solid var(--line);background:#fff;}
  #notepop .save{background:var(--accent);border-color:var(--accent);color:#fff;}
  #notepop .del{color:var(--accent-d);border-color:#eeccc2;}

  #pager{position:fixed;right:22px;bottom:22px;z-index:75;display:none;align-items:center;gap:2px;background:#566340;color:#eee;border-radius:10px;padding:5px;box-shadow:0 6px 22px rgba(0,0,0,.25);}
  #pager button{background:none;border:none;color:#eee;font-size:16px;cursor:pointer;padding:4px 10px;border-radius:7px;}
  #pager button:hover{background:#475434;color:var(--accent2-l);}
  #pager .pos{font-size:12px;color:#cfc9bf;padding:0 4px;min-width:62px;text-align:center;}
  #pager .pos b{color:var(--accent);}
  #cbtn{position:fixed;right:22px;top:calc(var(--nav-h) + 14px);z-index:75;background:#566340;color:#eee;border:none;font-size:12px;border-radius:9px;padding:9px 13px;cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.25);display:none;align-items:center;gap:8px;}
  #cbtn:hover{background:#475434;}
  #cbtn .badge{background:var(--accent2);color:#fff;border-radius:20px;font-size:11px;padding:1px 7px;min-width:20px;text-align:center;}
  #recap{position:fixed;top:0;right:0;z-index:85;width:340px;max-width:90vw;height:100vh;background:var(--panel);border-left:1px solid var(--line);box-shadow:-8px 0 30px rgba(0,0,0,.10);transform:translateX(100%);transition:transform .22s ease;display:flex;flex-direction:column;}
  #recap.open{transform:translateX(0);}
  #recap header{padding:18px 18px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;}
  #recap header h2{font-size:13px;letter-spacing:.12em;text-transform:uppercase;margin:0;color:var(--accent-d);}
  #recap .close{background:none;border:none;font-size:18px;cursor:pointer;color:var(--muted);}
  #recap-list{padding:12px;overflow-y:auto;flex:1;}
  #recap .empty{color:var(--muted);font-size:12.5px;text-align:center;padding:40px 16px;line-height:1.7;}
  .recap-item{background:#fff;border:1px solid var(--line);border-radius:8px;padding:11px 12px;margin-bottom:10px;cursor:pointer;transition:.12s;}
  .recap-item:hover{border-color:var(--accent2);transform:translateX(-2px);}
  .ri-head{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--accent2);font-weight:700;margin-bottom:5px;}
  .ri-quote{font-size:11.5px;color:var(--muted);border-left:2px solid var(--line);padding-left:7px;margin-bottom:5px;}
  .ri-note{font-size:13px;color:var(--ink);}

  @media (max-width:1180px){ .gridrow,.exchange{grid-template-columns:60px minmax(0,720px) 0;column-gap:20px;} .gutter{display:none;} }
  @media (max-width:860px){
    .layout{grid-template-columns:1fr;}
    aside#sidebar{position:static;height:auto;border-right:none;border-bottom:1px solid var(--line);}
    #side-inner{max-height:46vh;}
    #side-resizer{display:none;}
    .gridrow,.exchange{grid-template-columns:40px 1fr 0;column-gap:14px;}
    #viewer{padding:24px 16px 0;}
    .exchange-n{font-size:20px;}
    #findbar{left:12px;right:12px;bottom:12px;}
  }

  /* ═══════ Transitions entre vues ═══════ */
  .view.active{animation:viewIn .42s cubic-bezier(.22,.7,.2,1) both;}
  @keyframes viewIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  body.welcome #topnav{display:none;}

  /* ═══════ Tableaux — rendus lisibles (fini les pipes bruts) ═══════ */
  .tbl-wrap{overflow-x:auto;margin:13px 0;border:1px solid var(--line);border-radius:8px;}
  .tbl-wrap table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.55;}
  .tbl-wrap th,.tbl-wrap td{padding:7px 13px;border-bottom:1px solid var(--line);
    border-right:1px solid var(--line);text-align:left;vertical-align:top;}
  .tbl-wrap thead th{background:rgba(142,156,99,.10);font-weight:700;white-space:nowrap;}
  .tbl-wrap tbody tr:last-child td{border-bottom:none;}
  .tbl-wrap th:last-child,.tbl-wrap td:last-child{border-right:none;}
  .tbl-wrap tbody tr:hover{background:rgba(0,0,0,.02);}
  .tbl-wrap code{white-space:nowrap;}

  /* ═══════ Welcome / hero ruban-mémoire WebGL ═══════ */
  #view-welcome{padding-top:0;}
  #view-welcome.active{display:flex;align-items:center;justify-content:center;min-height:100vh;}
  /* fond du welcome : quadrillage léger + vignettage de profondeur */
  body.welcome::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background:
      radial-gradient(130% 100% at 50% 40%,transparent 0 48%,rgba(86,99,64,.13) 100%),
      repeating-linear-gradient(0deg,rgba(86,99,64,.05) 0 1px,transparent 1px 38px),
      repeating-linear-gradient(90deg,rgba(86,99,64,.05) 0 1px,transparent 1px 38px);}
  .welcome-inner{position:relative;z-index:1;text-align:center;width:100%;padding:20px;}
  /* bande 3D large en haut, titre tapé dessous */
  .hero-stage{position:relative;width:min(1460px,98vw);height:300px;margin:0 auto -6px;}
  #gl{position:absolute;inset:0;width:100%;height:100%;display:block;}
  #glfail{display:none;color:var(--muted);font-size:13px;padding:40px;}
  /* signature : barre de recherche qui tape une requête, un nœud répond */
  .searchbar{position:absolute;left:50%;top:2%;transform:translateX(-50%);z-index:2;
    display:flex;align-items:center;gap:10px;min-width:240px;
    background:rgba(246,237,220,.7);backdrop-filter:blur(9px);
    border:1px solid var(--line);border-radius:13px;padding:10px 16px;
    font:14px var(--mono);color:var(--ink);box-shadow:0 10px 28px rgba(86,99,64,.14);
    opacity:0;transition:opacity .5s;pointer-events:none;}
  .searchbar .mag{color:var(--accent);font-size:16px;line-height:1;}
  .searchbar .q{white-space:nowrap;}
  .searchbar .scaret{display:inline-block;width:.5ch;height:1.05em;background:var(--accent);
    vertical-align:-.15em;animation:caret 1s step-end infinite;}
  /* étiquettes de commit projetées sur les nœuds : la bande devient un git log vivant */
  .nlabel{position:absolute;left:0;top:0;z-index:2;font:10.5px/1.3 var(--mono);
    color:var(--ink);white-space:nowrap;pointer-events:none;opacity:0;
    transform:translate(-50%,-50%);
    /* halo couleur papier (casing carto) : lisible même posé sur la bande */
    text-shadow:0 0 3px var(--paper),0 0 3px var(--paper),0 0 5px var(--paper),0 0 8px var(--paper);}
  .nlabel .h{color:var(--accent-d);opacity:.85;letter-spacing:.02em;}
  .nlabel.hit{font-weight:700;
    text-shadow:0 0 3px var(--paper),0 0 3px var(--paper),0 0 5px var(--paper),0 0 9px rgba(142,156,99,.85);}
  .nlabel.hit .h{color:var(--accent);opacity:1;}
  #wTitle{font-size:clamp(44px,7vw,88px);font-weight:800;letter-spacing:-.04em;margin:0;color:#566340;
    white-space:nowrap;min-height:1.1em;line-height:1;}
  #wTitle:after{content:"";display:inline-block;width:.055em;height:.9em;margin-left:.05em;vertical-align:-.1em;
    background:var(--accent);animation:caret 1s step-end infinite;}
  #wTitle.typed:after{animation:none;opacity:0;}
  @keyframes caret{50%{opacity:0}}
  .welcome-inner .wsub{color:var(--muted);font-size:15px;
    opacity:0;animation:upFade .8s 3.1s forwards;}
  .enter-btn{margin-top:28px;font-size:14px;font-weight:700;color:#fff;background:var(--accent);
    border:none;border-radius:11px;padding:13px 26px;cursor:pointer;letter-spacing:.01em;
    box-shadow:0 6px 20px rgba(142,156,99,.36);transition:transform .14s,box-shadow .14s,background .14s;
    opacity:0;animation:upFade .8s 3.25s forwards;}
  .enter-btn:hover{background:var(--accent-d);transform:translateY(-2px);box-shadow:0 10px 28px rgba(110,122,75,.44);}
  .enter-btn:active{transform:translateY(0);}
  .welcome-inner .wnote{margin-top:22px;font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--muted);opacity:0;animation:upFade .8s 3.4s forwards;}
  @keyframes upFade{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){
    #wTitle:after{animation:none;opacity:0;}
    .welcome-inner .wsub,.enter-btn,.welcome-inner .wnote{animation:none!important;opacity:1;transform:none;}
  }

  /* ═══════ Résultats de recherche plein-texte ═══════ */
  .sr-head{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
    font-weight:700;margin:2px 2px 11px;}
  .sr-none{color:var(--muted);font-size:12.5px;text-align:center;padding:34px 12px;line-height:1.7;}
  .sresult{background:#fff;border:1px solid var(--line);border-radius:9px;padding:10px 12px;margin:0 0 8px;
    cursor:pointer;transition:transform .12s,border-color .12s,box-shadow .12s;}
  .sresult:hover{border-color:var(--accent);transform:translateX(-2px);box-shadow:0 3px 12px rgba(0,0,0,.06);}
  .sresult .sr-title{font-size:12.5px;font-weight:600;color:var(--ink);line-height:1.35;margin-bottom:4px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .sresult .sr-meta{font-size:10.5px;color:var(--muted);display:flex;gap:9px;align-items:center;margin-bottom:5px;flex-wrap:wrap;}
  .sresult .sr-count{color:var(--accent-d);font-weight:700;}
  .sresult .sr-snip{font-size:11.5px;color:var(--muted);line-height:1.55;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
  .sresult .sr-snip mark{background:var(--hl4);color:inherit;padding:0 1px;border-radius:2px;}

  /* ═══════ Find-bar (occurrences dans la session) ═══════ */
  mark.find{background:#FFE39A;color:inherit;border-radius:2px;padding:.02em 0;
    box-decoration-break:clone;-webkit-box-decoration-break:clone;}
  mark.find.cur{background:var(--accent);color:#fff;}
  #findbar{position:fixed;left:320px;bottom:22px;z-index:78;display:none;align-items:center;gap:6px;
    background:#566340;color:#eee;border-radius:10px;padding:6px 8px;box-shadow:0 6px 22px rgba(0,0,0,.25);
    font-size:12px;}
  #findbar.show{display:flex;}
  #findbar .fq{max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--accent2-l);}
  #findbar button{background:none;border:none;color:#eee;font-size:15px;cursor:pointer;
    padding:2px 8px;border-radius:6px;line-height:1;}
  #findbar button:hover{background:#475434;color:var(--accent2-l);}
  #findbar .fpos{min-width:44px;text-align:center;color:#cfc9bf;}
  #findbar .fpos b{color:var(--accent);}
  #findbar .fclose{font-size:13px;}

  /* ═══════ Lisibilité ═══════ */
  .prose{font-size:14px;}
  .prose h3{margin-top:1.7em;}
</style>
</head>
<body>
<nav id="topnav">
  <div class="nav-brand" id="navHome"><svg class="brand-svg" viewBox="0 0 48 24" fill="none" aria-hidden="true"><path d="M3 12 C9 5.5, 15 18.5, 22 12 C29 5.5, 34 16.5, 40 12" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round"/><path d="M40 8.5 L45 12 L40 15.5" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M11 8.5 V6" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="11" cy="4.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/><path d="M30 14.5 V17.5" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="30" cy="19.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/></svg>Memorium</div>
  <div class="nav-tabs">
    <button data-view="dash" class="active">Dashboard</button>
    <button data-view="carnet">Carnet</button>
    <button data-view="edition">Edition</button>
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
    <p class="wsub">La mémoire vivante de tes sessions Claude&nbsp;Code — relis, cherche, annote.</p>
    <button id="enterBtn" class="enter-btn">Ouvrir le journal →</button>
    <div class="wnote">__COUNT__ conversations indexées</div>
  </div>
</section>

<section id="view-dash" class="view">
  <div class="dash-hero">
    <h1><svg class="brand-svg" viewBox="0 0 48 24" fill="none" aria-hidden="true"><path d="M3 12 C9 5.5, 15 18.5, 22 12 C29 5.5, 34 16.5, 40 12" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round"/><path d="M40 8.5 L45 12 L40 15.5" stroke="var(--accent)" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M11 8.5 V6" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="11" cy="4.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/><path d="M30 14.5 V17.5" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round"/><circle cx="30" cy="19.4" r="2.7" fill="var(--accent2)" stroke="var(--accent-d)" stroke-width="1.5"/></svg>Memorium</h1>
    <p>__COUNT__ conversations · la mémoire de tes sessions Claude Code</p>
    <div class="accent-rule"></div>
  </div>
  <div id="dash-grid" class="dash-grid"></div>
</section>

<section id="view-read" class="view">
  <div class="layout">
    <aside id="sidebar">
      <div id="side-resizer" title="Glisser pour redimensionner"></div>
      <div id="side-inner">
        <input id="search" placeholder="Rechercher un mot ou une expression…" autocomplete="off">
        <div id="sesslist"></div>
      </div>
    </aside>
    <main>
      <div id="emptyread">
        <div class="big">Choisis une session à gauche</div>
        <div>Sélectionne du texte pour <b>surligner</b> / <b>commenter</b> · <kbd>j</kbd>/<kbd>k</kbd> navigue entre prompts.</div>
      </div>
      <div id="viewer"></div>
    </main>
  </div>
</section>

<section id="view-carnet" class="view">
  <div class="cn-layout">
    <div class="cn-source" id="cn-source"></div>
    <div class="cn-editor">
      <div class="cn-etop">
        <span class="cn-title">Carnet</span>
        <span class="cn-status" id="cn-status"></span>
      </div>
      <textarea id="cn-text" spellcheck="false" placeholder="Écris ici. Glisse une annotation depuis la gauche pour l'insérer en citation."></textarea>
    </div>
  </div>
</section>

<section id="view-edition" class="view">
  <div class="ed-wrap">
    <div class="ed-head">
      <h1>Edition</h1>
      <p>Renomme dossiers et sessions, glisse une session vers un autre dossier. Rien ne bouge dans <code>~/.claude/projects</code> — tout est enregistré dans <code>data/metadata.json</code>.</p>
      <div class="accent-rule"></div>
    </div>
    <div id="ed-toolbar"><button id="ed-newfolder">＋ Nouveau dossier</button></div>
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
  <button id="findprev" title="Précédent">‹</button>
  <span class="fpos"><b id="fcur">0</b>/<span id="ftot">0</span></span>
  <button id="findnext" title="Suivant">›</button>
  <button id="findclose" class="fclose" title="Fermer">✕</button>
</div>

<script src="searchindex.js"></script>
<script>
const MANIFEST = __MANIFEST__;
const cache = {};
let curSid=null, anns=[], pendingSid=null, curView="dash", afterMountAid=null, curProject=null;
let META={folders:{},sessions:{}};   // overrides chargés depuis data/metadata.json (couche dérivée, jamais la source)
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
  if(!FS_OK){alert("Écriture disque non supportée — utilise Chrome/Edge.");return false;}
  rootDir=await window.showDirectoryPicker({mode:"readwrite"});
  await idbSet(HKEY,rootDir); updateDirPill(); await initStore(); return true;
}
async function restoreDir(){                    // au chargement : réutilise le handle mémorisé
  if(!FS_OK){updateDirPill();return;}
  const h=await idbGet(HKEY); if(!h){updateDirPill();return;}
  if(await h.queryPermission({mode:"readwrite"})==="granted"){rootDir=h;updateDirPill();await initStore();}
  else updateDirPill();                          // permission perdue → bouton "Reconnecter"
}
async function reconnectDir(){                   // re-demande la permission (1 geste imposé par l'API)
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
  const d=await dataDir(); if(!d)throw new Error("Dossier non connecté");
  const w=await (await d.getFileHandle(name,{create:true})).createWritable();
  await w.write(text); await w.close();
}
async function initStore(){                      // charge metadata.json → META, puis rafraîchit
  if(!rootDir)return;
  let meta=await readData("metadata.json");
  if(meta===null){await writeData("metadata.json","{}\n");meta="{}";}
  try{const j=JSON.parse(meta); META={folders:j.folders||{},sessions:j.sessions||{}};}
  catch(e){console.warn("metadata.json illisible, ignoré",e);}
  buildDashboard();
  if(curView==="carnet") buildCarnet();
  if(curView==="read") buildSidebar("");
  if(curSid) document.querySelectorAll(".sess").forEach(e=>e.classList.toggle("active",e.dataset.sid===curSid));
  console.log("[memorium] store OK —",Object.keys(META.folders).length,"dossiers,",Object.keys(META.sessions).length,"sessions modifiées");
}
function updateDirPill(){
  const el=document.getElementById("dirpill"); if(!el)return;
  if(!FS_OK){el.textContent="Écriture indispo (Chrome/Edge)";el.className="dirpill off";el.onclick=null;return;}
  if(rootDir){el.textContent="● Dossier connecté";el.className="dirpill on";el.onclick=null;}
  else{el.textContent="○ Connecter le dossier export";el.className="dirpill";
       el.onclick=async()=>{(await idbGet(HKEY))?reconnectDir():connectDir();};}
}

/* ───── Navigation entre vues ───── */
function setView(v){
  const prev=curView;
  if(prev==="carnet"&&v!=="carnet")cnFlush();
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
  if(v==="carnet")buildCarnet();
  if(v==="edition")buildEdition();
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

/* ───── Carnet éditable : gauche = source draggable, droite = éditeur → carnet.md ───── */
let cnTimer=null, cnLoaded=false;
function cnSnippetAnn(m,a){                       // annotation → citation + lien retour
  let s="\n> « "+(a.quote||"").trim()+" »\n";
  if(a.note)s+=a.note.trim()+"\n";
  s+="— ["+effTitle(m)+"](session:"+m.sid+"#"+a.id+")\n";
  return s;
}
function cnSnippetSess(m){ return "\n— ["+effTitle(m)+"](session:"+m.sid+")\n"; }
function cnStatus(state){
  const el=document.getElementById("cn-status"); if(!el)return;
  el.className="cn-status"+(state==="saved"?" ok":state==="error"?" err":"");
  el.textContent=state==="saving"?"✎ enregistrement…":state==="saved"?"✓ enregistré":state==="error"?"✗ écriture impossible":"";
}
function cnSchedule(){ clearTimeout(cnTimer); cnStatus("saving"); cnTimer=setTimeout(cnFlush,600); }
async function cnFlush(){                         // écrit carnet.md ; appelé au débounce ET au changement de vue
  clearTimeout(cnTimer); cnTimer=null;
  const ta=document.getElementById("cn-text"); if(!ta||!rootDir)return;
  try{await writeData("carnet.md",ta.value); cnStatus("saved");}
  catch(e){cnStatus("error");}
}
function buildCarnet(){
  const src=document.getElementById("cn-source"); src.innerHTML="";
  const ta=document.getElementById("cn-text"); ta.oninput=cnSchedule;
  // charge carnet.md une seule fois ; le flush au changement de vue protège les frappes non sauvées
  if(rootDir && !cnLoaded){ cnLoaded=true; readData("carnet.md").then(t=>{ if(t!==null) ta.value=t; }); }
  const hint=document.createElement("div"); hint.className="cn-src-hint";
  hint.textContent=rootDir?"Glisse une annotation ou une session dans l'éditeur →":"Connecte le dossier export (pastille en haut à droite) pour enregistrer ton carnet.";
  src.appendChild(hint);
  const g=projectGroups();
  const order=Object.keys(g).sort((a,b)=>Math.max(...g[b].map(s=>s.mtime))-Math.max(...g[a].map(s=>s.mtime)));
  let total=0;
  order.forEach(proj=>{
    const withAnns=g[proj].slice().sort((a,b)=>b.mtime-a.mtime)
      .map(m=>({m,list:loadAnns(m.sid)})).filter(x=>x.list.length);
    if(!withAnns.length)return;
    const pcount=withAnns.reduce((t,x)=>t+x.list.length,0); total+=pcount;
    const pd=document.createElement("div"); pd.className="cn-proj";
    pd.innerHTML='<h2>'+esc(folderName(proj))+'<span class="c">'+pcount+' annotation'+(pcount>1?'s':'')+'</span></h2>';
    withAnns.forEach(({m,list})=>{
      const sd=document.createElement("div"); sd.className="cn-sess";
      const h=document.createElement("h3"); h.textContent=effTitle(m);
      h.draggable=true;
      h.ondragstart=e=>{e.dataTransfer.setData("text/plain",cnSnippetSess(m));e.dataTransfer.effectAllowed="copy";};
      h.onclick=()=>gotoAnn(m.sid,null); sd.appendChild(h);
      list.slice().sort((a,b)=>a.start-b.start).forEach(a=>{
        const it=document.createElement("div"); it.className="cn-item";
        it.innerHTML='<span class="cn-dot" style="background:'+SWVAR[a.color||"1"]+'"></span>'+
          '<div class="cn-body"><div class="cn-quote">'+esc(a.quote||"")+'</div>'+
          (a.note?'<div class="cn-note">'+esc(a.note)+'</div>':'')+'</div>';
        it.draggable=true;
        it.ondragstart=e=>{e.dataTransfer.setData("text/plain",cnSnippetAnn(m,a));e.dataTransfer.effectAllowed="copy";e.stopPropagation();};
        it.onclick=()=>gotoAnn(m.sid,a.id);
        sd.appendChild(it);
      });
      pd.appendChild(sd);
    });
    src.appendChild(pd);
  });
  if(!total){const e=document.createElement("div");e.className="cn-empty";e.innerHTML="Aucune annotation pour l'instant.<br>Surligne un passage dans une session pour la voir apparaître ici.";src.appendChild(e);}
}
function gotoAnn(sid,aid){
  afterMountAid=aid;
  setView("read");
  if(curSid===sid){flashAnn(aid);afterMountAid=null;}
  else openSession(sid);
}
function flashAnn(aid){
  if(!aid)return;
  const m=viewer.querySelector('mark.hl[data-aid="'+aid+'"]');
  if(m){m.scrollIntoView({behavior:"smooth",block:"center"});m.classList.add("flash");setTimeout(()=>m.classList.remove("flash"),1100);}
}

/* ───── Edition : dossiers & sessions (couche metadata.json) ───── */
async function saveMeta(){
  if(!rootDir){alert("Connecte d'abord le dossier export (pastille en haut à droite).");return false;}
  try{await writeData("metadata.json",JSON.stringify(META,null,2));return true;}
  catch(e){alert("Écriture impossible : "+e.message);return false;}
}
function cleanupSession(sid){                    // retire l'entrée si plus aucun override
  const o=META.sessions[sid]; if(o&&!o.title&&!o.folder)delete META.sessions[sid];
}
async function moveSession(sid,id){              // déplace une session vers le dossier `id`
  const m=metaOf(sid); if(!m||effFolder(m)===id)return;
  META.sessions[sid]=META.sessions[sid]||{};
  if(id===m.project)delete META.sessions[sid].folder; else META.sessions[sid].folder=id;  // retour à l'origine = pas d'override
  cleanupSession(sid);
  if(await saveMeta()){buildEdition();buildDashboard();}
}
function allFolderIds(){
  const s=new Set();
  MANIFEST.forEach(m=>s.add(m.project));         // dossiers-projets d'origine
  Object.keys(META.folders).forEach(id=>s.add(id));
  return [...s].filter(id=>!(META.folders[id]&&META.folders[id].deleted));  // masque les projets vidés supprimés
}
function buildEdition(){
  const body=document.getElementById("ed-body"); body.innerHTML="";
  if(!rootDir){
    body.innerHTML='<div class="ed-empty">Pour éditer, connecte le dossier <b>export</b> via la pastille en haut à droite.<br>Les modifications sont enregistrées dans <code>data/metadata.json</code>.</div>';
    return;
  }
  const groups=projectGroups();
  const ids=allFolderIds().sort((a,b)=>{
    const ca=a.startsWith("f_"), cb=b.startsWith("f_");
    if(ca!==cb)return ca?-1:1;                 // dossiers custom en premier
    if(ca)return a<b?1:-1;                      // custom : id ~ timestamp, plus récent d'abord
    return folderName(a).localeCompare(folderName(b));  // projets : alphabétique
  });
  ids.forEach(id=>{
    const sess=(groups[id]||[]).slice().sort((a,b)=>b.mtime-a.mtime);
    const custom=id.startsWith("f_");
    const card=document.createElement("div"); card.className="ed-folder"; card.dataset.folder=id;
    const head=document.createElement("div"); head.className="ed-fhead";
    const nameIn=document.createElement("input"); nameIn.className="ed-fname"; nameIn.value=folderName(id);
    nameIn.onchange=async()=>{const v=nameIn.value.trim(); if(!v){nameIn.value=folderName(id);return;}
      META.folders[id]=Object.assign(META.folders[id]||{},{name:v}); if(await saveMeta())buildDashboard();};
    head.appendChild(nameIn);
    const cnt=document.createElement("span"); cnt.className="ed-fcount"; cnt.textContent=sess.length+" sess"; head.appendChild(cnt);
    if(sess.length===0){                          // suppression permise seulement si le dossier est vide
      const del=document.createElement("button"); del.className="ed-del"; del.textContent="✕";
      del.title=custom?"Supprimer le dossier":"Retirer ce dossier vide de la liste";
      del.onclick=async()=>{
        if(custom)delete META.folders[id];
        else META.folders[id]=Object.assign(META.folders[id]||{},{deleted:true});
        if(await saveMeta()){buildEdition();buildDashboard();}
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
      const ph=document.createElement("option"); ph.value=""; ph.textContent="⤳ Déplacer…"; ph.disabled=true; ph.selected=true; mv.appendChild(ph);
      allFolderIds().filter(fid=>fid!==id).sort((a,b)=>folderName(a).localeCompare(folderName(b))).forEach(fid=>{
        const op=document.createElement("option"); op.value=fid; op.textContent=folderName(fid); mv.appendChild(op);
      });
      mv.onmousedown=e=>e.stopPropagation();
      mv.onchange=()=>{ if(mv.value)moveSession(m.sid,mv.value); };
      row.appendChild(mv);
      const ren=document.createElement("button"); ren.className="ed-ren"; ren.textContent="✎"; ren.title="Renommer la session";
      ren.onmousedown=e=>e.stopPropagation();
      ren.onclick=async()=>{const v=prompt("Nouveau titre :",effTitle(m)); if(v===null)return; const t=v.trim();
        META.sessions[m.sid]=META.sessions[m.sid]||{};
        if(t&&t!==m.title)META.sessions[m.sid].title=t; else delete META.sessions[m.sid].title;
        cleanupSession(m.sid); if(await saveMeta()){buildEdition();buildDashboard();}};
      row.appendChild(ren);
      listEl.appendChild(row);
    });
    if(!sess.length){const e=document.createElement("div"); e.className="ed-drop-hint"; e.textContent="Dépose des sessions ici"; listEl.appendChild(e);}
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
  META.folders[id]={name:"Nouveau dossier"};
  if(await saveMeta()){
    buildEdition();
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
function openSession(sid){
  if(curView!=="read")setView("read");
  pendingSid=sid;
  emptyread.style.display="none";
  if(cache[sid]){mount(sid);return;}
  viewer.innerHTML='<div class="gridrow"><div class="col-read" style="grid-column:2;color:var(--muted);padding:40px 0">Chargement…</div></div>';
  const s=document.createElement("script"); s.src="sessions/"+sid+".js";
  s.onerror=()=>{viewer.innerHTML='<div class="gridrow"><div class="col-read" style="grid-column:2;color:var(--accent-d);padding:40px 0">Erreur de chargement de la session.</div></div>';};
  document.body.appendChild(s);
}

function mount(sid){
  curSid=sid;
  const p=cache[sid]; if(!p)return;
  emptyread.style.display="none";
  viewer.innerHTML=p.html;
  // La sidebar suit la session ouverte : re-scope si on arrive d'un autre dossier (ex. résultat de recherche).
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
  if(afterMountAid){const a=afterMountAid;afterMountAid=null;setTimeout(()=>flashAnn(a),60);}
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
bar.innerHTML=COLORS.map(c=>'<span class="sw" data-color="'+c+'" style="background:'+SWVAR[c]+'" title="Surligner"></span>').join("")+
  '<span class="sep"></span><button class="cmt" data-act="cmt">✎ Commenter</button>';
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

/* ───── Gouttière + récap ───── */
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
  if(!noted.length){list.innerHTML='<div class="empty">Aucun commentaire dans cette session.<br>Sélectionne un passage puis « Commenter ».</div>';return;}
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
  if(!hits.length){list.innerHTML='<div class="sr-none">Aucun résultat pour<br>« '+esc(q)+' »</div>';return;}
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
/* Sidebar réglable : largeur persistée en localStorage (préférence UI pure, pas sur disque) */
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

/* ═════════ Hero welcome : ruban-mémoire WebGL (zéro dépendance) ═════════
   Gating perf : la boucle rAF ne tourne que quand la vue welcome est visible,
   pilotée par setView via window.__heroSetActive. */
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

/* ── mini algèbre mat4 (column-major, style gl-matrix) ── */
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

/* ── géométrie du ruban (régénérée par frame pour la rotation) ── */
const NSEG=300, HW=0.60, SPAN=26.0, TWIST=1.4;
const bPos=gl.createBuffer(), bNor=gl.createBuffer(), bS=gl.createBuffer(), bV=gl.createBuffer();
const posA=new Float32Array((NSEG+1)*6), norA=new Float32Array((NSEG+1)*6),
      sA=new Float32Array((NSEG+1)*2), vA=new Float32Array((NSEG+1)*2);
// positions + tailles + face (±1) : nœuds sur les DEUX faces du ruban, espacés pour les étiquettes
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
  // forme FIGÉE (statique) : les nœuds restent persistants, aucun churn à l'ondulation
  const env=Math.sin(Math.PI*Math.min(1,Math.max(0,s)));
  out[0]=(s-0.5)*SPAN;
  out[1]=env*(1.02*Math.sin(s*4.2+0.6) + 0.30*Math.sin(s*9.0+0.9));
  out[2]=env*(0.40*Math.sin(s*3.0+1.1));
}
const _a=[0,0,0],_b=[0,0,0];
// repère local (point de l'épine + largeur W + normale N) — épine figée, W/N tournent avec le temps
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
// anneau ping qui jaillit du nœud trouvé (pingT 0→1 = jeune→dissipé)
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
const DRAW_DUR=2.4;                                   // tracé G→D (assez lent pour voir les nœuds un par un)
function easeOutCubic(x){return 1-Math.pow(1-x,3);}

function render(elapsed,sig){
  if(!projM)return;
  const progress=Math.min(1,elapsed/DRAW_DUR);
  const active = sig?sig.activeNode:-1, inten = sig?sig.intensity:0;
  gl.clearColor(0,0,0,0);
  gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  const view=lookAt(CAM,[0,0,0],[0,1,0]);
  const mvp=mul(projM,view);
  curMVP=mvp;                                         // partagé avec la projection des étiquettes

  // passe 1 : ruban, révélé jusqu'au front
  gl.useProgram(prog);
  gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.disable(gl.CULL_FACE);
  buildRibbon(elapsed);
  gl.uniformMatrix4fv(loc.uMVP,false,mvp);
  gl.uniform3fv(loc.uCam,CAM);
  gl.uniform1f(loc.uScan, active>=0 ? NODES[active] : -1);
  const pairs=Math.max(1,Math.floor(NSEG*progress));
  gl.drawArrays(gl.TRIANGLE_STRIP,0,(pairs+1)*2);

  // passe 2 : nœuds de commit sur les 2 faces (germent un par un, orbitent, persistent)
  gl.useProgram(ptProg);
  gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
  buildTicks(progress,active,inten,elapsed);
  gl.uniformMatrix4fv(ploc.uMVP,false,mvp);
  gl.drawArrays(gl.TRIANGLES,0,NODES.length*VPM);

  // passe 3 : ping radar sur le nœud trouvé (transparent, n'écrit pas la profondeur)
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

/* signature : requêtes tapées, chaque requête illumine son nœud */
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
      pingT=((ct-1.25)%0.9)/0.9; } }                                     // pings radar répétés
  else { const er=Math.max(0,1-(ct-3.1)/0.5); text=Q.q.slice(0,Math.ceil(Q.q.length*er)); }
  return {visible:true,text,activeNode,intensity,pingT};
}
const sbar=document.getElementById("sbar"), sq=document.getElementById("sq");
function updateSearchBar(sig){ sbar.style.opacity=sig.visible?"1":"0"; sq.textContent=sig.text; }

/* étiquettes de commit : hash tamponné à la germination, message tapé lettre à lettre.
   Les requêtes de la barre matchent ces messages → la scène raconte une seule histoire. */
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
  const pxu=canvas.clientWidth/SPAN;                 // ≈ pixels par unité monde
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
    const off=14+NODE_R*NSIZE[i]*pxu;                // posé au-delà de la perle, dans l'axe de la tige
    L.root.style.left=Math.round(_pt[0]+dx*off)+"px";
    L.root.style.top =Math.round(_pt[1]+dy*off)+"px";
    L.h.textContent=LABELS[i].h;
    const chars=RM?99:Math.max(0,Math.floor((elapsed-s*DRAW_DUR)*TYPE_CPS));
    L.m.textContent=LABELS[i].m.slice(0,chars);
    // face avant = lisible, face arrière = discret (suit la rotation de la bande)
    const facing=Math.max(0,Math.min(1,_fN[2]*f*1.6+0.55));
    const hit=sig&&sig.activeNode===i&&sig.intensity>0.3;
    L.root.style.opacity=((hit?1:0.30+0.55*facing)*reveal).toFixed(3);
    L.root.classList.toggle("hit",hit);
  }
}

/* titre tapé lettre par lettre, après le tracé de la bande */
function typeTitle(){
  let i=0;
  (function step(){
    titleEl.textContent=WORD.slice(0,i);
    if(i<WORD.length){ i++; typeTimer=setTimeout(step,95); }
    else titleEl.classList.add("typed");
  })();
}

/* ── cycle de vie : démarrage/arrêt pilotés par la visibilité de la vue ── */
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
function staticFrame(){                              // reduced-motion : scène finale figée
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
    """Écrit sessions/<sid>.js : enregistre le HTML rendu via __loadSession (chargé au clic)."""
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
    """Écrit searchindex.js : window.__SEARCH = {sid: "texte brut minuscule"}."""
    js = "window.__SEARCH=" + json.dumps(index, ensure_ascii=False) + ";\n"
    with open(os.path.join(out_dir, "searchindex.js"), "w", encoding="utf-8") as f:
        f.write(js)


# Favicon : marque ruban ondulé en SVG data URI (vecteur, net de 16 à 512 px)
FAVICON_URI = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23F6EDDC'/%3E%3Cpath d='M4 16 C9 9, 14 23, 20 16 C23 12.5, 25 13.5, 27 15' stroke='%238E9C63' stroke-width='2.5' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='10' cy='8' r='3.2' fill='%23A4AC86' stroke='%236E7A4B' stroke-width='1.6'/%3E%3Ccircle cx='21' cy='24' r='3.2' fill='%23A4AC86' stroke='%236E7A4B' stroke-width='1.6'/%3E%3C/svg%3E"


def build_index(out_dir, manifest):
    out = INDEX_TEMPLATE
    out = out.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
    out = out.replace("__COUNT__", str(len(manifest)))
    out = out.replace("__FAVICON__", FAVICON_URI)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)


def serve(out_dir, port=8137):
    # Sert export/ sur localhost : la File System Access API exige un secure context
    # (indisponible en file://). Serveur de fichiers statiques pur, zéro logique métier.
    import http.server
    os.chdir(out_dir)
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
    url = f"http://localhost:{port}/index.html"
    print(f"\n  ▶ Memorium servi sur {url}   (Ctrl+C pour arrêter)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  ✓ Serveur arrêté.")


def main():
    # Console Windows = cp1252 par défaut : force UTF-8 pour les caractères accentués / symboles.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Arguments : "serve" (sert sur localhost) et/ou un dossier de sortie
    serve_mode = "serve" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "serve"]
    out_dir = clean_input(args[0]) if args else os.path.join(os.getcwd(), "export")
    os.makedirs(os.path.join(out_dir, "sessions"), exist_ok=True)

    paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    # Exclut les sessions automatiques du memory-observer (bruit : 1 prompt, non conversationnel)
    paths = [p for p in paths if "observer-sessions" not in os.path.dirname(p)]
    paths.sort(key=os.path.getmtime, reverse=True)
    print(f"\n  {len(paths)} sessions trouvées. Parsing…\n")

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
            "sid": sid, "project": proj, "title": data["title"],
            "prompts": inner["count"], "mtime": int(mtime),
            "date": datetime.datetime.fromtimestamp(mtime).strftime("%d %b %Y"),
        })
        ok += 1
        if ok % 25 == 0:
            print(f"  … {ok} sessions rendues")

    write_search_index(out_dir, search_index)
    build_index(out_dir, manifest)
    index = os.path.join(out_dir, "index.html")
    print(f"\n  ✓ {ok} sessions exportées ({skipped} vides/ignorées)")
    print(f"  ✓ Index : {index}")
    if serve_mode:
        serve(out_dir)
    else:
        try:
            webbrowser.open("file:///" + index.replace("\\", "/"))
            print("  → Ouverture dans le navigateur…")
        except Exception:
            pass


if __name__ == "__main__":
    main()
