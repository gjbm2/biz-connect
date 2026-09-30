"""gslides — read and edit Google Slides decks via the central Google credentials.

Access is the same as the other Google connectors: the service account, acting as
GOOGLE_IMPERSONATE_SUBJECT when that is set (domain-wide delegation, `drive` scope), or
as itself on decks shared with its email. <deck> is a presentation URL or id. <slide> is
a slide NUMBER as shown in the Slides UI (1-based; "27") or a slide objectId; <slides>
also takes ranges and lists ("26-28", "3,5", "all").

Read
  check    <deck>                                  access pre-flight: title, revision, size, slide count
  outline  <deck> [--slides S] [--full] [--json]    per slide: index, id, hidden, layout, title, elements
                                                    (type, geometry in inches, text, style), notes
  layouts  <deck>                                  masters and their layouts (ids, names, placeholders)
  thumb    <deck> <slides> [--out P] [--size LARGE|MEDIUM|SMALL]   slide PNG(s) for visual QA
  pdf      <deck> [--out P]                        whole deck as PDF (Drive export)

Write (each reads the deck first and sends ONE batchUpdate guarded by
writeControl.requiredRevisionId; --revision R fails unless the deck is still at R, the
revision your outline showed; --dry-run prints the requests and sends nothing)
  apply     <deck> <requests.json|->              raw batchUpdate: a list of requests or {"requests": [...]}
  build     <deck> <spec.yaml|json> [--out F]      build a slide from an inch-based spec (one atomic batch;
                                                   warns on likely text overflow)
  hide      <deck> <slides>                        skip slides in presentation mode (isSkipped)
  unhide    <deck> <slides>
  duplicate <deck> <slide> [--at N] [--id ID]      copy a slide (lands after the original unless --at)
  move      <deck> <slide> --to N                  move a slide so it becomes slide N
  insert    <deck> --at N [--layout NAME|ID] [--master ID] [--id ID]
                                                   new slide from one of the deck's own layouts
                                                   (default layout: Blank, from the neighbouring slide's master)
  notes     <deck> <slide> (--text T | --file F | --clear)   set the speaker notes
  replace   <deck> --find T --with R [--slides S] [--match-case]   replaceAllText
  create    --title T                              new, empty presentation (10 x 5.625 in; the API
                                                   cannot set another page size)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

from .. import _google

EMU_IN = 914400
EMU_PT = 12700
MIME_PDF = "application/pdf"
INSET_IN = 0.1              # Slides' default text-box padding (each side)
CELL_PAD_X, CELL_PAD_Y = 0.1, 0.1     # Slides' table-cell padding (measured)
LINE_HEIGHT = 1.2           # line height as a multiple of font size, at 100% line spacing
_LINE_HEIGHTS = {"arial": 1.2, "helvetica": 1.2, "liberation sans": 1.2, "roboto": 1.2,
                 "open sans": 1.36, "lato": 1.2, "montserrat": 1.22, "inter": 1.21, "calibri": 1.22,
                 "verdana": 1.22, "georgia": 1.14, "times new roman": 1.15}
DEFAULT_BULLET = "BULLET_DISC_CIRCLE_SQUARE"
OWN = "bc_"                 # objectIds the slide builder creates start with this marker
RETRIES = 3                 # reads retry transient errors (5xx, 429); writes never retry


class SpecError(ValueError):
    """A problem with a slide spec, a slide reference or an argument."""


# ------------------------------------------------------------------------------ args + ids
def deck_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"/presentation/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" in s or " " in s:
        raise SpecError(f"could not find a presentation id in: {s!r}")
    return s


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
        raise SpecError(f"{name} needs a value")
    return default


def flag(args, name):
    return name in args


def positional(args, value_opts=()):
    """Arguments that are neither --flags nor the value of an option in `value_opts`."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in value_opts:
            skip = True
            continue
        if a.startswith("--"):
            continue
        out.append(a)
    return out


_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-:]{4,49}$")


def check_object_id(oid, what="objectId"):
    if not isinstance(oid, str) or not _ID_RE.match(oid):
        raise SpecError(f"{what} {oid!r} is not a valid Slides objectId: 5-50 characters, "
                        f"letters, digits, _ - : (not starting with - or :)")
    return oid


def new_id(prefix="s"):
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------------------ units + colour
def emu(inches):
    return int(round(float(inches) * EMU_IN))


def _mag(d, default=0.0):
    """magnitude of a Dimension in EMU (PT dimensions converted)."""
    if not d:
        return default
    m = d.get("magnitude", 0) or 0
    return m * EMU_PT if d.get("unit") == "PT" else m


def _in(e):
    return round(e / EMU_IN, 2)


def _hex_from_rgb(rgb):
    rgb = rgb or {}
    return "#%02X%02X%02X" % tuple(int(round((rgb.get(k) or 0) * 255)) for k in ("red", "green", "blue"))


def color_hex(opaque, scheme=None):
    """'#RRGGBB' (or 'theme:ACCENT1(#RRGGBB)') from an OpaqueColor; None if unset."""
    if not opaque:
        return None
    if "rgbColor" in opaque:
        return _hex_from_rgb(opaque["rgbColor"])
    if "themeColor" in opaque:
        t = opaque["themeColor"]
        h = (scheme or {}).get(t)
        return f"theme:{t}" + (f"({h})" if h else "")
    return None


def parse_color(value):
    """Spec colour -> OpaqueColor. '#1C1917', '1C1917', '#abc' or 'theme:ACCENT1'."""
    if not isinstance(value, str):
        raise SpecError(f"colour must be a string like '#1C1917', got {value!r}")
    v = value.strip()
    if v.lower().startswith("theme:"):
        return {"themeColor": v.split(":", 1)[1].strip().upper()}
    h = v.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        raise SpecError(f"colour {value!r} is not a hex colour (#RRGGBB) or theme:NAME")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return {"rgbColor": {"red": round(r, 6), "green": round(g, 6), "blue": round(b, 6)}}


# ------------------------------------------------------------------------------ geometry
IDENTITY = (1, 0, 0, 1, 0, 0)


def _tf(t):
    """(a, b, c, d, tx, ty) of an AffineTransform. The API omits zero fields (proto3), so a
    missing scale is 0 (a flat line), not 1; only a missing transform is the identity."""
    if not t:
        return IDENTITY
    return (t.get("scaleX", 0), t.get("shearY", 0), t.get("shearX", 0), t.get("scaleY", 0),
            t.get("translateX", 0), t.get("translateY", 0))


def _compose(parent, child):
    """Absolute transform = parent x child (affine, as (a, b, c, d, tx, ty))."""
    a1, b1, c1, d1, x1, y1 = parent
    a2, b2, c2, d2, x2, y2 = child
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * x2 + c1 * y2 + x1, b1 * x2 + d1 * y2 + y1)


def element_box(el, parent=IDENTITY):
    """(x, y, w, h) in EMU of a page element's bounding box (absolute when `parent` is the
    composed transform of its groups)."""
    tf = _compose(parent, _tf(el.get("transform")))
    if "elementGroup" in el:
        boxes = [element_box(c, tf) for c in el["elementGroup"].get("children", [])]
        if not boxes:
            return (tf[4], tf[5], 0, 0)
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes)
        y1 = max(b[1] + b[3] for b in boxes)
        return (x0, y0, x1 - x0, y1 - y0)
    if "table" in el:
        t = el["table"]
        w = sum(_mag(c.get("columnWidth")) for c in t.get("tableColumns", []))
        h = sum(_mag(r.get("rowHeight")) for r in t.get("tableRows", []))
    else:
        size = el.get("size") or {}
        w, h = _mag(size.get("width")), _mag(size.get("height"))
    a, b, c, d, tx, ty = tf
    xs = [tx, tx + a * w, tx + c * h, tx + a * w + c * h]
    ys = [ty, ty + b * w, ty + d * h, ty + b * w + d * h]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def geom_str(box):
    x, y, w, h = box
    return f"x{_in(x):g} y{_in(y):g} w{_in(w):g} h{_in(h):g}"


# ------------------------------------------------------------------------------ reading text
def text_of(textobj):
    if not textobj:
        return ""
    return "".join(te["textRun"].get("content", "") for te in textobj.get("textElements", [])
                   if "textRun" in te)


def _runs(textobj):
    for te in (textobj or {}).get("textElements", []):
        if "textRun" in te:
            yield te["textRun"].get("content", ""), te["textRun"].get("style") or {}


def _style_key(st, scheme=None):
    fg = st.get("foregroundColor")
    col = color_hex(fg.get("opaqueColor"), scheme) if fg else None
    fs = st.get("fontSize")
    return (st.get("fontFamily"), (fs or {}).get("magnitude"), bool(st.get("bold")),
            bool(st.get("italic")), bool(st.get("underline")), col)


def style_tag(key):
    font, size, bold, italic, underline, col = key
    parts = [font or "?", f"{size:g}pt" if size else "?pt"]
    if bold:
        parts.append("bold")
    if italic:
        parts.append("italic")
    if underline:
        parts.append("underline")
    parts.append(col or "?colour")
    return " ".join(parts)


def text_styles(textobj, scheme=None, inherit=None):
    """[(style_key, n_chars)] most-used first, ignoring newline-only runs. `inherit` fills
    fields a run leaves unset (placeholders inherit from their layout/master)."""
    counts = {}
    for content, st in _runs(textobj):
        n = len(content.strip("\n"))
        if not n:
            continue
        if inherit:
            st = {**inherit, **{k: v for k, v in st.items() if v not in (None, {}, "")}}
        k = _style_key(st, scheme)
        counts[k] = counts.get(k, 0) + n
    return sorted(counts.items(), key=lambda kv: -kv[1])


def _alignments(textobj):
    out = []
    for te in (textobj or {}).get("textElements", []):
        pm = te.get("paragraphMarker")
        if pm:
            out.append((pm.get("style") or {}).get("alignment", "START"))
            if pm.get("bullet"):
                out[-1] += "+bullet"
    return out


def _fill_str(fill, scheme):
    if not fill:
        return None
    if fill.get("propertyState") in ("NOT_RENDERED", "INHERIT"):
        return "none" if fill["propertyState"] == "NOT_RENDERED" else "inherit"
    sf = fill.get("solidFill")
    if not sf:
        return None
    h = color_hex(sf.get("color"), scheme)
    a = sf.get("alpha", 1)
    return h + (f"@{a:.2g}" if a is not None and a < 1 else "")


def _outline_str(o, scheme):
    if not o:
        return None
    if o.get("propertyState") == "NOT_RENDERED":
        return "none"
    if o.get("propertyState") == "INHERIT":
        return "inherit"
    f = _fill_str(o.get("outlineFill"), scheme)
    w = _mag(o.get("weight"))
    return f"{f or '?'} {w / EMU_PT:g}pt" + ("" if o.get("dashStyle", "SOLID") == "SOLID" else " " + o["dashStyle"])


def _clip(s, n):
    s = s.replace("\n", " / ").replace("\x0b", " ").strip(" /")
    return s if n is None or len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------------------ outline
def _schemes(pres):
    out = {}
    for m in pres.get("masters", []):
        cs = ((m.get("pageProperties") or {}).get("colorScheme") or {}).get("colors", [])
        out[m["objectId"]] = {c["type"]: _hex_from_rgb(c.get("color")) for c in cs}
    return out


def _element_index(pres):
    idx = {}
    for page in pres.get("masters", []) + pres.get("layouts", []):
        for el in page.get("pageElements", []):
            idx[el["objectId"]] = el
    return idx


def _inherited_style(el, index, depth=0):
    """Text style a placeholder inherits from its parent chain (first run of each parent)."""
    ph = (el.get("shape") or {}).get("placeholder") or {}
    pid = ph.get("parentObjectId")
    if not pid or depth > 4 or pid not in index:
        return {}
    parent = index[pid]
    base = _inherited_style(parent, index, depth + 1)
    for _c, st in _runs((parent.get("shape") or {}).get("text")):
        return {**base, **{k: v for k, v in st.items() if v not in (None, {}, "")}}
    return base


def _kind(el):
    for k in ("shape", "table", "image", "line", "elementGroup", "sheetsChart", "video", "wordArt",
              "speakerSpotlight"):
        if k in el:
            return k
    return "?"


def summarize_element(el, scheme, index, full=False, parent=IDENTITY):
    kind = _kind(el)
    d = {"id": el["objectId"], "geom": geom_str(element_box(el, parent))}
    if kind == "shape":
        sh = el["shape"]
        d["type"] = sh.get("shapeType", "SHAPE")
        ph = sh.get("placeholder")
        if ph:
            d["placeholder"] = ph.get("type") + (f"#{ph['index']}" if ph.get("index") else "")
        txt = text_of(sh.get("text"))
        if txt.strip():
            d["text"] = _clip(txt, None if full else 90)
            st = text_styles(sh.get("text"), scheme, _inherited_style(el, index) if ph else None)
            d["style"] = [style_tag(k) + (f" ({n}ch)" if len(st) > 1 else "") for k, n in
                          (st if full else st[:2])]
            if not full and len(st) > 2:
                d["style"].append(f"+{len(st) - 2} more")
            al = sorted(set(_alignments(sh.get("text"))))
            if al and al != ["START"]:
                d["align"] = ",".join(al)
        sp = sh.get("shapeProperties") or {}
        fill, outl = _fill_str(sp.get("shapeBackgroundFill"), scheme), _outline_str(sp.get("outline"), scheme)
        if fill and fill not in ("none", "inherit"):
            d["fill"] = fill
        if outl and outl not in ("none", "inherit"):
            d["outline"] = outl
        if sp.get("contentAlignment") and sp["contentAlignment"] != "TOP" and txt.strip():
            d["valign"] = sp["contentAlignment"]
    elif kind == "table":
        t = el["table"]
        d["type"] = f"TABLE {t.get('rows')}x{t.get('columns')}"
        d["col_widths"] = [_in(_mag(c.get("columnWidth"))) for c in t.get("tableColumns", [])]
        d["row_heights"] = [_in(_mag(r.get("rowHeight"))) for r in t.get("tableRows", [])]
        rows = []
        for r in t.get("tableRows", []):
            cells, fills, styles, aligns = [], [], {}, set()
            for c in r.get("tableCells", []):
                cells.append(_clip(text_of(c.get("text")), None if full else 45))
                cp = c.get("tableCellProperties") or {}
                fills.append(_fill_str(cp.get("tableCellBackgroundFill"), scheme) or "-")
                for k, n in text_styles(c.get("text"), scheme):
                    styles[k] = styles.get(k, 0) + n
                if text_of(c.get("text")).strip():
                    aligns |= set(_alignments(c.get("text")))
                    if cp.get("contentAlignment") not in (None, "TOP"):
                        aligns.add("v" + cp["contentAlignment"])
            row = {"cells": cells}
            if any(f not in ("-", "none") for f in fills):
                row["fills"] = fills if len(set(fills)) > 1 else fills[0]
            if aligns - {"START"}:
                row["align"] = ",".join(sorted(aligns))
            if styles:
                ranked = sorted(styles.items(), key=lambda kv: -kv[1])
                row["style"] = [style_tag(k) for k, _n in (ranked if full else ranked[:2])]
            rows.append(row)
        d["rows"] = rows
        if len(t.get("tableRows", [])) > 1:
            d["columns"] = _column_summary(t, scheme, full)
        borders = {}
        for grp in ("horizontalBorderRows", "verticalBorderRows"):
            for br in t.get(grp, []):
                for bc in br.get("tableBorderCells", []):
                    p = bc.get("tableBorderProperties") or {}
                    f = _fill_str(p.get("tableBorderFill"), scheme) or "none"
                    k = f"{f} {_mag(p.get('weight')) / EMU_PT:g}pt {p.get('dashStyle', 'SOLID')}"
                    borders[k] = borders.get(k, 0) + 1
        if borders:
            d["borders"] = [f"{k} x{n}" for k, n in sorted(borders.items(), key=lambda kv: -kv[1])]
    elif kind == "image":
        d["type"] = "IMAGE"
        if full and el["image"].get("sourceUrl"):
            d["source"] = el["image"]["sourceUrl"]
    elif kind == "line":
        ln = el["line"]
        d["type"] = f"LINE {ln.get('lineCategory', '')}".strip()
        lp = ln.get("lineProperties") or {}
        f = _fill_str(lp.get("lineFill"), scheme)
        if f:
            d["outline"] = f"{f} {_mag(lp.get('weight')) / EMU_PT:g}pt"
    elif kind == "elementGroup":
        kids = el["elementGroup"].get("children", [])
        d["type"] = f"GROUP ({len(kids)})"
        tf = _compose(parent, _tf(el.get("transform")))
        if full:
            d["children"] = [summarize_element(c, scheme, index, full, tf) for c in kids]
        else:
            txt = " | ".join(t for t in (_clip(text_of((c.get("shape") or {}).get("text")), 30)
                                         for c in kids) if t)
            if txt:
                d["text"] = _clip(txt, 90)
    else:
        d["type"] = kind.upper()
    if el.get("title") or el.get("description"):
        d["alt"] = _clip(el.get("title") or el.get("description"), 60)
    return d


def _column_summary(t, scheme, full=False):
    """Per column, over the body rows (all but the first): the styles, alignment and fills used."""
    cols = []
    for c in range(t.get("columns", 0)):
        styles, aligns, fills = {}, set(), {}
        for r in t.get("tableRows", [])[1:]:
            cells = r.get("tableCells", [])
            if c >= len(cells):
                continue
            cell = cells[c]
            if not text_of(cell.get("text")).strip():
                continue
            for k, n in text_styles(cell.get("text"), scheme):
                styles[k] = styles.get(k, 0) + n
            aligns |= set(_alignments(cell.get("text")))
            cp = cell.get("tableCellProperties") or {}
            aligns.add("v" + cp.get("contentAlignment", "TOP"))
            f = _fill_str(cp.get("tableCellBackgroundFill"), scheme) or "none"
            fills[f] = fills.get(f, 0) + 1
        ranked = sorted(styles.items(), key=lambda kv: -kv[1])
        cols.append({"style": [style_tag(k) for k, _n in (ranked if full else ranked[:2])],
                     "align": ",".join(sorted(aligns)),
                     "fills": [f for f, _n in sorted(fills.items(), key=lambda kv: -kv[1])]})
    return cols


TITLE_TYPES = ("TITLE", "CENTERED_TITLE")


def slide_title(slide, index, page_h_emu=6858000):
    """(text, how): the title placeholder's text, else the largest-font text in the top 30%."""
    els = slide.get("pageElements", [])
    top_emu = 0.3 * page_h_emu
    for el in els:
        ph = (el.get("shape") or {}).get("placeholder") or {}
        if ph.get("type") in TITLE_TYPES:
            t = text_of(el["shape"].get("text")).strip()
            if t:
                return t, "placeholder"
    best = None
    for el in els:
        sh = el.get("shape") or {}
        t = text_of(sh.get("text")).strip()
        if not t:
            continue
        sizes = [((st.get("fontSize") or {}).get("magnitude") or 0) for c, st in _runs(sh.get("text"))
                 if c.strip()]
        size = max(sizes or [0])
        y = element_box(el)[1]
        score = (y < top_emu, size, -y)             # prefer the top of the slide, then the biggest font
        if best is None or score > best[0]:
            best = (score, t)
    return (best[1], "largest text near the top") if best else ("", None)


def _layout_names(pres):
    return {l["objectId"]: (l.get("layoutProperties") or {}).get("displayName")
            or (l.get("layoutProperties") or {}).get("name") for l in pres.get("layouts", [])}


def notes_text(slide):
    np_ = (slide.get("slideProperties") or {}).get("notesPage") or {}
    sid = (np_.get("notesProperties") or {}).get("speakerNotesObjectId")
    for el in np_.get("pageElements", []):
        if el["objectId"] == sid:
            return text_of((el.get("shape") or {}).get("text"))
    return ""


def summarize_slide(pres, i, full=False, schemes=None, index=None, layouts=None):
    schemes = schemes if schemes is not None else _schemes(pres)
    index = index if index is not None else _element_index(pres)
    layouts = layouts if layouts is not None else _layout_names(pres)
    s = pres["slides"][i]
    sp = s.get("slideProperties") or {}
    scheme = schemes.get(sp.get("masterObjectId"), {})
    title, how = slide_title(s, index, _mag(((pres.get("pageSize") or {}).get("height"))) or 6858000)
    d = {"number": i + 1, "index": i, "id": s["objectId"], "skipped": bool(sp.get("isSkipped")),
         "layout": sp.get("layoutObjectId"), "layout_name": layouts.get(sp.get("layoutObjectId")),
         "master": sp.get("masterObjectId"), "title": _clip(title, None if full else 100)}
    if how and how != "placeholder":
        d["title_from"] = how
    bg = _fill_str((s.get("pageProperties") or {}).get("pageBackgroundFill"), scheme)
    if bg and bg != "inherit":
        d["background"] = bg
    d["elements"] = [summarize_element(el, scheme, index, full) for el in s.get("pageElements", [])]
    n = notes_text(s).strip()
    if n:
        d["notes"] = _clip(n, None if full else 200)
    return d


def outline(pres, numbers=None, full=False):
    schemes, index, layouts = _schemes(pres), _element_index(pres), _layout_names(pres)
    ps = pres.get("pageSize") or {}
    d = {"id": pres.get("presentationId"), "title": pres.get("title"), "revision": pres.get("revisionId"),
         "size_in": [_in(_mag(ps.get("width"))), _in(_mag(ps.get("height")))],
         "slide_count": len(pres.get("slides", [])),
         "masters": [{"id": m["objectId"], "name": (m.get("masterProperties") or {}).get("displayName"),
                      "background": _fill_str((m.get("pageProperties") or {}).get("pageBackgroundFill"),
                                              schemes.get(m["objectId"]))} for m in pres.get("masters", [])]}
    idxs = range(len(pres.get("slides", []))) if numbers is None else [n - 1 for n in numbers]
    d["slides"] = [summarize_slide(pres, i, full, schemes, index, layouts) for i in idxs]
    return d


def _render_el(e, indent):
    pad = " " * indent
    bits = [f"{pad}- {e['id']}  {e.get('type', '?')}", e["geom"]]
    if e.get("placeholder"):
        bits.append(f"ph={e['placeholder']}")
    for k in ("fill", "outline", "valign", "align"):
        if e.get(k):
            bits.append(f"{k}={e[k]}")
    lines = ["  ".join(bits)]
    if e.get("text"):
        lines.append(f"{pad}    \"{e['text']}\"")
    if e.get("style"):
        lines.append(f"{pad}    style: " + "; ".join(e["style"]))
    if e.get("col_widths"):
        lines.append(f"{pad}    cols(in): {e['col_widths']}  rows(in): {e['row_heights']}")
        for r, row in enumerate(e.get("rows", [])):
            extra = []
            if row.get("fills"):
                extra.append(f"fill={row['fills']}")
            if row.get("align"):
                extra.append(f"align={row['align']}")
            if row.get("style"):
                extra.append("style=" + "; ".join(row["style"]))
            lines.append(f"{pad}    r{r}: " + " | ".join(f'"{c}"' for c in row["cells"])
                         + (("   [" + "  ".join(extra) + "]") if extra else ""))
        for c, col in enumerate(e.get("columns", [])):
            lines.append(f"{pad}    body c{c}: " + "; ".join(col["style"]) + f"  align={col['align']}"
                         + f"  fill={'/'.join(col['fills'])}")
        if e.get("borders"):
            lines.append(f"{pad}    borders: " + "; ".join(e["borders"]))
    if e.get("alt"):
        lines.append(f"{pad}    alt: {e['alt']}")
    for c in e.get("children", []):
        lines.extend(_render_el(c, indent + 4))
    return lines


def render_outline(d):
    out = [f"deck: {d['title']!r}  id={d['id']}",
           f"revision: {d['revision']}  size: {d['size_in'][0]:g} x {d['size_in'][1]:g} in  "
           f"slides: {d['slide_count']}",
           "masters: " + ", ".join(f"{m['id']} {m['name']!r}" + (f" bg={m['background']}" if m.get("background") else "")
                                   for m in d["masters"])]
    for s in d["slides"]:
        out.append("")
        head = (f"slide {s['number']} [index {s['index']}]  id={s['id']}  layout={s['layout']} "
                f"{s['layout_name']!r}  master={s['master']}" + ("  HIDDEN (skipped)" if s["skipped"] else ""))
        out.append(head)
        if s.get("title"):
            out.append(f"  title: \"{s['title']}\"" + (f"  ({s['title_from']})" if s.get("title_from") else ""))
        if s.get("background"):
            out.append(f"  background: {s['background']}")
        for e in s["elements"]:
            out.extend(_render_el(e, 2))
        if s.get("notes"):
            out.append(f"  notes: \"{s['notes']}\"")
    return "\n".join(out)


# ------------------------------------------------------------------------------ slide refs
def slide_index(pres, ref):
    """0-based index of a slide given its 1-based number or objectId."""
    slides = pres.get("slides", [])
    r = str(ref).strip()
    if r.lstrip("#").isdigit():
        n = int(r.lstrip("#"))
        if not 1 <= n <= len(slides):
            raise SpecError(f"slide {n} does not exist (the deck has {len(slides)} slides)")
        return n - 1
    for i, s in enumerate(slides):
        if s["objectId"] == r:
            return i
    raise SpecError(f"no slide with objectId {r!r} (use a slide number or an id from `outline`)")


def slide_numbers(pres, sel):
    """1-based slide numbers for '27', '26-28', '3,5,9', 'all' or objectIds."""
    total = len(pres.get("slides", []))
    if sel is None or str(sel).strip().lower() == "all":
        return list(range(1, total + 1))
    out = []
    for part in str(sel).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b or a < 1 or b > total:
                raise SpecError(f"slide range {part!r} is outside 1-{total}")
            out.extend(range(a, b + 1))
        else:
            out.append(slide_index(pres, part) + 1)
    seen = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def position(pres, at, allow_end=True):
    """0-based insertion index for 'becomes slide N' (N may be count+1, i.e. the end)."""
    total = len(pres.get("slides", []))
    try:
        n = int(str(at).lstrip("#"))
    except ValueError:
        raise SpecError(f"position must be a slide number, got {at!r}")
    hi = total + 1 if allow_end else total
    if not 1 <= n <= hi:
        raise SpecError(f"position {n} is outside 1-{hi}")
    return n - 1


def move_insertion_index(current, final):
    """updateSlidesPosition.insertionIndex (counted before the move) that leaves the slide at
    0-based `current` at 0-based `final`."""
    return final + 1 if final > current else final


# ------------------------------------------------------------------------------ layouts
def masters_layouts(pres):
    out = []
    names = _layout_names(pres)
    used = {}
    for s in pres.get("slides", []):
        lid = (s.get("slideProperties") or {}).get("layoutObjectId")
        used[lid] = used.get(lid, 0) + 1
    for m in pres.get("masters", []):
        ls = []
        for l in pres.get("layouts", []):
            lp = l.get("layoutProperties") or {}
            if lp.get("masterObjectId") != m["objectId"]:
                continue
            phs = []
            for el in l.get("pageElements", []):
                ph = (el.get("shape") or {}).get("placeholder")
                if ph:
                    phs.append(ph.get("type") + (f"#{ph['index']}" if ph.get("index") else ""))
            ls.append({"id": l["objectId"], "name": lp.get("name"), "display": names.get(l["objectId"]),
                       "placeholders": phs, "used_by": used.get(l["objectId"], 0)})
        out.append({"id": m["objectId"], "name": (m.get("masterProperties") or {}).get("displayName"),
                    "layouts": ls})
    return out


def resolve_layout(pres, name=None, master=None, near=None):
    """Layout objectId for a layout id or name (display name first, then internal name; case-
    and space-insensitive). Ambiguity is settled by `master`, else by the master of the slide
    at 0-based index `near` (the neighbour of an insertion point)."""
    layouts = pres.get("layouts", [])
    name = name or "Blank"
    for l in layouts:
        if l["objectId"] == name:
            return l["objectId"]
    key = " ".join(str(name).lower().split())

    def norm(v):
        return " ".join(str(v or "").lower().split())

    def matches(field):
        return [l for l in layouts if norm((l.get("layoutProperties") or {}).get(field)) == key]

    cands = matches("displayName") or matches("name")
    if not cands:
        known = ", ".join(sorted({repr((l.get("layoutProperties") or {}).get("displayName")) for l in layouts}))
        raise SpecError(f"no layout named {name!r}. Layouts: {known}")
    if master:
        cands = [l for l in cands if (l.get("layoutProperties") or {}).get("masterObjectId") == master] or []
        if not cands:
            raise SpecError(f"layout {name!r} is not in master {master!r} (see `layouts`)")
    if len(cands) > 1 and near is not None and pres.get("slides"):
        i = min(max(near, 0), len(pres["slides"]) - 1)
        m = (pres["slides"][i].get("slideProperties") or {}).get("masterObjectId")
        narrowed = [l for l in cands if (l.get("layoutProperties") or {}).get("masterObjectId") == m]
        cands = narrowed or cands
    if len(cands) > 1:
        opts = ", ".join(f"{l['objectId']} (master {(l.get('layoutProperties') or {}).get('masterObjectId')})"
                         for l in cands)
        raise SpecError(f"layout {name!r} is ambiguous: {opts}. Pass --master or the layout id.")
    return cands[0]["objectId"]


# ------------------------------------------------------------------------------ simple requests
def req_skip(slide_id, skipped):
    return {"updateSlideProperties": {"objectId": slide_id, "slideProperties": {"isSkipped": bool(skipped)},
                                      "fields": "isSkipped"}}


def req_move(slide_ids, insertion_index):
    return {"updateSlidesPosition": {"slideObjectIds": list(slide_ids), "insertionIndex": insertion_index}}


def req_create_slide(layout_id, insertion_index, object_id=None):
    body = {"insertionIndex": insertion_index, "slideLayoutReference": {"layoutId": layout_id}}
    if object_id:
        body["objectId"] = object_id
    return {"createSlide": body}


def reqs_notes(slide, text):
    np_ = (slide.get("slideProperties") or {}).get("notesPage") or {}
    sid = (np_.get("notesProperties") or {}).get("speakerNotesObjectId")
    if not sid:
        raise SpecError(f"slide {slide['objectId']} has no speaker-notes shape id")
    reqs = []
    if notes_text(slide).strip("\n"):
        reqs.append({"deleteText": {"objectId": sid, "textRange": {"type": "ALL"}}})
    if text:
        reqs.append({"insertText": {"objectId": sid, "insertionIndex": 0, "text": text}})
    return reqs


# ------------------------------------------------------------------------------ text measuring
# Advance widths (1/1000 em) of Helvetica / Arial for ASCII 32..126, regular then bold.
_W_REG = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
          556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667,
          611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
          667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500,
          222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_W_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
           556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667,
           611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
           667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556,
           278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_W_SPECIAL = {"…": 1000, "—": 1000, "–": 556, "‘": 222, "’": 222, "“": 333,
              "”": 333, "•": 350, "£": 556, "€": 556, "\t": 1112}
# rough width of other families relative to Helvetica/Arial
_FONT_FACTOR = {"arial": 1.0, "helvetica": 1.0, "helvetica neue": 1.0, "liberation sans": 1.0,
                "roboto": 0.98, "inter": 1.03, "open sans": 1.07, "lato": 0.96, "montserrat": 1.12,
                "poppins": 1.12, "verdana": 1.15, "tahoma": 1.02, "calibri": 0.9, "segoe ui": 0.98,
                "georgia": 1.05, "times new roman": 0.9, "garamond": 0.88, "arial narrow": 0.82,
                "roboto condensed": 0.82, "source sans pro": 0.93, "dm sans": 1.02, "work sans": 1.06,
                "ibm plex sans": 1.02, "nunito": 1.02, "raleway": 1.08, "playfair display": 1.02}


def text_width_pt(s, size, bold=False, font="Arial"):
    table = _W_BOLD if bold else _W_REG
    units = 0
    for ch in s:
        o = ord(ch)
        if 32 <= o <= 126:
            units += table[o - 32]
        else:
            units += _W_SPECIAL.get(ch, 556)
    return units / 1000.0 * size * _FONT_FACTOR.get(str(font or "").lower(), 1.05)


def _wrap_lines(words, width_pt):
    """Greedy word wrap. `words` = [(word_width, space_width_after)]; returns the line count."""
    lines, cur = 1, 0.0
    for w, sp in words:
        if cur and cur + w > width_pt:
            lines += 1
            cur = 0.0
        while w > width_pt and width_pt > 0:       # a word longer than the line breaks mid-word
            w -= width_pt
            lines += 1
        cur += w + sp
    return lines


def paragraph_height_pt(runs, width_pt, line_spacing=100, space_before=0, space_after=0):
    """Estimated height of one paragraph: runs = [(text, size, bold, font)]."""
    words, max_size, lh = [], 0, LINE_HEIGHT
    for text, size, bold, font in runs:
        if size >= max_size:
            max_size, lh = size, _LINE_HEIGHTS.get(str(font or "").lower(), LINE_HEIGHT)
        for tok in re.findall(r"\S+\s*", text) or ([text] if text else []):
            word = tok.rstrip()
            words.append((text_width_pt(word, size, bold, font),
                          text_width_pt(tok[len(word):], size, bold, font)))
    max_size = max_size or 12
    n = _wrap_lines(words, width_pt) if words else 1
    return n * max_size * lh * (line_spacing or 100) / 100.0 + (space_before or 0) + (space_after or 0)


# ------------------------------------------------------------------------------ spec parsing
STYLE_KEYS = {"font", "size", "bold", "italic", "underline", "color", "link", "strike"}
PARA_KEYS = {"text", "runs", "align", "bullet", "level", "style", "space_before", "space_after",
             "line_spacing"} | STYLE_KEYS
TEXT_EL_KEYS = {"id", "type", "x", "y", "w", "h", "text", "style", "align", "valign", "bullet",
                "line_spacing", "space_before", "space_after", "fill", "outline", "markup"} | STYLE_KEYS
TABLE_KEYS = {"id", "type", "x", "y", "w", "h", "rows", "col_widths", "row_heights", "style", "header",
              "header_rows", "fill", "borders", "merge", "align", "valign", "markup", "line_spacing",
              "space_before", "space_after", "bullet"} | STYLE_KEYS
CELL_KEYS = {"text", "fill", "style", "align", "valign", "bullet", "markup", "line_spacing", "space_before",
             "space_after"} | STYLE_KEYS
LINE_KEYS = {"id", "type", "x", "y", "w", "h", "color", "weight", "dash", "start_arrow", "end_arrow"}
IMAGE_KEYS = {"id", "type", "x", "y", "w", "h", "url"}
SHAPES = {"text": "TEXT_BOX", "textbox": "TEXT_BOX", "rect": "RECTANGLE", "rectangle": "RECTANGLE",
          "round_rect": "ROUND_RECTANGLE", "rounded_rect": "ROUND_RECTANGLE", "ellipse": "ELLIPSE"}
ALIGN = {"start": "START", "left": "START", "center": "CENTER", "centre": "CENTER", "end": "END",
         "right": "END", "justified": "JUSTIFIED", "justify": "JUSTIFIED"}
VALIGN = {"top": "TOP", "middle": "MIDDLE", "center": "MIDDLE", "centre": "MIDDLE", "bottom": "BOTTOM"}
SPEC_KEYS = {"slide", "new", "prefix", "defaults", "elements", "markup", "notes", "replace"}

_EMPH = r"([^\s*]|[^\s*].*?[^\s\\*])"  # emphasised text: no space or * just inside the markers,
#                                        and an escaped \* never closes it
_MARK = re.compile(r"\\\*|\*\*\*" + _EMPH + r"\*\*\*|\*\*" + _EMPH + r"\*\*|\*" + _EMPH + r"\*")


def parse_markup(s, markup=True):
    """'plain **bold** *italic* ***both***' -> runs; '\\*' is a literal asterisk."""
    if not markup:
        return [{"text": s}]
    runs, pos = [], 0
    for m in _MARK.finditer(s):
        if m.start() > pos:
            runs.append({"text": s[pos:m.start()]})
        if m.group(0) == "\\*":
            runs.append({"text": "*"})
        elif m.group(1) is not None:
            runs.append({"text": m.group(1).replace("\\*", "*"), "bold": True, "italic": True})
        elif m.group(2) is not None:
            runs.append({"text": m.group(2).replace("\\*", "*"), "bold": True})
        else:
            runs.append({"text": m.group(3).replace("\\*", "*"), "italic": True})
        pos = m.end()
    if pos < len(s):
        runs.append({"text": s[pos:]})
    merged = []                                  # join neighbouring plain runs
    for r in runs:
        if merged and len(r) == 1 and len(merged[-1]) == 1:
            merged[-1] = {"text": merged[-1]["text"] + r["text"]}
        else:
            merged.append(r)
    return merged or [{"text": ""}]


def _check_keys(d, allowed, where):
    bad = sorted(set(d) - allowed)
    if bad:
        raise SpecError(f"{where}: unknown key(s) {', '.join(bad)} (allowed: {', '.join(sorted(allowed))})")


def _style_of(d, where):
    """Style keys found directly on `d` merged over its `style:` mapping."""
    st = d.get("style") or {}
    if not isinstance(st, dict):
        raise SpecError(f"{where}: style must be a mapping")
    st = dict(st)
    _check_keys(st, STYLE_KEYS, f"{where} style")
    st.update({k: d[k] for k in STYLE_KEYS if k in d})
    if "color" in st:
        parse_color(st["color"])                      # fail on a bad colour even with no text
    if "size" in st:
        try:
            if float(st["size"]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise SpecError(f"{where}: size must be a positive number of points, got {st['size']!r}")
    return st


def paragraphs(value, markup, where):
    """Normalise spec text into [{'runs': [{'text', **style}], 'style': {}, **para props}]."""
    if value is None:
        return []
    if isinstance(value, (int, float)):
        value = str(value)
    if isinstance(value, str):
        return [{"runs": parse_markup(line, markup), "style": {}} for line in value.split("\n")]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise SpecError(f"{where}: text must be a string or a list of paragraphs")
    out = []
    for n, p in enumerate(value):
        w = f"{where} paragraph {n + 1}"
        if isinstance(p, (int, float)):
            p = str(p)
        if isinstance(p, str):
            out.extend({"runs": parse_markup(line, markup), "style": {}} for line in p.split("\n"))
            continue
        if not isinstance(p, dict):
            raise SpecError(f"{w}: must be a string or a mapping")
        _check_keys(p, PARA_KEYS, w)
        para = {k: p[k] for k in ("align", "bullet", "level", "space_before", "space_after", "line_spacing")
                if k in p}
        para["style"] = _style_of(p, w)
        if "runs" in p:
            runs = []
            for r in p["runs"] or []:
                if isinstance(r, str):
                    runs.extend(parse_markup(r, markup))
                elif isinstance(r, dict):
                    _check_keys(r, STYLE_KEYS | {"text"}, f"{w} run")
                    runs.append(dict(r, text=str(r.get("text", ""))))
                else:
                    raise SpecError(f"{w}: a run must be a string or a mapping")
            para["runs"] = runs or [{"text": ""}]
            out.append(para)
        else:
            lines = str(p.get("text", "")).split("\n")
            for line in lines:
                out.append(dict(para, runs=parse_markup(line, markup)))
    return out


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _text_style_body(st):
    body, fields = {}, []
    if "font" in st:
        body["fontFamily"] = str(st["font"]); fields.append("fontFamily")
    if "size" in st:
        body["fontSize"] = {"magnitude": float(st["size"]), "unit": "PT"}; fields.append("fontSize")
    for k, api in (("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
                   ("strike", "strikethrough")):
        if k in st:
            body[api] = bool(st[k]); fields.append(api)
    if "color" in st:
        body["foregroundColor"] = {"opaqueColor": parse_color(st["color"])}; fields.append("foregroundColor")
    if st.get("link"):
        body["link"] = {"url": str(st["link"])}; fields.append("link")
    return body, fields


def _para_style_body(p):
    body, fields = {}, []
    if p.get("align"):
        a = ALIGN.get(str(p["align"]).lower())
        if not a:
            raise SpecError(f"align {p['align']!r}: use start/center/end/justified")
        body["alignment"] = a; fields.append("alignment")
    if p.get("line_spacing") is not None:
        body["lineSpacing"] = float(p["line_spacing"]); fields.append("lineSpacing")
    if p.get("space_before") is not None:
        body["spaceAbove"] = {"magnitude": float(p["space_before"]), "unit": "PT"}; fields.append("spaceAbove")
    if p.get("space_after") is not None:
        body["spaceBelow"] = {"magnitude": float(p["space_after"]), "unit": "PT"}; fields.append("spaceBelow")
    return body, fields


def text_requests(object_id, paras, base_style, base_para, cell=None):
    """insertText + style + bullet requests that fill an EMPTY shape or table cell with
    `paras`. base_style / base_para are the inherited run style / paragraph props."""
    loc = {"cellLocation": {"rowIndex": cell[0], "columnIndex": cell[1]}} if cell else {}
    pieces, spans, pos = [], [], 0
    for n, p in enumerate(paras):
        pp = {**base_para, **{k: v for k, v in p.items() if k not in ("runs", "style")}}
        bullet = pp.get("bullet")
        preset = (DEFAULT_BULLET if bullet is True else str(bullet)) if bullet else None
        level = int(pp.get("level") or 0) if preset else 0
        start = pos
        prefix = "\t" * level
        if prefix:
            pieces.append(prefix)
            pos += len(prefix)
        runs = []
        for r in p["runs"] or [{"text": ""}]:
            t = str(r.get("text", ""))
            st = {**base_style, **p.get("style", {}), **{k: v for k, v in r.items() if k != "text"}}
            runs.append({"a": pos, "b": pos + _u16(t), "st": st, "text": t})
            pieces.append(t)
            pos += _u16(t)
        spans.append({"start": start, "end": pos, "runs": runs, "para": pp, "preset": preset})
        if n < len(paras) - 1:
            pieces.append("\n")
            pos += 1
    text = "".join(pieces)
    if not text:
        return [], spans
    total = _u16(text)
    reqs = [{"insertText": {"objectId": object_id, **loc, "insertionIndex": 0, "text": text}}]
    # run styles: one request per stretch of identical style, covering every character
    segs = []
    for i, sp in enumerate(spans):
        for j, r in enumerate(sp["runs"]):
            a = sp["start"] if j == 0 else r["a"]      # the tab prefix takes the first run's style
            b = r["b"]
            if j == len(sp["runs"]) - 1 and i < len(spans) - 1:
                b += 1                                  # the paragraph's newline goes with its last run
            if b <= a:
                continue
            st = r["st"]
            if segs and segs[-1][2] == st and segs[-1][1] == a:
                segs[-1] = (segs[-1][0], b, st)
            else:
                segs.append((a, b, st))
    for a, b, st in segs:
        body, fields = _text_style_body(st)
        if fields:
            reqs.append({"updateTextStyle": {"objectId": object_id, **loc, "style": body,
                                             "textRange": {"type": "FIXED_RANGE", "startIndex": a,
                                                           "endIndex": b},
                                             "fields": ",".join(fields)}})
    # paragraph styles, grouped over neighbouring paragraphs with the same properties
    groups = []
    for sp in spans:
        body, fields = _para_style_body(sp["para"])
        a, b = sp["start"], min(max(sp["end"], sp["start"] + 1), total)
        if not fields or b <= a:
            continue
        key = json.dumps(body, sort_keys=True)
        if groups and groups[-1][2] == key and groups[-1][1] + 1 >= a:
            groups[-1] = (groups[-1][0], b, key, body, fields)
        else:
            groups.append((a, b, key, body, fields))
    for a, b, _k, body, fields in groups:
        reqs.append({"updateParagraphStyle": {"objectId": object_id, **loc, "style": body,
                                              "textRange": {"type": "FIXED_RANGE", "startIndex": a,
                                                            "endIndex": b},
                                              "fields": ",".join(fields)}})
    # bullets last, back to front: each createParagraphBullets strips the leading tabs it reads
    bgroups = []
    for sp in spans:
        if not sp["preset"]:
            continue
        if bgroups and bgroups[-1][2] == sp["preset"] and bgroups[-1][1] + 1 == sp["start"]:
            bgroups[-1] = (bgroups[-1][0], sp["end"], sp["preset"])
        else:
            bgroups.append((sp["start"], sp["end"], sp["preset"]))
    for a, b, preset in reversed(bgroups):
        reqs.append({"createParagraphBullets": {"objectId": object_id, **loc, "bulletPreset": preset,
                                                "textRange": {"type": "FIXED_RANGE", "startIndex": a,
                                                              "endIndex": min(max(b, a + 1), total)}}})
    return reqs, spans


def estimate_height_in(spans, width_in):
    """Estimated text height (inches) of the spans `text_requests` returned."""
    total = 0.0
    width_pt = max(width_in, 0.1) * 72
    for sp in spans:
        runs = [(r["text"], float(r["st"].get("size", 14)), bool(r["st"].get("bold")),
                 r["st"].get("font", "Arial")) for r in sp["runs"]]
        indent = (0.3 * (1 + int(sp["para"].get("level") or 0)) * 72) if sp["preset"] else 0
        p = sp["para"]
        total += paragraph_height_pt(runs, width_pt - indent, p.get("line_spacing") or 100,
                                     p.get("space_before") or 0, p.get("space_after") or 0)
    return total / 72.0


def _fill_props(value, where, kind="shape"):
    """(properties, fields) for a shape/cell background fill."""
    key = "shapeBackgroundFill" if kind == "shape" else "tableCellBackgroundFill"
    if value is None:
        return {}, []
    if isinstance(value, str) and value.lower() in ("none", "transparent"):
        return {key: {"propertyState": "NOT_RENDERED"}}, [f"{key}.propertyState"]
    if isinstance(value, dict):
        _check_keys(value, {"color", "alpha"}, f"{where} fill")
        col, alpha = value.get("color"), float(value.get("alpha", 1))
    else:
        col, alpha = value, 1.0
    return ({key: {"propertyState": "RENDERED", "solidFill": {"color": parse_color(col), "alpha": alpha}}},
            [f"{key}.propertyState", f"{key}.solidFill.color", f"{key}.solidFill.alpha"])


def _outline_props(value, where):
    if value is None:
        return {}, []
    if isinstance(value, str) and value.lower() == "none":
        return {"outline": {"propertyState": "NOT_RENDERED"}}, ["outline.propertyState"]
    if isinstance(value, str):
        value = {"color": value}
    if not isinstance(value, dict):
        raise SpecError(f"{where}: outline must be a colour, 'none' or a mapping")
    _check_keys(value, {"color", "weight", "dash", "alpha"}, f"{where} outline")
    o = {"propertyState": "RENDERED",
         "outlineFill": {"solidFill": {"color": parse_color(value.get("color", "#000000")),
                                       "alpha": float(value.get("alpha", 1))}},
         "weight": {"magnitude": float(value.get("weight", 1)), "unit": "PT"},
         "dashStyle": str(value.get("dash", "SOLID")).upper()}
    return {"outline": o}, ["outline.propertyState", "outline.outlineFill.solidFill.color",
                            "outline.outlineFill.solidFill.alpha", "outline.weight", "outline.dashStyle"]


def _num(el, k, where, default=None, required=True):
    v = el.get(k, default)
    if v is None:
        if required:
            raise SpecError(f"{where}: needs {k} (inches)")
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise SpecError(f"{where}: {k} must be a number of inches, got {v!r}")


def _element_props(page_id, x, y, w, h):
    return {"pageObjectId": page_id,
            "size": {"width": {"magnitude": emu(w), "unit": "EMU"}, "height": {"magnitude": emu(h), "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "shearX": 0, "shearY": 0,
                          "translateX": emu(x), "translateY": emu(y), "unit": "EMU"}}


def _read_input(path):
    if str(path) == "-":
        return sys.stdin.read()
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"file not found: {path}")
    return p.read_text(encoding="utf-8-sig")


def load_spec(path):
    raw = _read_input(path)
    if str(path).lower().endswith(".json") or raw.lstrip().startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise SpecError(f"{path}: invalid JSON: {e}")
    from ruamel.yaml import YAML
    try:
        return YAML(typ="safe").load(raw)
    except Exception as e:
        raise SpecError(f"{path}: invalid YAML: {e}")


def _default_prefix(slide_id):
    return hashlib.sha1(slide_id.encode("utf-8")).hexdigest()[:6]


def build_requests(pres, spec, spec_name="spec"):
    """-> (requests, warnings, info). Pure: `pres` is a fresh presentations.get."""
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a mapping (slide/new, elements, ...)")
    _check_keys(spec, SPEC_KEYS, "spec")
    warnings, reqs = [], []
    slides = pres.get("slides", [])
    ps = pres.get("pageSize") or {}
    page_w, page_h = _mag(ps.get("width")) / EMU_IN, _mag(ps.get("height")) / EMU_IN
    all_ids = {}
    for s in slides:
        all_ids[s["objectId"]] = s["objectId"]
        for el in s.get("pageElements", []):
            all_ids[el["objectId"]] = s["objectId"]
            for c in (el.get("elementGroup") or {}).get("children", []):
                all_ids[c["objectId"]] = s["objectId"]

    # ---- the target slide
    info = {}
    if ("slide" in spec) == ("new" in spec):
        raise SpecError("the spec needs exactly one of `slide:` (an existing slide number or id) "
                        "or `new:` (a slide to create)")
    target = None
    if "new" in spec:
        new = spec["new"] or {}
        if not isinstance(new, dict):
            raise SpecError("new: must be a mapping (at, layout, id)")
        _check_keys(new, {"at", "layout", "master", "id"}, "new")
        sid = new.get("id") or ("bc_" + re.sub(r"[^A-Za-z0-9_]", "_", Path(spec_name).stem))[:50]
        if len(sid) < 5:
            sid = (sid + "_slide")[:50]
        check_object_id(sid, "new.id")
        existing = [i for i, s in enumerate(slides) if s["objectId"] == sid]
        if existing:
            target = slides[existing[0]]
            info["slide"] = f"slide {existing[0] + 1} (id {sid}, from an earlier build: rebuilt in place)"
        else:
            if sid in all_ids:
                raise SpecError(f"new.id {sid!r} is already used by an element on slide {all_ids[sid]}")
            at = new.get("at", len(slides) + 1)
            ins = position(pres, at)
            layout = resolve_layout(pres, new.get("layout"), new.get("master"), near=ins - 1 if ins else 0)
            reqs.append(req_create_slide(layout, ins, sid))
            target = {"objectId": sid, "pageElements": []}
            info["slide"] = f"new slide {ins + 1} (id {sid}, layout {layout})"
    else:
        i = slide_index(pres, spec["slide"])
        target = slides[i]
        info["slide"] = f"slide {i + 1} (id {target['objectId']})"
    page_id = target["objectId"]
    info["slide_id"] = page_id

    prefix = str(spec.get("prefix") or _default_prefix(page_id))
    if not re.fullmatch(r"[A-Za-z0-9]{1,20}", prefix):
        raise SpecError("prefix must be 1-20 letters/digits")
    own = f"{OWN}{prefix}_"

    # ---- remove what an earlier build of this spec left on the slide (only our own ids)
    if spec.get("replace", True):
        for el in target.get("pageElements", []):
            if el["objectId"].startswith(own) and not (el.get("shape") or {}).get("placeholder"):
                reqs.append({"deleteObject": {"objectId": el["objectId"]}})
    removed = sum(1 for r in reqs if "deleteObject" in r)
    if removed:
        info["removed"] = removed

    defaults = spec.get("defaults") or {}
    _check_keys(defaults, STYLE_KEYS | {"markup", "align", "valign", "line_spacing", "space_before",
                                        "space_after"}, "defaults")
    d_style = {"font": "Arial", "size": 14, "color": "#000000", "bold": False, "italic": False}
    d_style.update({k: defaults[k] for k in STYLE_KEYS if k in defaults})
    d_para = {k: defaults[k] for k in ("align", "line_spacing", "space_before", "space_after") if k in defaults}
    d_valign = defaults.get("valign")
    markup_default = spec.get("markup", defaults.get("markup", True))

    els = spec.get("elements") or []
    if not isinstance(els, list) or not els:
        raise SpecError("elements: needs a list of elements")
    seen, created = set(), []
    for n, el in enumerate(els):
        if not isinstance(el, dict):
            raise SpecError(f"element {n + 1}: must be a mapping")
        eid = str(el.get("id") or "")
        where = f"element {n + 1} ({eid or 'no id'})"
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", eid):
            raise SpecError(f"{where}: needs an id (letters, digits, _ or -)")
        if eid in seen:
            raise SpecError(f"{where}: duplicate id")
        seen.add(eid)
        oid = check_object_id(own + eid, f"{where} objectId")
        if oid in all_ids and all_ids[oid] != page_id:
            raise SpecError(f"{where}: objectId {oid} is already used on slide {all_ids[oid]}; set a "
                            f"different `prefix:`")
        typ = str(el.get("type", "text")).lower()
        markup = el.get("markup", markup_default)
        x, y = _num(el, "x", where), _num(el, "y", where)
        if typ in SHAPES:
            _check_keys(el, TEXT_EL_KEYS, where)
            w, h = _num(el, "w", where), _num(el, "h", where)
            reqs.append({"createShape": {"objectId": oid, "shapeType": SHAPES[typ],
                                         "elementProperties": _element_props(page_id, x, y, w, h)}})
            props, fields = {}, []
            fill = el.get("fill", None if typ in ("text", "textbox") else "none")
            outline = el.get("outline", None if typ in ("text", "textbox") else "none")
            for fn in (lambda: _fill_props(fill, where), lambda: _outline_props(outline, where)):
                p, f = fn()
                props.update(p); fields += f
            va = el.get("valign", d_valign)
            if va:
                if str(va).lower() not in VALIGN:
                    raise SpecError(f"{where}: valign {va!r}: use top/middle/bottom")
                props["contentAlignment"] = VALIGN[str(va).lower()]; fields.append("contentAlignment")
            if fields:
                reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props,
                                                       "fields": ",".join(fields)}})
            paras = paragraphs(el.get("text"), markup, where)
            base_para = {**d_para, **{k: el[k] for k in ("align", "bullet", "line_spacing", "space_before",
                                                         "space_after") if k in el}}
            treqs, spans = text_requests(oid, paras, {**d_style, **_style_of(el, where)}, base_para)
            reqs += treqs
            if treqs:
                # the text may eat into the box's padding; warn when it runs past the box itself
                need = estimate_height_in(spans, w - 2 * INSET_IN)
                if need > h * 1.02:
                    warnings.append(f"{eid}: text needs ~{need:.2f} in but the box is {h:g} in tall "
                                    f"(Slides does not shrink text): enlarge h, cut text or reduce size")
            _bounds(eid, x, y, w, h, page_w, page_h, warnings)
        elif typ == "table":
            _check_keys(el, TABLE_KEYS, where)
            treqs, tw, th = _table_requests(oid, page_id, el, x, y, where, d_style, d_para, d_valign,
                                            markup, warnings)
            reqs += treqs
            _bounds(eid, x, y, tw, th, page_w, page_h, warnings)
        elif typ == "line":
            _check_keys(el, LINE_KEYS, where)
            w, h = _num(el, "w", where, 0), _num(el, "h", where, 0)
            if w < 0 or h < 0:
                raise SpecError(f"{where}: a line runs from (x, y) to (x+w, y+h); w and h must be >= 0")
            reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT",
                                        "elementProperties": _element_props(page_id, x, y, w, h)}})
            lp = {"lineFill": {"solidFill": {"color": parse_color(el.get("color", "#000000"))}},
                  "weight": {"magnitude": float(el.get("weight", 1)), "unit": "PT"},
                  "dashStyle": str(el.get("dash", "SOLID")).upper()}
            fields = ["lineFill.solidFill.color", "weight", "dashStyle"]
            for k, api in (("start_arrow", "startArrow"), ("end_arrow", "endArrow")):
                if el.get(k):
                    lp[api] = str(el[k]).upper(); fields.append(api)
            reqs.append({"updateLineProperties": {"objectId": oid, "lineProperties": lp,
                                                  "fields": ",".join(fields)}})
            _bounds(eid, x, y, w, h, page_w, page_h, warnings)
        elif typ == "image":
            _check_keys(el, IMAGE_KEYS, where)
            w, h = _num(el, "w", where), _num(el, "h", where)
            if not el.get("url"):
                raise SpecError(f"{where}: an image needs a public url")
            reqs.append({"createImage": {"objectId": oid, "url": str(el["url"]),
                                         "elementProperties": _element_props(page_id, x, y, w, h)}})
            _bounds(eid, x, y, w, h, page_w, page_h, warnings)
        else:
            raise SpecError(f"{where}: unknown type {typ!r} (text, rect, round_rect, ellipse, table, line, image)")
        created.append(oid)

    if spec.get("notes") is not None:
        if any(s["objectId"] == page_id for s in slides):
            reqs += reqs_notes(target, str(spec["notes"]))
        else:                       # the new slide's notes shape exists only after this batch
            info["notes_after"] = str(spec["notes"])
    info["created"] = created
    info["prefix"] = prefix
    return reqs, warnings, info


def _bounds(eid, x, y, w, h, page_w, page_h, warnings):
    if page_w and (x < 0 or y < 0 or x + w > page_w + 0.01 or y + h > page_h + 0.01):
        warnings.append(f"{eid}: extends beyond the {page_w:g} x {page_h:g} in slide "
                        f"(x{x:g}..{x + w:.2f}, y{y:g}..{y + h:.2f})")


def _cell_value(c, where):
    if isinstance(c, dict):
        _check_keys(c, CELL_KEYS, where)
        return c
    if c is None:
        return {"text": ""}
    if isinstance(c, (list, str, int, float)):
        return {"text": c}
    raise SpecError(f"{where}: a cell must be text, a list of paragraphs or a mapping")


def _table_requests(oid, page_id, el, x, y, where, d_style, d_para, d_valign, markup, warnings):
    rows = el.get("rows")
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) and r for r in rows):
        raise SpecError(f"{where}: rows must be a non-empty list of non-empty lists")
    nr, nc = len(rows), max(len(r) for r in rows)
    cw = el.get("col_widths")
    w = el.get("w")
    if cw is not None:
        if len(cw) != nc:
            raise SpecError(f"{where}: col_widths has {len(cw)} entries for {nc} columns")
        cw = [float(v) for v in cw]
        w = sum(cw) if w is None else float(w)
        if abs(sum(cw) - w) > 0.05:
            warnings.append(f"{el['id']}: col_widths add up to {sum(cw):.2f} in, not w={w:g}; using col_widths")
            w = sum(cw)
    else:
        if w is None:
            raise SpecError(f"{where}: needs w or col_widths")
        w = float(w)
        cw = [w / nc] * nc
    rh = el.get("row_heights")
    if isinstance(rh, (int, float)):
        rh = [float(rh)] * nr
    if rh is not None and len(rh) != nr:
        raise SpecError(f"{where}: row_heights has {len(rh)} entries for {nr} rows")
    h = el.get("h")
    if rh is None and h is not None:
        rh = [float(h) / nr] * nr
    h = float(h) if h is not None else (sum(rh) if rh else 0.4 * nr)
    reqs = [{"createTable": {"objectId": oid, "rows": nr, "columns": nc,
                             "elementProperties": {"pageObjectId": page_id,
                                                   "size": {"width": {"magnitude": emu(w), "unit": "EMU"},
                                                            "height": {"magnitude": emu(h), "unit": "EMU"}},
                                                   "transform": {"scaleX": 1, "scaleY": 1,
                                                                 "translateX": emu(x), "translateY": emu(y),
                                                                 "unit": "EMU"}}}}]
    by_w = {}
    for i, v in enumerate(cw):
        if v < 32 / 72:
            raise SpecError(f"{where}: column {i + 1} is narrower than Slides' minimum (0.45 in)")
        by_w.setdefault(emu(v), []).append(i)
    for mag, idxs in by_w.items():
        reqs.append({"updateTableColumnProperties": {"objectId": oid, "columnIndices": idxs,
                                                     "tableColumnProperties": {"columnWidth": {"magnitude": mag,
                                                                                               "unit": "EMU"}},
                                                     "fields": "columnWidth"}})
    if rh:
        by_h = {}
        for i, v in enumerate(rh):
            by_h.setdefault(emu(v), []).append(i)
        for mag, idxs in by_h.items():
            reqs.append({"updateTableRowProperties": {"objectId": oid, "rowIndices": idxs,
                                                      "tableRowProperties": {"minRowHeight": {"magnitude": mag,
                                                                                              "unit": "EMU"}},
                                                      "fields": "minRowHeight"}})
    covered = set()
    for m in el.get("merge") or []:
        if not (isinstance(m, list) and len(m) == 4):
            raise SpecError(f"{where}: each merge is [row, col, rowspan, colspan] (0-based row/col)")
        r0, c0, rs, cs = (int(v) for v in m)
        if r0 < 0 or c0 < 0 or rs < 1 or cs < 1 or r0 + rs > nr or c0 + cs > nc:
            raise SpecError(f"{where}: merge {m} is outside the {nr}x{nc} table")
        reqs.append({"mergeTableCells": {"objectId": oid, "tableRange": {
            "location": {"rowIndex": r0, "columnIndex": c0}, "rowSpan": rs, "columnSpan": cs}}})
        covered |= {(r, c) for r in range(r0, r0 + rs) for c in range(c0, c0 + cs)} - {(r0, c0)}
    merged_width = {}
    for m in el.get("merge") or []:
        r0, c0, rs, cs = (int(v) for v in m)
        merged_width[(r0, c0)] = sum(cw[c0:c0 + cs])

    header = el.get("header") or {}
    if not isinstance(header, dict):
        raise SpecError(f"{where}: header must be a mapping (style, fill, align, ...)")
    _check_keys(header, CELL_KEYS - {"text"}, f"{where} header")
    n_head = int(el.get("header_rows", 1 if header else 0))
    t_style = {**d_style, **_style_of(el, where)}
    t_para = {**d_para, **{k: el[k] for k in ("align", "bullet", "line_spacing", "space_before", "space_after")
                           if k in el}}
    fill_reqs, row_need = [], [0.0] * nr

    def fill_req(r0, c0, rs, cs, fill, valign, w_):
        props, fields = _fill_props(fill, w_, kind="cell")
        if valign:
            if str(valign).lower() not in VALIGN:
                raise SpecError(f"{w_}: valign {valign!r}: use top/middle/bottom")
            props["contentAlignment"] = VALIGN[str(valign).lower()]
            fields.append("contentAlignment")
        if fields:
            fill_reqs.append({"updateTableCellProperties": {
                "objectId": oid, "tableRange": {"location": {"rowIndex": r0, "columnIndex": c0},
                                                "rowSpan": rs, "columnSpan": cs},
                "tableCellProperties": props, "fields": ",".join(fields)}})

    fill_req(0, 0, nr, nc, el.get("fill"), el.get("valign", d_valign), where)
    if n_head and (header.get("fill") is not None or header.get("valign")):
        fill_req(0, 0, min(n_head, nr), nc, header.get("fill"), header.get("valign"), f"{where} header")
    for r, row in enumerate(rows):
        for c in range(nc):
            cw_ = f"{where} cell [{r}][{c}]"
            cell = _cell_value(row[c] if c < len(row) else None, cw_)
            if (r, c) in covered:
                if str(cell.get("text") or "").strip():
                    warnings.append(f"{el['id']}: cell [{r}][{c}] is inside a merge; its text is ignored")
                continue
            is_head = r < n_head
            style = {**t_style, **(_style_of(header, f"{where} header") if is_head else {}),
                     **_style_of(cell, cw_)}
            para = {**t_para, **({k: header[k] for k in ("align", "bullet", "line_spacing", "space_before",
                                                          "space_after") if k in header} if is_head else {}),
                    **{k: cell[k] for k in ("align", "bullet", "line_spacing", "space_before", "space_after")
                       if k in cell}}
            paras = paragraphs(cell.get("text"), cell.get("markup", markup), cw_)
            treqs, spans = text_requests(oid, paras, style, para, cell=(r, c))
            reqs += treqs
            if "fill" in cell or "valign" in cell:
                fill_req(r, c, 1, 1, cell.get("fill"), cell.get("valign"), cw_)
            if treqs:
                width = merged_width.get((r, c), cw[c]) - 2 * CELL_PAD_X
                need = estimate_height_in(spans, width) + 2 * CELL_PAD_Y
                row_need[r] = max(row_need[r], need)
    reqs += fill_reqs
    borders = el.get("borders")
    if borders is not None:
        for b in (borders if isinstance(borders, list) else [borders]):
            if isinstance(b, str):
                b = {"color": b} if b.lower() != "none" else {"none": True}
            if not isinstance(b, dict):
                raise SpecError(f"{where}: borders must be a colour, 'none', a mapping or a list of them")
            _check_keys(b, {"color", "weight", "dash", "position", "none"}, f"{where} borders")
            none = b.get("none")
            props = {"tableBorderFill": {"solidFill": {"color": parse_color(b.get("color", "#000000")),
                                                       "alpha": 0.0 if none else 1.0}},
                     "weight": {"magnitude": float(b.get("weight", 1)), "unit": "PT"},
                     "dashStyle": str(b.get("dash", "SOLID")).upper()}
            reqs.append({"updateTableBorderProperties": {
                "objectId": oid, "borderPosition": str(b.get("position", "ALL")).upper(),
                "tableBorderProperties": props,
                "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight,dashStyle"}})
    heights = [max(row_need[r], (rh[r] if rh else 0)) for r in range(nr)]
    total = sum(heights)
    if total > h * 1.02:
        grown = [f"r{r}" for r in range(nr) if rh and row_need[r] > rh[r] * 1.02]
        warnings.append(f"{el['id']}: rows grow to fit their text: the table will be ~{total:.2f} in tall, "
                        f"not {h:g} in" + (f" (rows {', '.join(grown)})" if grown else ""))
    return reqs, w, max(total, h)


# ------------------------------------------------------------------------------ API plumbing
def _slides_api():
    return _google.build("slides", "v1", [_google.DRIVE], subject=_google.impersonation_subject())


def _drive_api():
    return _google.build("drive", "v3", [_google.DRIVE], subject=_google.impersonation_subject())


def _fail(e, pid=None, write=False):
    """Explain a Google error and exit non-zero. `write`: the error came from a batchUpdate."""
    name = type(e).__name__
    if name == "RefreshError" or "unauthorized_client" in str(e):
        cid = _google.client_id() or "<service-account client id>"
        subj = _google.impersonation_subject()
        print(f"AUTH ERROR: {e}")
        if subj:
            print(f"Domain-wide delegation must authorise client id {cid} for the scope "
                  f"{_google.DRIVE} (Workspace Admin > Security > API controls > Domain-wide "
                  f"delegation). Impersonating {subj}.")
        sys.exit(1)
    status, detail = _google.http_error(e)
    if str(status) in ("403", "404"):
        subj = _google.impersonation_subject()
        who = subj or _google.client_email() or "the service account"
        print(f"NO ACCESS [{status}]: {detail}")
        print(f"Fix: share the presentation with {who} (Editor to write, Viewer to read), then re-run.")
    elif str(status) == "400" and "revision" in detail.lower():
        print(f"REVISION MISMATCH [{status}]: {detail}")
        print("The deck changed after it was read (someone else is editing). Nothing was written. "
              "Re-run `outline`, check your requests still fit, then try again.")
        sys.exit(3)
    else:
        print(f"slides API error [{status}]: {_clip(str(detail), 300)}")
        if write and str(status) not in ("400", "401", "403", "404"):
            print("This write may or may not have been applied. Run `outline` to see the deck as it is "
                  "now before trying again (a retry is still revision-guarded).")
    sys.exit(1)


def fetch(slides, pid):
    try:
        return slides.presentations().get(presentationId=pid).execute(num_retries=RETRIES)
    except Exception as e:
        _fail(e, pid)


def send(slides, pres, requests, revision=None, dry=False, label="batchUpdate"):
    """One guarded batchUpdate. The required revision is the fresh read's, and must equal
    `revision` when the caller passes one (the revision its plan was made against)."""
    if not requests:
        print(f"{label}: nothing to do")
        return None
    rev = pres["revisionId"]
    if revision and revision != rev:
        print(f"REVISION MISMATCH: you planned against revision {revision}, the deck is now at {rev}. "
              f"Nothing was written. Re-run `outline` and re-check.")
        sys.exit(3)
    body = {"requests": requests, "writeControl": {"requiredRevisionId": rev}}
    if dry:
        print(json.dumps(body, indent=1, ensure_ascii=False))
        print(f"-- dry run: {len(requests)} request(s), nothing sent", file=sys.stderr)
        return None
    try:
        # no automatic retry: a write whose outcome is unknown is reported, not repeated
        resp = slides.presentations().batchUpdate(presentationId=pres["presentationId"], body=body).execute()
    except Exception as e:
        _fail(e, pres["presentationId"], write=True)
    new_rev = (resp.get("writeControl") or {}).get("requiredRevisionId")
    print(f"{label}: {len(requests)} request(s) applied; revision {rev} -> {new_rev}")
    return resp


def _write_args(args):
    return opt(args, "--revision"), flag(args, "--dry-run")


def _deck(args, verb):
    pos = positional(args, VALUE_OPTS)
    if not pos:
        raise SpecError(f"{verb} needs <deck>")
    return deck_id(pos[0]), pos[1:]


VALUE_OPTS = ("--slides", "--out", "--size", "--revision", "--at", "--to", "--id", "--layout", "--master",
              "--text", "--file", "--find", "--with", "--title")


# ------------------------------------------------------------------------------ verbs
def cmd_check(args):
    pid, _ = _deck(args, "check")
    pres = fetch(_slides_api(), pid)
    d = outline(pres, numbers=[])
    print(f"OK - can read {pid}\ntitle: {d['title']!r}\nrevision: {d['revision']}\n"
          f"size: {d['size_in'][0]:g} x {d['size_in'][1]:g} in\nslides: {d['slide_count']}")


def cmd_outline(args):
    pid, _ = _deck(args, "outline")
    pres = fetch(_slides_api(), pid)
    d = outline(pres, slide_numbers(pres, opt(args, "--slides")), full=flag(args, "--full"))
    if flag(args, "--json"):
        print(json.dumps(d, indent=1, ensure_ascii=False))
    else:
        print(render_outline(d))


def cmd_layouts(args):
    pid, _ = _deck(args, "layouts")
    pres = fetch(_slides_api(), pid)
    for m in masters_layouts(pres):
        print(f"master {m['id']} {m['name']!r}")
        for l in m["layouts"]:
            ph = ", ".join(l["placeholders"]) or "no placeholders"
            used = f"  used by {l['used_by']} slide(s)" if l["used_by"] else ""
            print(f"  {l['id']:<8} {l['display']!r} (name {l['name']!r})  [{ph}]{used}")


def cmd_thumb(args):
    pid, rest = _deck(args, "thumb")
    if not rest:
        raise SpecError("thumb needs <slides> (e.g. 27 or 26-28)")
    size = (opt(args, "--size", "LARGE") or "LARGE").upper()
    if size not in ("LARGE", "MEDIUM", "SMALL"):
        raise SpecError("--size must be LARGE, MEDIUM or SMALL")
    slides = _slides_api()
    pres = fetch(slides, pid)
    nums = slide_numbers(pres, rest[0])
    out = opt(args, "--out")
    import urllib.request
    for n in nums:
        s = pres["slides"][n - 1]
        if out and len(nums) == 1 and not Path(out).is_dir() and out.lower().endswith(".png"):
            dest = Path(out)
        else:
            dest = Path(out or ".") / f"slide-{n:02d}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            th = slides.presentations().pages().getThumbnail(
                presentationId=pid, pageObjectId=s["objectId"],
                thumbnailProperties_mimeType="PNG", thumbnailProperties_thumbnailSize=size).execute(
                num_retries=RETRIES)
        except Exception as e:
            _fail(e, pid)
        with urllib.request.urlopen(th["contentUrl"], timeout=60) as r:
            dest.write_bytes(r.read())
        print(f"slide {n} ({s['objectId']}): {dest}  {th.get('width')}x{th.get('height')}")


def cmd_pdf(args):
    pid, _ = _deck(args, "pdf")
    drive = _drive_api()
    try:
        data = drive.files().export(fileId=pid, mimeType=MIME_PDF).execute(num_retries=RETRIES)
    except Exception as e:
        _fail(e, pid)
    dest = Path(opt(args, "--out") or f"{pid}.pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"wrote {dest} ({len(data) // 1024} KB)")


def load_requests(path):
    raw = _read_input(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SpecError(f"{path}: invalid JSON: {e}")
    if isinstance(data, dict):
        if "writeControl" in data:
            print("note: the file's writeControl is ignored; the revision comes from a fresh read",
                  file=sys.stderr)
        data = data.get("requests")
    if not isinstance(data, list) or not all(isinstance(r, dict) and len(r) == 1 for r in data):
        raise SpecError(f"{path}: expected a list of requests (each a one-key mapping) or {{\"requests\": [...]}}")
    return data


def cmd_apply(args):
    pid, rest = _deck(args, "apply")
    if not rest:
        raise SpecError("apply needs <requests.json> (or - for stdin)")
    reqs = load_requests(rest[0])
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    resp = send(slides, pres, reqs, revision, dry, "apply")
    if resp and flag(args, "--replies"):
        print(json.dumps(resp.get("replies", []), indent=1))


def _skip_verb(args, skipped):
    verb = "hide" if skipped else "unhide"
    pid, rest = _deck(args, verb)
    if not rest:
        raise SpecError(f"{verb} needs <slides>")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    nums = slide_numbers(pres, ",".join(rest))
    reqs = [req_skip(pres["slides"][n - 1]["objectId"], skipped) for n in nums]
    if send(slides, pres, reqs, revision, dry, verb):
        print(f"{'hidden' if skipped else 'shown'}: slide(s) {', '.join(map(str, nums))}")


def cmd_hide(args):
    _skip_verb(args, True)


def cmd_unhide(args):
    _skip_verb(args, False)


def cmd_duplicate(args):
    pid, rest = _deck(args, "duplicate")
    if not rest:
        raise SpecError("duplicate needs <slide>")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    i = slide_index(pres, rest[0])
    src = pres["slides"][i]["objectId"]
    nid = check_object_id(opt(args, "--id") or new_id("dup"), "--id")
    reqs = [{"duplicateObject": {"objectId": src, "objectIds": {src: nid}}}]
    final = i + 1
    if opt(args, "--at"):
        pres_after = {"slides": pres["slides"] + [None]}           # the copy adds one slide
        final = position(pres_after, opt(args, "--at"), allow_end=False)
        if final != i + 1:
            reqs.append(req_move([nid], move_insertion_index(i + 1, final)))
    if send(slides, pres, reqs, revision, dry, "duplicate"):
        print(f"duplicated slide {i + 1} -> slide {final + 1} (id {nid})")


def cmd_move(args):
    pid, rest = _deck(args, "move")
    to = opt(args, "--to")
    if not rest or not to:
        raise SpecError("move needs <slide> --to N")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    i = slide_index(pres, rest[0])
    final = position(pres, to, allow_end=False)
    if final == i:
        print(f"slide {i + 1} is already there")
        return
    reqs = [req_move([pres["slides"][i]["objectId"]], move_insertion_index(i, final))]
    if send(slides, pres, reqs, revision, dry, "move"):
        print(f"moved slide {i + 1} -> slide {final + 1}")


def cmd_insert(args):
    pid, _rest = _deck(args, "insert")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    ins = position(pres, opt(args, "--at", len(pres.get("slides", [])) + 1))
    layout = resolve_layout(pres, opt(args, "--layout"), opt(args, "--master"), near=ins - 1 if ins else 0)
    nid = check_object_id(opt(args, "--id") or new_id("s"), "--id")
    if send(slides, pres, [req_create_slide(layout, ins, nid)], revision, dry, "insert"):
        print(f"inserted slide {ins + 1} (id {nid}, layout {layout} "
              f"{_layout_names(pres).get(layout)!r})")


def cmd_notes(args):
    pid, rest = _deck(args, "notes")
    if not rest:
        raise SpecError("notes needs <slide> and --text T, --file F or --clear")
    if opt(args, "--text") is not None:
        text = opt(args, "--text")
    elif opt(args, "--file"):
        text = _read_input(opt(args, "--file"))
    elif flag(args, "--clear"):
        text = ""
    else:
        raise SpecError("notes needs --text T, --file F or --clear")
    text = text.replace("\r\n", "\n").rstrip("\n")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    i = slide_index(pres, rest[0])
    if send(slides, pres, reqs_notes(pres["slides"][i], text), revision, dry, "notes"):
        print(f"speaker notes of slide {i + 1} {'cleared' if not text else 'set'}")


def cmd_replace(args):
    pid, _rest = _deck(args, "replace")
    find, repl = opt(args, "--find"), opt(args, "--with")
    if not find or repl is None:
        raise SpecError("replace needs --find T --with R")
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    body = {"containsText": {"text": find, "matchCase": flag(args, "--match-case")}, "replaceText": repl}
    if opt(args, "--slides"):
        body["pageObjectIds"] = [pres["slides"][n - 1]["objectId"] for n in slide_numbers(pres, opt(args, "--slides"))]
    resp = send(slides, pres, [{"replaceAllText": body}], revision, dry, "replace")
    if resp:
        n = ((resp.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged", 0)
        print(f"replaced {n} occurrence(s)")


def cmd_build(args):
    pid, rest = _deck(args, "build")
    if not rest:
        raise SpecError("build needs <spec.yaml|spec.json>")
    spec = load_spec(rest[0])
    revision, dry = _write_args(args)
    slides = _slides_api()
    pres = fetch(slides, pid)
    reqs, warnings, info = build_requests(pres, spec, rest[0])
    out = opt(args, "--out")
    if out:
        Path(out).write_text(json.dumps(reqs, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {len(reqs)} request(s) to {out}")
    print(f"target: {info['slide']}" + (f"; replacing {info['removed']} element(s) from an earlier build"
                                        if info.get("removed") else ""))
    for w in warnings:
        print(f"WARNING: {w}")
    if dry or out:
        if dry and not out:
            send(slides, pres, reqs, revision, True, "build")
        else:
            print("-- not sent (--out writes the requests only; run `apply` on the file to send them)")
        return
    if send(slides, pres, reqs, revision, False, "build"):
        print(f"built {len(info['created'])} element(s) on {info['slide']}: "
              f"{', '.join(info['created'])}")
        if info.get("notes_after") is not None:
            # a second, separately guarded write: only the new slide's own notes
            fresh = fetch(slides, pid)
            i = slide_index(fresh, info["slide_id"])
            send(slides, fresh, reqs_notes(fresh["slides"][i], info["notes_after"]), None, False, "notes")
        print(f"check it: bizconnect slides thumb {pid} {info['slide_id']} --out <file.png>")


def cmd_create(args):
    title = opt(args, "--title")
    if not title:
        raise SpecError("create needs --title T")
    try:
        p = _slides_api().presentations().create(body={"title": title}).execute()
    except Exception as e:
        _fail(e)
    pid = p["presentationId"]
    print(f"created {title!r}\n  id:  {pid}\n  url: https://docs.google.com/presentation/d/{pid}/edit")


VERBS = {"check": cmd_check, "outline": cmd_outline, "layouts": cmd_layouts, "thumb": cmd_thumb,
         "thumbnail": cmd_thumb, "pdf": cmd_pdf, "apply": cmd_apply, "build": cmd_build, "hide": cmd_hide,
         "unhide": cmd_unhide, "duplicate": cmd_duplicate, "move": cmd_move, "insert": cmd_insert,
         "notes": cmd_notes, "replace": cmd_replace, "create": cmd_create}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown slides verb {verb!r}. One of: {', '.join(VERBS)}")
    try:
        fn(rest)
    except SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
