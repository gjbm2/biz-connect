"""notion_md — Markdown <-> Notion blocks, for `notion push/pull` (the tree sync).

Pure functions, stdlib only, no API calls. The design rule that makes the sync safe:

  A block's SIGNATURE is its Markdown rendering.

Two blocks are "the same" iff they render to the same Markdown. The sync diffs a page's
remote blocks against the local file's blocks by signature, so a Notion block whose
Markdown didn't change is never rewritten — its comments, mentions, colours and block id
survive. The one invariant the converter must hold is idempotence:

  render(parse(render(x))) == render(x)

Blocks Markdown can't express (child pages/databases, toggles, embeds, synced blocks,
columns, PDFs/files, ...) are UNMANAGED: never created, changed or deleted by a push,
excluded from signatures, and shown on pull as an HTML comment (`<!-- notion: ... -->`)
that the parser ignores.

Supported Markdown: `#`..`###` headings (deeper -> ###), paragraphs (soft-wrapped lines join
with a space; `<br>` = line break), `-`/`*`/`+`, `1.` and `- [ ]` lists (nested by indent),
`>` quotes, GitHub alerts (`> [!NOTE]`) as callouts, fenced code, `---` dividers, pipe
tables, a line holding just `![caption](src)` as an image, and inline **bold**, *italic*,
~~strike~~, `code` and [links](url).
"""
from __future__ import annotations

import json
import re
import string

# ------------------------------------------------------------------ constants
TEXT_TYPES = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
              "numbered_list_item", "to_do", "quote", "callout")
LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do")
MANAGED = set(TEXT_TYPES) | {"code", "divider", "table", "image"}

ALERTS = {"NOTE": "ℹ️", "TIP": "💡", "IMPORTANT": "❗", "WARNING": "⚠️", "CAUTION": "🛑"}
ALERT_OF_ICON = {v: k for k, v in ALERTS.items()}

NOTION_LANGS = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#", "css",
    "dart", "diff", "docker", "elixir", "elm", "erlang", "flow", "fortran", "f#", "gherkin",
    "glsl", "go", "graphql", "groovy", "haskell", "html", "java", "javascript", "json", "julia",
    "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile", "markdown", "markup",
    "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl", "php", "plain text",
    "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby", "rust", "sass", "scala",
    "scheme", "scss", "shell", "sql", "swift", "typescript", "vb.net", "verilog", "vhdl",
    "visual basic", "webassembly", "xml", "yaml", "java/c/c++/c#"}
LANG_ALIAS = {"sh": "shell", "zsh": "shell", "console": "shell", "js": "javascript",
              "jsx": "javascript", "ts": "typescript", "tsx": "typescript", "py": "python",
              "yml": "yaml", "ps1": "powershell", "pwsh": "powershell", "cpp": "c++",
              "cs": "c#", "csharp": "c#", "md": "markdown", "dockerfile": "docker",
              "jsonc": "json", "text": "plain text", "txt": "plain text", "plain": "plain text",
              "": "plain text"}

ESCAPABLE = set(string.punctuation)
ANN_KEYS = ("bold", "italic", "strikethrough", "code")

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^`\s]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$")
_TODO = re.compile(r"^\[([ xX])\]\s+(.*)$")
_IMAGE_LINE = re.compile(r'^!\[(.*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)$')
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_ALERT = re.compile(r"^\[!(\w+)\]\s*(.*)$")


# ============================================================= rich text model
def seg(content, link=None, **ann):
    """One rich-text segment in API create-shape."""
    t = {"content": content}
    if link:
        t["link"] = {"url": link}
    a = {k: True for k in ANN_KEYS if ann.get(k)}
    out = {"type": "text", "text": t}
    if a:
        out["annotations"] = a
    return out


def _seg_parts(r):
    """(content, href, annotations-tuple) for a create-shape OR retrieved-shape segment."""
    if r.get("type") == "text" and isinstance(r.get("text"), dict):
        content = r["text"].get("content", "")
        link = (r["text"].get("link") or {}).get("url") or r.get("href")
    else:                                    # mention / equation: keep what Notion displays
        content = r.get("plain_text", "")
        if r.get("type") == "equation":
            content = r.get("equation", {}).get("expression", content)
        link = r.get("href")
    ann = r.get("annotations") or {}
    return content, link, tuple(bool(ann.get(k)) for k in ANN_KEYS)


def plain(rich):
    return "".join(_seg_parts(r)[0] for r in rich or [])


# ============================================================ inline: parse
def parse_inline(text):
    """Inline Markdown -> list of rich-text segments (create-shape, raw hrefs)."""
    out = []
    _inline(text, {}, None, out)
    return _merge(out)


def _flush(buf, ann, link, out):
    if buf:
        out.append(seg("".join(buf), link, **ann))
        buf.clear()


def _find_closer(s, start, delim):
    """Index of a closing `delim` at/after `start` not preceded by whitespace, else -1.
    For single `*`/`_`, runs of the doubled delimiter are skipped (they belong to bold)."""
    j, n, d = start, len(s), len(delim)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":                         # don't look for closers inside a code span
            k = _code_end(s, j)
            if k > 0:
                j = k
                continue
        if s.startswith(delim, j) and j > start and not s[j - 1].isspace():
            if d == 1 and s.startswith(delim * 2, j):
                j += 2
                continue
            if delim == "_" and j + 1 < n and s[j + 1].isalnum():
                j += 1                       # intraword underscore is literal
                continue
            return j
        if d == 1 and s.startswith(delim * 2, j):
            j += 2
            continue
        j += 1
    return -1


def _code_end(s, i):
    """If s[i] opens a code span, return the index just past its closer, else -1."""
    n = len(s)
    k = i
    while k < n and s[k] == "`":
        k += 1
    run = k - i
    j = k
    while j < n:
        if s[j] == "`":
            m = j
            while m < n and s[m] == "`":
                m += 1
            if m - j == run:
                return m
            j = m
        else:
            j += 1
    return -1


def _link_at(s, i):
    """Parse `[text](url)` at s[i]=='['. Returns (text, url, end) or None."""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if j >= n or j + 1 >= n or s[j + 1] != "(":
        return None
    text = s[i + 1:j]
    k, depth = j + 2, 1
    while k < n:
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    if k >= n:
        return None
    target = s[j + 2:k].strip()
    m = re.match(r'^<?([^>\s]*)>?(?:\s+"[^"]*")?$', target)
    url = m.group(1) if m else target.split()[0] if target else ""
    return text, url, k + 1


def _inline(s, ann, link, out):
    buf, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in ESCAPABLE:
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "<" and s.startswith("<br>", i):
            buf.append("\n")
            i += 4
            continue
        if c == "`":
            end = _code_end(s, i)
            if end > 0:
                k = i
                while s[k] == "`":
                    k += 1
                run = k - i
                body = s[k:end - run]
                if len(body) >= 2 and body[0] == " " and body[-1] == " " and body.strip():
                    body = body[1:-1]
                _flush(buf, ann, link, out)
                out.append(seg(body, link, **{**ann, "code": True}))
                i = end
                continue
        if c == "[" or (c == "!" and s.startswith("![", i)):
            at = i + (1 if c == "!" else 0)
            lk = _link_at(s, at)
            if lk and lk[1]:
                _flush(buf, ann, link, out)
                _inline(lk[0], ann, lk[1], out)
                i = lk[2]
                continue
        if c == "<":
            m = re.match(r"<((?:https?|mailto):[^>\s]+)>", s[i:])
            if m:
                _flush(buf, ann, link, out)
                out.append(seg(m.group(1), m.group(1), **ann))
                i += m.end()
                continue
        for delim, key in (("***", "bold+italic"), ("___", "bold+italic"), ("**", "bold"), ("__", "bold"),
                           ("~~", "strikethrough"), ("*", "italic"), ("_", "italic")):
            if not s.startswith(delim, i):
                continue
            d = len(delim)
            if i + d >= n or s[i + d].isspace():
                continue
            if delim in ("_", "__", "___") and i > 0 and s[i - 1].isalnum():
                continue                     # intraword underscore: literal
            j = _find_closer(s, i + d, delim)
            if j < 0:
                continue
            _flush(buf, ann, link, out)
            more = {"bold": True, "italic": True} if key == "bold+italic" else {key: True}
            _inline(s[i + d:j], {**ann, **more}, link, out)
            i = j + d
            break
        else:
            buf.append(c)
            i += 1
    _flush(buf, ann, link, out)


def _edge_ws(rich):
    """Styling on edge whitespace can't survive Markdown (`**a **` isn't bold), so a styled
    segment's leading/trailing spaces are made plain. Code keeps its spaces (they're content)."""
    for r in rich:
        c, l, a = _seg_parts(r)
        if not c or a[3] or not any(a[:3]) or c.strip() == c:
            yield c, l, a
            continue
        core = c.strip()
        lead, trail = c[:len(c) - len(c.lstrip())], c[len(c.rstrip()):]
        plain_a = (False, False, False, False)
        if lead:
            yield lead, l, plain_a
        if core:
            yield core, l, a
        if trail:
            yield trail, l, plain_a


def _merge(rich):
    out = []
    for c, l, a in _edge_ws(rich):
        if not c:
            continue
        if out:
            pc, pl, pa = _seg_parts(out[-1])
            if pl == l and pa == a:
                out[-1] = seg(pc + c, l, **dict(zip(ANN_KEYS, a)))
                continue
        out.append(seg(c, l, **dict(zip(ANN_KEYS, a))))
    return out


# =========================================================== inline: render
def _escape(text):
    out, n = [], len(text)
    for i, ch in enumerate(text):
        if ch in "\\*`":
            out.append("\\" + ch)
        elif ch == "_":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            out.append("_" if (prev.isalnum() and nxt.isalnum()) else "\\_")
        elif ch == "~" and (text[i + 1:i + 2] == "~" or text[i - 1:i] == "~"):
            out.append("\\~")
        elif ch == "[" and "](" in text[i:]:
            out.append("\\[")
        elif ch == "<" and re.match(r"<(br>|https?:|mailto:|!--)", text[i:]):
            out.append("\\<")
        elif ch == "\n":
            out.append("<br>")
        else:
            out.append(ch)
    return "".join(out)


def _wrap(text, marker):
    """Wrap `text` in `marker`, keeping edge whitespace OUTSIDE (a closer can't follow a space)."""
    core = text.strip(" ")
    if not core:
        return text
    lead = text[:len(text) - len(text.lstrip(" "))]
    trail = text[len(text.rstrip(" ")):]
    return lead + marker + core + marker + trail


def _code_span(content):
    run = max((len(m) for m in re.findall(r"`+", content)), default=0) + 1
    ticks = "`" * run
    edge_sp = content[:1] == " " and content[-1:] == " " and content.strip(" ")     # a re-parse eats one each side
    pad = " " if (content.startswith("`") or content.endswith("`") or run > 1 or edge_sp) else ""
    return ticks + pad + content + pad + ticks


def render_inline(rich, href=None):
    """Rich text -> inline Markdown. `href(url)` maps a link target (or returns None to drop
    the link and keep its text); default keeps it."""
    segs = []
    for r in _merge(rich or []):
        c, l, a = _seg_parts(r)
        if l and href is not None:
            l = href(l)
        segs.append((c, l, dict(zip(ANN_KEYS, a))))
    out, i = [], 0
    while i < len(segs):
        link = segs[i][1]
        j = i
        while j < len(segs) and segs[j][1] == link:
            j += 1
        run = [(c, {k for k, v in a.items() if v}) for c, _l, a in segs[i:j]]
        before = segs[i - 1][0][-1:] if i else ""
        after = segs[j][0][:1] if j < len(segs) else ""
        want = [(c, tuple(k in s for k in ANN_KEYS)) for c, s in _merged_run(run)]
        # `*italic*`, as people write it, unless that would be ambiguous; then `_italic_`; then
        # each segment wrapped on its own (`_Q3_**2026**`, where `*Q3***2026**` misparses)
        for body in (f(run, before, after, star) for f, star in (
                (_render_run, True), (_render_run, False), (_render_each, False), (_render_each, True))):
            if [(c, a) for c, _l, a in (_seg_parts(r) for r in parse_inline(body))] == want:
                break
        else:
            body = _render_run(run, before, after)              # nothing re-parses exactly
        out.append("[%s](%s)" % (body, link.replace(" ", "%20").replace(")", "%29")) if link else body)
        i = j
    return "".join(out)


def _render_each(run, before="", after="", star=False):
    """Render each segment with its own markers (no shared runs). Next to a styled neighbour
    the adjacent character is a marker, not text, so it doesn't count as inside a word."""
    out = []
    for k, (c, a) in enumerate(run):
        prev = "" if k and run[k - 1][1] else run[k - 1][0][-1:] if k else before
        nxt = "" if k + 1 < len(run) and run[k + 1][1] else run[k + 1][0][:1] if k + 1 < len(run) else after
        out.append(_render_run([(c, a)], prev, nxt, star))
    return "".join(out)


def _merged_run(run):
    out = []
    for c, s in run:
        if not c:
            continue
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0] + c, s)
        else:
            out.append((c, s))
    return out


def _render_run(segs, before="", after="", star=False):
    """Render styled segments, wrapping each MAXIMAL run that shares a style once (outermost
    first), so adjacent same-style segments never emit colliding markers like `*a**b*`.
    Italic uses `_` (unambiguous next to `**`) unless the run sits inside a word; with
    `star`, it uses `*` (the caller re-parses to check that stayed unambiguous)."""
    out, i, n = [], 0, len(segs)
    while i < n:
        c, a = segs[i]
        for key, marker in (("strikethrough", "~~"), ("italic", None), ("bold", "**")):
            if key not in a:
                continue
            j = i
            while j < n and key in segs[j][1]:
                j += 1
            prev = segs[i - 1][0][-1:] if i else before
            nxt = segs[j][0][:1] if j < n else after
            inner = _render_run([(cc, aa - {key}) for cc, aa in segs[i:j]], prev, nxt, star)
            if marker is None:
                marker = "*" if (star or prev.isalnum() or nxt.isalnum()) else "_"
            out.append(_wrap(inner, marker))
            i = j
            break
        else:
            out.append(_code_span(c.replace("\n", " ")) if "code" in a else _escape(c))
            i += 1
    return "".join(out)


def _escape_leading(line):
    """Stop paragraph text that LOOKS like a block marker from re-parsing as one."""
    if re.match(r"^(#{1,6}\s|>|[-+*]\s|\d{1,9}[.)]\s|```|~~~|\||!\[|<!--|\[[ xX]\]\s)", line) or _HR.match(line):
        m = re.match(r"^(\d{1,9})([.)])(.*)$", line, re.S)
        if m:
            return m.group(1) + "\\" + m.group(2) + m.group(3)
        return "\\" + line
    return line


# ============================================================ blocks: parse
def _text_block(btype, rich, **extra):
    body = {"rich_text": rich}
    body.update(extra)
    return {"type": btype, btype: body}


def norm_lang(lang):
    lang = (lang or "").strip().lower()
    lang = LANG_ALIAS.get(lang, lang)
    return lang if lang in NOTION_LANGS else "plain text"


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _expand(line):
    return line.replace("\t", "    ")


def _starts_block(line):
    s = line.strip()
    return bool(not s or _FENCE.match(line) or _HEADING.match(s) or _HR.match(s)
                or s.startswith(">") or _LIST.match(line) or s.startswith("<!--")
                or _IMAGE_LINE.match(s))


def parse_blocks(md):
    """Markdown body -> list of blocks (create-shape; children under block[type]['children']).
    Links keep their RAW targets; local images carry `_src` for the caller to upload."""
    lines = [_expand(l) for l in (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _parse(lines)


def _parse(lines):
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("<!--"):
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            fence, lang = m.group(2), m.group(3)
            body, i = [], i + 1
            base = len(m.group(1))
            while i < n:
                if re.match(r"^\s*" + re.escape(fence[0]) + "{%d,}\\s*$" % len(fence), lines[i]):
                    i += 1
                    break
                l = lines[i]
                body.append(l[min(base, _indent(l)):])
                i += 1
            code = "\n".join(body)
            out.append({"type": "code", "code": {"rich_text": [seg(code)] if code else [],
                                                 "language": norm_lang(lang)}})
            continue
        m = _HEADING.match(s)
        if m:
            level = min(len(m.group(1)), 3)
            out.append(_text_block("heading_%d" % level, parse_inline(m.group(2))))
            i += 1
            continue
        if _HR.match(s):
            out.append({"type": "divider", "divider": {}})
            i += 1
            continue
        if s.startswith(">"):
            q = []
            while i < n and lines[i].strip().startswith(">"):
                t = lines[i].strip()[1:]
                q.append(t[1:] if t.startswith(" ") else t)
                i += 1
            out.append(_quote(q))
            continue
        if "|" in s and i + 1 < n and "|" in lines[i + 1] and _TABLE_SEP.match(lines[i + 1]):
            rows = [_cells(s)]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append(_table(rows))
            continue
        m = _IMAGE_LINE.match(s)
        if m:
            out.append(_image(m.group(2), parse_inline(m.group(1))))
            i += 1
            continue
        m = _LIST.match(line)
        if m:
            blk, i = _list_item(lines, i)
            out.append(blk)
            continue
        para = []
        while i < n and lines[i].strip() and (not para or not _starts_block(lines[i])):
            para.append(lines[i])
            i += 1
        out.append(_text_block("paragraph", parse_inline(_join_para(para))))
    return out


def _join_para(lines):
    """Soft-wrapped lines join with a space; a hard break (two trailing spaces or a trailing
    backslash) becomes a line break."""
    parts = []
    for k, l in enumerate(lines):
        last = k == len(lines) - 1
        if not last and l.endswith("  "):
            parts.append(l.strip() + "<br>")
        elif not last and l.rstrip().endswith("\\") and not l.rstrip().endswith("\\\\"):
            parts.append(l.strip()[:-1] + "<br>")
        else:
            parts.append(l.strip() + ("" if last else " "))
    return "".join(parts).replace("<br> ", "<br>")


def _quote(qlines):
    kind, first = None, 0
    if qlines:
        m = _ALERT.match(qlines[0].strip())
        if m and m.group(1).upper() in ALERTS:
            kind = m.group(1).upper()
            rest = m.group(2)
            if rest:
                qlines = [rest] + qlines[1:]
            else:
                first = 1
    paras, cur = [], []
    for l in qlines[first:]:
        if l.strip():
            cur.append(l)
        elif cur:
            paras.append(_join_para(cur))
            cur = []
    if cur:
        paras.append(_join_para(cur))
    rich = parse_inline("<br>".join(paras))
    if kind:
        return _text_block("callout", rich, icon={"type": "emoji", "emoji": ALERTS[kind]})
    return _text_block("quote", rich)


def _cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, i = [], [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")                  # GFM: `\|` is a literal pipe, even inside code spans
            i += 2
            continue
        if s[i] == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(s[i])
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _table(rows):
    width = max(1, len(rows[0]))
    children = []
    for r in rows:
        r = (r + [""] * width)[:width]
        children.append({"type": "table_row", "table_row": {"cells": [parse_inline(c) for c in r]}})
    return {"type": "table", "table": {"table_width": width, "has_column_header": True,
                                       "has_row_header": False, "children": children}}


def _image(src, caption):
    if re.match(r"^https?://", src):
        return {"type": "image", "image": {"type": "external", "external": {"url": src},
                                           "caption": caption}}
    return {"type": "image", "image": {"_src": src, "caption": caption}}


def _list_item(lines, i):
    m = _LIST.match(lines[i])
    ind, marker, gap, text = len(m.group(1)), m.group(2), m.group(3), m.group(4)
    content_ind = ind + len(marker) + min(len(gap), 4)
    i += 1
    n = len(lines)
    # lazy/indented continuation of the item's first paragraph
    cont = [text]
    while i < n and lines[i].strip() and not _LIST.match(lines[i]) and not (
            _indent(lines[i]) <= ind and _starts_block(lines[i])):
        if _indent(lines[i]) >= content_ind and _starts_block(lines[i][content_ind:]):
            break
        cont.append(lines[i])
        i += 1
    # nested region: deeper-indented lines (blank lines allowed inside)
    region = []
    while i < n:
        l = lines[i]
        if l.strip():
            if _indent(l) > ind:
                region.append(l)
                i += 1
                continue
            break
        k = i
        while k < n and not lines[k].strip():
            k += 1
        if k < n and _indent(lines[k]) > ind:
            region.extend(lines[i:k])
            i = k
            continue
        break
    if region:
        dd = min([_indent(l) for l in region if l.strip()] + [content_ind])
        region = [l[dd:] if l.strip() else "" for l in region]
    body = _join_para(cont)
    if marker[0].isdigit():
        btype, extra = "numbered_list_item", {}
    else:
        tm = _TODO.match(body)
        if tm:
            btype, extra, body = "to_do", {"checked": tm.group(1) in "xX"}, tm.group(2)
        else:
            btype, extra = "bulleted_list_item", {}
    blk = _text_block(btype, parse_inline(body), **extra)
    kids = _parse(region) if region else []
    if kids:
        blk[btype]["children"] = kids
    return blk, i


# =========================================================== blocks: render
def children_of(block):
    """Children in either shape: fetched (`_children`) or create-shape (`[type].children`)."""
    if "_children" in block:
        return block["_children"]
    body = block.get(block.get("type"), {})
    return body.get("children", []) if isinstance(body, dict) else []


def is_managed(block):
    """Does the Markdown own this block? (Empty paragraphs are left alone: Notion spacing.)"""
    t = block.get("type")
    if t not in MANAGED:
        return False
    if t == "paragraph" and not plain(block["paragraph"].get("rich_text")) and not children_of(block):
        return False
    if t in ("heading_1", "heading_2", "heading_3") and block[t].get("is_toggleable"):
        return False
    return True


def _marker(block):
    t = block.get("type", "?")
    body = block.get(t, {}) if isinstance(block.get(t), dict) else {}
    title = body.get("title") or plain(body.get("rich_text") or body.get("caption") or [])
    title = title.replace("--", "—").replace("\n", " ")[:80]
    return "<!-- notion:%s%s -->" % (t, (" " + json.dumps(title, ensure_ascii=False)) if title else "")


def image_src(block, media=None):
    body = block.get("image", {})
    if "_src" in body:
        src = body["_src"]
    elif body.get("type") == "external":
        src = body.get("external", {}).get("url", "")
    else:
        src = (body.get("file") or {}).get("url", "")
    return media(src, block) if media else src


def render_blocks(blocks, href=None, media=None, markers=False, indent=0):
    """Blocks -> Markdown. `markers=True` (pull output) keeps a comment line for each
    unmanaged block; signatures use markers=False so unmanaged blocks never count."""
    pieces = []                              # (text, is_list_type)
    for b in blocks:
        text = _render_block(b, href, media, markers)
        if text is None:
            continue
        pieces.append((text, b.get("type")))
    out = []
    for k, (text, btype) in enumerate(pieces):
        if k:
            prev = pieces[k - 1][1]
            tight = btype in LIST_TYPES and prev in LIST_TYPES and (
                btype == prev or {btype, prev} <= {"bulleted_list_item", "to_do"})
            out.append("\n" if tight else "\n\n")
        out.append(text)
    s = "".join(out)
    if indent:
        s = "\n".join((" " * indent + l) if l else l for l in s.split("\n"))
    return s


def _render_block(b, href, media, markers):
    t = b.get("type")
    if not is_managed(b):
        if markers and not (t == "paragraph"):
            return _marker(b)
        return None
    body = b[t]
    kids = children_of(b)

    def ri(rich):                            # edge whitespace can't survive a re-parse
        return render_inline(rich, href).strip()

    if t == "paragraph":
        s = _escape_leading(ri(body.get("rich_text")))
    elif t.startswith("heading_"):
        s = "#" * int(t[-1]) + " " + ri(body.get("rich_text"))
    elif t in LIST_TYPES:
        if t == "numbered_list_item":
            lead = "1. "
        elif t == "to_do":
            lead = "- [x] " if body.get("checked") else "- [ ] "
        else:
            lead = "- "
        s = lead + _escape_leading(ri(body.get("rich_text")))
        if kids:
            inner = render_blocks(kids, href, media, markers, indent=3 if t == "numbered_list_item" else 2)
            if inner.strip():
                first = next((c.get("type") for c in kids
                              if _render_block(c, href, media, markers) is not None), None)
                # a nested non-list block needs a blank line, or it re-parses as the item's text
                return s + ("\n" if first in LIST_TYPES else "\n\n") + inner
        return s
    elif t == "quote":
        q = ri(body.get("rich_text"))
        s = "> " + ("\\" + q if q.startswith("[!") else q)
    elif t == "callout":
        icon = (body.get("icon") or {}).get("emoji", "")
        s = "> [!%s]\n> %s" % (ALERT_OF_ICON.get(icon, "NOTE"), ri(body.get("rich_text")))
    elif t == "code":
        code = plain(body.get("rich_text"))
        lang = body.get("language") or "plain text"
        lang = "" if lang == "plain text" else lang
        fence = "```" if "```" not in code else "~~~~"
        return "%s%s\n%s\n%s" % (fence, lang, code, fence)
    elif t == "divider":
        return "---"
    elif t == "table":
        rows = [r for r in kids if r.get("type") == "table_row"]
        if not rows:
            return None
        width = body.get("table_width") or max(len(r["table_row"].get("cells", [])) for r in rows)
        lines = []
        for k, r in enumerate(rows):
            cells = (list(r["table_row"].get("cells", [])) + [[]] * width)[:width]
            lines.append("| " + " | ".join(render_inline(c, href).replace("|", "\\|") for c in cells) + " |")
            if k == 0:
                lines.append("|" + "|".join(["---"] * width) + "|")
        return "\n".join(lines)
    elif t == "image":
        cap = render_inline(body.get("caption"), href)
        return "![%s](%s)" % (cap, image_src(b, media).replace(" ", "%20"))
    else:
        return None
    if kids and t not in LIST_TYPES:         # Notion "indented" children: flattened on render
        inner = render_blocks(kids, href, media, markers)
        if inner.strip():
            s += "\n\n" + inner
    return s


def signature(block, href=None, media=None):
    return render_blocks([block], href, media, markers=False)


# ======================================================= page-level helpers
_H1 = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$")
_FM = re.compile(r"^---[ \t]*\n(.*?\n)?---[ \t]*(\n|$)", re.S)


def split_front_matter(text):
    """(front_matter_text or None, rest). Only a block at the very top counts."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):                # a UTF-8 BOM (PowerShell 5.1 writes one)
        text = text[1:]
    m = _FM.match(text)
    if not m:
        return None, text
    return (m.group(1) or ""), text[m.end():]


def split_title(body):
    """(title or None, rest): a first non-blank line `# Title` is the page title."""
    lines = body.split("\n")
    for k, l in enumerate(lines):
        if not l.strip():
            continue
        m = _H1.match(l)
        if m:
            return plain(parse_inline(m.group(1))), "\n".join(lines[k + 1:])
        return None, body
    return None, body
