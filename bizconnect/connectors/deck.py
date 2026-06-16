"""deck — render a slide deck from per-section slide-specs + a PowerPoint template.

The build-by-section companion to `compose`: compose's `slide` stage produces one
slide-spec JSON per section (-> <slides_dir>/<id>.slide.json); `deck` assembles them
into a .pptx from a template and renders previews. **Content-free**: the template, the
archetype->template-slide map, the field->shape map, and the slide order all live in the
consuming repo's `deck.yaml` — this module hardcodes no layout, shape name, or content.

COM SAFETY (Windows + PowerPoint) — the repo CLAUDE.md hard rule, honoured exactly:
  * attach to a RUNNING PowerPoint via GetActiveObject and NEVER quit it;
  * only Quit() an instance WE spawned, only Close() OUR own presentation;
  * open the template READ-ONLY and SaveAs a copy, so the template is never mutated;
  * never kill/terminate/taskkill any Office process, and never touch the user's
    other open documents.

Verbs
  build      slide-specs + template -> <output> deck.pptx     (single COM pass)
  preview    <output> deck.pptx -> per-slide PNGs (+ optional PDF)
  status     FRESH/STALE/MISSING per slide vs the slide-spec hashes
  push       publish the built .pptx (+ PDF) to Drive, log a docs-registry row,
             and ATTACH the .pptx onto that row's Notion page (no COM/PowerPoint)

Driven by `deck.yaml` (found by walking up from the cwd, like pipeline.yaml). The COM
build/preview run on Windows with PowerPoint installed; status/push validation of the
field->shape mapping is finalised against the real template (the consuming repo's deck.yaml).

deck.yaml schema
----------------
  template: 05.deck/template.pptx          # the slide-template formats (REQUIRED)
  output:   final/deck.pptx                 # assembled deck (default final/deck.pptx)
  slides_dir: slides                        # where <id>.slide.json live (default "slides")
  assets_dir: 05.deck/assets                # generated PNGs (default 05.deck/assets)
  order: [S01, S02, ...]                    # optional explicit slide order; else sorted by id
  preview: {width: 1920, height: 1080, pdf: true, dir: final/previews, pdf_path: final/deck.pdf}
  archetypes:                               # one entry per slide archetype (REQUIRED)
    bullets:
      source_slide: "Bullets"               # the example slide's Name in template.pptx
      fields:                               # slide-spec field -> template shape name
        headline: "Title"
        subhead:  "Subtitle"
        claims:   "Body"                    # a list fills as bullet paragraphs
    chart:
      source_slide: "Chart"
      fields:   {headline: "Title", takeaway: "Takeaway"}
      anchors:  {chart: "ChartArea"}        # native object placed at this shape's box,
                                            # which is then deleted (anchor = kind -> shape)
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DECK_NAME = "deck.yaml"

# PowerPoint COM enums (mirrors projects/pptx-pipeline + projects/vdr).
MSO_TRUE, MSO_FALSE = -1, 0
PP_PDF, INTENT_PRINT, OUT_SLIDES = 2, 2, 1

# XlChartType values (Office shared enum) used by Shapes.AddChart2. We keep a small,
# explicit map and degrade to a clustered column for anything unmapped.
XL_COLUMN_CLUSTERED = 51
XL_COLUMN_STACKED = 52
XL_LINE_MARKERS = 65
XL_PIE = 5
XL_XY_SCATTER = -4169
CHART_TYPE = {
    "bar": XL_COLUMN_CLUSTERED,
    "stacked-bar": XL_COLUMN_STACKED,
    "stacked_bar": XL_COLUMN_STACKED,
    "line": XL_LINE_MARKERS,
    "pie": XL_PIE,
    "scatter": XL_XY_SCATTER,
}

# A slide-spec must carry at least these; archetype must be declared in deck.yaml.
REQUIRED_SPEC_KEYS = ("id", "archetype", "headline")

# Per-archetype required payload (spec §1): archetype -> the spec field that must be a
# non-empty dict/list. Archetypes not listed here carry no required structured payload
# (e.g. divider, quote, bullets, exec-summary fill text fields only).
ARCHETYPE_PAYLOAD = {
    "chart": "chart",
    "table": "table",
    "matrix": "matrix",
    "metric-panel": "metrics",
    "image": "image",
    "map": "image",
    "diagram": "image",
    "timeline": "timeline",
    "two-column": "two_column",
}


def _yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("ruamel.yaml missing — run via the launcher (scripts/bizconnect.py "
                 "bootstraps the central-store venv).")
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    return y


class Cfg:
    """deck.yaml, found by walking up from the cwd; paths resolve from its directory."""

    def __init__(self, start=None):
        d = Path(start or Path.cwd()).resolve()
        f = next((c / DECK_NAME for c in [d, *d.parents] if (c / DECK_NAME).exists()), None)
        if not f:
            sys.exit("no %s found (searched up from %s)." % (DECK_NAME, d))
        with open(f, encoding="utf-8") as fh:
            self.d = _yaml().load(fh) or {}
        self.root = f.parent
        self.file = f

    def g(self, dotted, default=None):
        cur = self.d
        for p in dotted.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    def req(self, key):
        v = self.g(key)
        if v in (None, ""):
            sys.exit("%s: %r is required" % (DECK_NAME, key))
        return v

    def ap(self, rel):
        return self.root / str(rel)

    def sha(self, p):
        p = Path(p)
        return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.exists() else None


# --------------------------------------------------------------- slide-specs
def _payload_present(value):
    """A required structured payload counts as present iff it is a non-empty dict/list."""
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return False


def validate_spec(spec, archetypes):
    errs = []
    if not isinstance(spec, dict):
        return ["not a JSON object"]
    for k in REQUIRED_SPEC_KEYS:
        if not spec.get(k):
            errs.append("missing required field %r" % k)
    arch = spec.get("archetype")
    if arch and arch not in archetypes:
        errs.append("archetype %r not declared in deck.yaml archetypes (have: %s)"
                    % (arch, ", ".join(archetypes) or "none"))
    # Per-archetype payload checks (spec §1): the structured field the archetype renders
    # must be present and non-empty. Unknown archetype or missing payload = hard error,
    # matching today's fail-on-mismatch discipline.
    need = ARCHETYPE_PAYLOAD.get(arch)
    if need and not _payload_present(spec.get(need)):
        errs.append("archetype %r requires a non-empty %r payload" % (arch, need))
    return errs


def load_specs(cfg):
    """Ordered [(id, spec, path)] from <slides_dir>/*.slide.json. Order = deck.yaml `order`
    if given, else the spec ids sorted; ids are the spec `id` field, falling back to filename."""
    sdir = cfg.ap(cfg.g("slides_dir", "slides"))
    if not sdir.exists():
        sys.exit("slides_dir %s does not exist — run `compose run slide all` first" % sdir)
    found = {}
    for p in sorted(sdir.glob("*.slide.json")):
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            sys.exit("%s is not valid JSON (%s)" % (p, e))
        sid = str(spec.get("id") or p.name[: -len(".slide.json")])
        found[sid] = (spec, p)
    if not found:
        sys.exit("no *.slide.json in %s" % sdir)
    order = cfg.g("order") or sorted(found)
    missing = [s for s in order if s not in found]
    if missing:
        sys.exit("deck.yaml order lists ids with no slide-spec: %s" % ", ".join(missing))
    return [(sid, found[sid][0], found[sid][1]) for sid in order if sid in found]


def _manifest_path(cfg):
    return cfg.ap(cfg.g("output", "final/deck.pptx")).with_suffix(".manifest.json")


def _assets_dir(cfg):
    """Where generated PNGs live: <deck.yaml dir>/05.deck/assets (spec §4). Overridable
    via `assets_dir` in deck.yaml for templates not under a 05.deck folder."""
    return cfg.ap(cfg.g("assets_dir", "05.deck/assets"))


# --------------------------------------------------------------- COM helpers
def _attach_or_spawn(win32com, pythoncom):
    """Reuse a running PowerPoint (never disturb the user's app); else spawn one we own."""
    try:
        return win32com.client.GetActiveObject("PowerPoint.Application"), False
    except pythoncom.com_error:
        ppt = win32com.client.Dispatch("PowerPoint.Application")
        ppt.Visible = MSO_TRUE                       # PowerPoint refuses Visible=False
        return ppt, True


def _iter_shapes(shapes):
    """Flat recursive walk so a shape nested inside a group is still reachable by name."""
    for shp in shapes:
        yield shp
        try:
            if shp.Type == 6:                        # msoGroup
                for inner in _iter_shapes(shp.GroupItems):
                    yield inner
        except Exception:
            pass


def _find_shape(slide, name):
    for shp in _iter_shapes(slide.Shapes):
        if shp.Name == name:
            return shp
    return None


def _set_shape_text(shape, value):
    """Fill a shape's text from a spec value. str -> text; list of str -> bullet paragraphs;
    list of {label,value} -> 'label  value' lines; None/'' -> cleared. No-op if no text frame."""
    try:
        if not shape.HasTextFrame:
            return
    except Exception:
        return
    if isinstance(value, (list, tuple)):
        lines = []
        for v in value:
            if isinstance(v, dict):
                lines.append("  ".join(str(x) for x in (v.get("label", ""), v.get("value", "")) if x != ""))
            else:
                lines.append(str(v))
        text = "\r".join(lines)                       # \r = PowerPoint paragraph break
    else:
        text = "" if value is None else str(value)
    shape.TextFrame.TextRange.Text = text


def _fill_slide(slide, spec, fields):
    """Fill each mapped shape from the spec. `fields` is {spec_field: shape_name}."""
    unfilled = []
    for spec_field, shape_name in (fields or {}).items():
        shp = _find_shape(slide, shape_name)
        if shp is None:
            unfilled.append(shape_name)
            continue
        _set_shape_text(shp, spec.get(spec_field))
    # speaker notes (if present) -> the notes page body, by convention.
    notes = spec.get("speaker_notes")
    if notes:
        try:
            for shp in _iter_shapes(slide.NotesPage.Shapes):
                if shp.HasTextFrame and shp.PlaceholderFormat.Type == 2:   # ppPlaceholderBody
                    shp.TextFrame.TextRange.Text = str(notes)
                    break
        except Exception:
            pass
    return unfilled


# ---------------------------------------------------------- anchored inserts
# Each _render_* reads the geometry (Left/Top/Width/Height) of a named placeholder
# shape (the "anchor"), inserts a native object there, then deletes the placeholder.
# COM safety is unchanged: we only ever touch OUR own SaveAs'd copy. Image-backed
# fallbacks use the pre-generated PNGs from _deck_assets (the asset pass runs first).


def _anchor_box(slide, anchor_name):
    """Return (shape, (Left, Top, Width, Height)) for the named anchor, or (None, None)."""
    shp = _find_shape(slide, anchor_name)
    if shp is None:
        return None, None
    try:
        return shp, (float(shp.Left), float(shp.Top), float(shp.Width), float(shp.Height))
    except Exception:
        return shp, None


def _delete_shape(shape):
    try:
        shape.Delete()
    except Exception:
        pass


def _add_picture(slide, box, path):
    """AddPicture `path` into `box` (Left, Top, Width, Height). Returns the shape or None."""
    if not path or not Path(path).exists():
        return None
    left, top, width, height = box
    return slide.Shapes.AddPicture(FileName=str(path), LinkToFile=MSO_FALSE,
                                   SaveWithDocument=MSO_TRUE,
                                   Left=left, Top=top, Width=width, Height=height)


def _render_chart(slide, box, chart, asset_path=None):
    """Insert a native PowerPoint chart (AddChart2) at `box`, populating its data workbook
    from `chart{}`. Falls back to AddPicture(asset_path) when render=='image' or AddChart2
    fails. `chart` = {type, categories, series:[{name,data}], unit, note, render}."""
    left, top, width, height = box
    if str(chart.get("render") or "native") == "image":
        return _add_picture(slide, box, asset_path)

    xl_type = CHART_TYPE.get(str(chart.get("type") or "bar").lower(), XL_COLUMN_CLUSTERED)
    try:
        gshape = slide.Shapes.AddChart2(-1, xl_type, left, top, width, height)
    except Exception:
        return _add_picture(slide, box, asset_path)         # AddChart2 unavailable -> image

    try:
        chartobj = gshape.Chart
        _write_chart_data(chartobj, chart)
        note = chart.get("note")
        if note:
            try:
                chartobj.HasTitle = MSO_TRUE
                chartobj.ChartTitle.Text = str(note)
            except Exception:
                pass
        return gshape
    except Exception:
        # Populating the data workbook failed — drop the empty chart and try the image.
        _delete_shape(gshape)
        return _add_picture(slide, box, asset_path)


def _col_letter(n):
    """1-based column index -> Excel column letters (1->A, 27->AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _write_chart_data(chartobj, chart):
    """Write categories + series into a chart's backing Excel workbook, then set the
    SetSourceData range. Closes the workbook we opened; never touches the user's Excel."""
    cats = [str(c) for c in (chart.get("categories") or [])]
    series = [s for s in (chart.get("series") or []) if isinstance(s, dict)]
    nrows, ncols = len(cats), len(series)

    cd = chartobj.ChartData
    cd.Activate()                                    # opens the embedded data workbook
    wb = cd.Workbook
    try:
        ws = wb.Worksheets(1)
        ws.UsedRange.Clear()
        ws.Cells(1, 1).Value = ""
        for j, s in enumerate(series, start=2):       # series names across row 1
            ws.Cells(1, j).Value = str(s.get("name", "Series %d" % (j - 1)))
        for i, cat in enumerate(cats, start=2):       # categories down column A
            ws.Cells(i, 1).Value = cat
        for j, s in enumerate(series, start=2):
            data = s.get("data") or []
            for i in range(nrows):
                val = data[i] if i < len(data) else None
                try:
                    ws.Cells(i + 2, j).Value = float(val) if val is not None else None
                except (TypeError, ValueError):
                    ws.Cells(i + 2, j).Value = None
        rng = "%s!$A$1:$%s$%d" % (ws.Name, _col_letter(ncols + 1), nrows + 1)
        try:
            chartobj.SetSourceData(Source=rng)
        except Exception:
            pass
    finally:
        try:
            wb.Application.Quit()                      # close ONLY this embedded data app
        except Exception:
            pass


def _render_table(slide, box, table):
    """Insert an AddTable grid at `box` filled with headers + rows; header row bold.
    `table` = {headers:[...], rows:[[...]], note}."""
    headers = [str(h) for h in (table.get("headers") or [])]
    rows = table.get("rows") or []
    ncols = max([len(headers)] + [len(r) for r in rows] or [1]) or 1
    nrows = len(rows) + (1 if headers else 0)
    nrows = max(nrows, 1)

    left, top, width, height = box
    gshape = slide.Shapes.AddTable(nrows, ncols, left, top, width, height)
    tbl = gshape.Table

    def _cell(r, c, text):
        try:
            cell = tbl.Cell(r, c)
            cell.Shape.TextFrame.TextRange.Text = "" if text is None else str(text)
            return cell
        except Exception:
            return None

    r0 = 1
    if headers:
        for c in range(ncols):
            cell = _cell(1, c + 1, headers[c] if c < len(headers) else "")
            if cell is not None:
                try:
                    cell.Shape.TextFrame.TextRange.Font.Bold = MSO_TRUE
                except Exception:
                    pass
        r0 = 2
    for i, row in enumerate(rows):
        for c in range(ncols):
            _cell(r0 + i, c + 1, row[c] if c < len(row) else "")
    return gshape


def _render_image(slide, box, image, asset_path=None):
    """AddPicture the asset at `box`. Prefers the pre-generated/resolved asset_path; else
    treats image.spec as a path. Returns the picture shape or None (no asset available)."""
    path = asset_path
    if not (path and Path(str(path)).exists()):
        spec_path = image.get("spec")
        path = spec_path if spec_path and Path(str(spec_path)).exists() else None
    return _add_picture(slide, box, path) if path else None


def _render_matrix(slide, box, matrix, asset_path=None):
    """render=='image' -> AddPicture the generated matrix asset; render=='table' (default)
    -> an AddTable grid with a header row (cols) and header column (rows) + cell glyphs.
    `matrix` = {rows:[...], cols:[...], cells:[[glyph,..]], legend:{glyph:meaning}, render}."""
    if str(matrix.get("render") or "table") == "image":
        return _add_picture(slide, box, asset_path)

    rows = [str(r) for r in (matrix.get("rows") or [])]
    cols = [str(c) for c in (matrix.get("cols") or [])]
    cells = matrix.get("cells") or []
    nrows, ncols = len(rows) + 1, len(cols) + 1      # +1 for header row/col
    nrows, ncols = max(nrows, 1), max(ncols, 1)

    left, top, width, height = box
    gshape = slide.Shapes.AddTable(nrows, ncols, left, top, width, height)
    tbl = gshape.Table

    def _cell(r, c, text, bold=False):
        try:
            tr = tbl.Cell(r, c).Shape.TextFrame.TextRange
            tr.Text = "" if text is None else str(text)
            if bold:
                tr.Font.Bold = MSO_TRUE
        except Exception:
            pass

    for j, col in enumerate(cols):                    # header row
        _cell(1, j + 2, col, bold=True)
    for i, row in enumerate(rows):                    # header column + cells
        _cell(i + 2, 1, row, bold=True)
        src = cells[i] if i < len(cells) else []
        for j in range(len(cols)):
            _cell(i + 2, j + 2, src[j] if j < len(src) else "")
    return gshape


_RENDERERS = {
    "chart": _render_chart,
    "table": _render_table,
    "matrix": _render_matrix,
    "image": _render_image,
}


def _render_anchors(slide, spec, anchors, assets):
    """For each declared anchor {kind: shape_name}, read the placeholder's geometry, insert
    the matching native object, then delete the placeholder. `assets` is the {kind: path}
    map this spec's pre-generated assets (from _deck_assets.generate_assets). Returns the
    list of anchor shape names that were declared but not found on the slide."""
    missing = []
    for kind, shape_name in (anchors or {}).items():
        renderer = _RENDERERS.get(kind)
        payload = spec.get(kind)
        if renderer is None or not isinstance(payload, dict):
            continue
        anchor, box = _anchor_box(slide, shape_name)
        if anchor is None or box is None:
            missing.append(shape_name)
            continue
        asset_path = (assets or {}).get(kind)
        try:
            if kind in ("chart", "matrix", "image"):
                renderer(slide, box, payload, asset_path=asset_path)
            else:
                renderer(slide, box, payload)
        except Exception as e:
            sys.stderr.write("[deck] %s anchor %r render failed: %s\n"
                             % (kind, shape_name, e))
        finally:
            _delete_shape(anchor)                     # remove the placeholder either way
    return missing


# --------------------------------------------------------------- verbs
def cmd_build(cfg, args):
    """Assemble <output> from the template + slide-specs in ONE COM pass.

    Strategy (validated against the real template in Phase D): open the template
    READ-ONLY then SaveAs the output (template stays pristine); for each spec, Duplicate
    its archetype's `source_slide`, move the copy to the end, and fill its named shapes;
    finally delete the original example slides, leaving only built slides. Non-archetype
    template slides (e.g. a fixed title) are preserved at the front in template order."""
    import pythoncom
    import win32com.client

    template = cfg.ap(cfg.req("template"))
    if not template.exists():
        sys.exit("template not found: %s" % template)
    output = cfg.ap(cfg.g("output", "final/deck.pptx"))
    archetypes = cfg.g("archetypes", {}) or {}
    specs = load_specs(cfg)

    errs = []
    for sid, spec, _p in specs:
        errs += ["%s: %s" % (sid, e) for e in validate_spec(spec, archetypes)]
    if errs:
        sys.exit("slide-spec validation failed:\n  - " + "\n  - ".join(errs))

    # Asset pass (pure Python, NO COM) BEFORE the COM pass: render image-backed charts,
    # the matrix grid, and resolve map/diagram/image assets to PNGs the COM renderer
    # AddPictures by path. Failures here are logged and degrade gracefully at render time.
    from . import _deck_assets
    assets_dir = _assets_dir(cfg)
    assets = _deck_assets.generate_assets(specs, assets_dir)

    output.parent.mkdir(parents=True, exist_ok=True)
    unfilled_all = {}
    pythoncom.CoInitialize()
    try:
        ppt, spawned = _attach_or_spawn(win32com, pythoncom)
        pres = ppt.Presentations.Open(str(template), ReadOnly=MSO_TRUE,
                                       Untitled=MSO_FALSE, WithWindow=MSO_FALSE)
        try:
            pres.SaveAs(str(output))                 # now editing the COPY; template untouched
            name_to_slide = {s.Name: s for s in pres.Slides}
            example_ids = []
            for sid, spec, _p in specs:
                src_name = archetypes[spec["archetype"]].get("source_slide")
                src = name_to_slide.get(src_name)
                if src is None:
                    raise SystemExit(
                        "template slide %r (archetype %r) not found. Template slides: %s"
                        % (src_name, spec["archetype"], ", ".join(name_to_slide) or "(none named)"))
                if src.SlideID not in example_ids:
                    example_ids.append(src.SlideID)
                src.Duplicate()                       # copy appears at src.SlideIndex + 1
                dup = pres.Slides(src.SlideIndex + 1)
                dup.MoveTo(pres.Slides.Count)         # push to the end (originals keep low indices)
                dup = pres.Slides(pres.Slides.Count)
                arch_cfg = archetypes[spec["archetype"]]
                u = _fill_slide(dup, spec, arch_cfg.get("fields", {}))
                # Anchored native inserts (charts/tables/matrix/images): read each anchor
                # placeholder's geometry, insert, delete the placeholder.
                u += _render_anchors(dup, spec, arch_cfg.get("anchors", {}),
                                     assets.get(sid, {}))
                if u:
                    unfilled_all[sid] = u
            for sid_ in example_ids:                  # remove the template example slides
                try:
                    pres.Slides.FindBySlideID(sid_).Delete()
                except Exception:
                    pass
            pres.Save()
        finally:
            try:
                pres.Close()                          # close only OUR handle
            except Exception:
                pass
            if spawned:
                ppt.Quit()                            # quit only an instance WE created
    finally:
        pythoncom.CoUninitialize()

    _manifest_path(cfg).write_text(
        json.dumps({sid: cfg.sha(p) for sid, _s, p in specs}, indent=2), encoding="utf-8")
    print("built %s (%d slide(s))" % (output, len(specs)))
    for sid, names in unfilled_all.items():
        print("  ⚠ %s: shape(s) not found in template: %s" % (sid, ", ".join(names)))


def cmd_preview(cfg, args):
    """Export each slide of <output> to PNG (and optionally the whole deck to PDF)."""
    import pythoncom
    import win32com.client

    deck = cfg.ap(cfg.g("output", "final/deck.pptx"))
    if not deck.exists():
        sys.exit("no deck at %s — run `deck build` first" % deck)
    pv = cfg.g("preview", {}) or {}
    w, h = int(pv.get("width", 1920)), int(pv.get("height", 1080))
    outdir = cfg.ap(pv.get("dir", "final/previews"))
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    pythoncom.CoInitialize()
    try:
        ppt, spawned = _attach_or_spawn(win32com, pythoncom)
        for p in list(ppt.Presentations):            # close our own copy if already open (unlock)
            try:
                if Path(p.FullName).resolve() == deck.resolve():
                    p.Close()
            except Exception:
                pass
        pres = ppt.Presentations.Open(str(deck), ReadOnly=MSO_TRUE, WithWindow=MSO_FALSE)
        try:
            n = pres.Slides.Count
            for i in range(1, n + 1):
                pres.Slides(i).Export(str(outdir / ("slide-%02d.png" % i)), "PNG", w, h)
            if bool(pv.get("pdf", True)):
                pres.ExportAsFixedFormat(str(cfg.ap(pv.get("pdf_path", "final/deck.pdf"))),
                                         PP_PDF, INTENT_PRINT, MSO_FALSE, 1,
                                         OUT_SLIDES, MSO_FALSE, None)
        finally:
            try:
                pres.Close()
            except Exception:
                pass
            if spawned:
                ppt.Quit()
    finally:
        pythoncom.CoUninitialize()
    print("previewed %d slide(s) -> %s%s" % (n, outdir, " (+ PDF)" if pv.get("pdf", True) else ""))


def cmd_status(cfg, args):
    """FRESH/STALE/MISSING per slide: compares each spec's current hash to the deck manifest."""
    specs = load_specs(cfg)
    deck = cfg.ap(cfg.g("output", "final/deck.pptx"))
    man = {}
    mp = _manifest_path(cfg)
    if mp.exists():
        man = json.loads(mp.read_text(encoding="utf-8"))
    icon = {"FRESH": "✓", "STALE": "~", "MISSING": "·"}
    deck_missing = not deck.exists()
    print("\ndeck: %s%s" % (deck, "  (NOT built)" if deck_missing else ""))
    for sid, _spec, p in specs:
        if deck_missing or sid not in man:
            st = "MISSING"
        elif man.get(sid) != cfg.sha(p):
            st = "STALE"
        else:
            st = "FRESH"
        print("  %s %-12s %s" % (icon[st], sid, st))
    print()


# --------------------------------------------------------------- push (publish)
# COM-SAFE: `push` operates ONLY on the already-built files on disk (the .pptx from
# `build` and the .pdf from `preview`). It never opens PowerPoint, never drives COM,
# and never touches any Office process — it uploads bytes to Drive and Notion.

def _push_opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _git_sha(root):
    """Short git sha of `root`'s HEAD, or "" (mirrors gdocs._git_sha — kept local so
    deck.py stays self-contained and never edits another connector's file)."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _folder_id(s):
    """Extract a Drive folder id from a folder URL or pass an id through (gdocs pattern)."""
    if not s:
        return None
    import re
    s = str(s).strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", s)
    return m.group(1) if m else s


def _content_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _drive_upload(drive, path, mime, folder, name=None):
    """Create a Drive file from `path`. Returns (file_id, webViewLink). Reuses the
    gdocs Drive-upload pattern (files().create + supportsAllDrives); streams bytes
    from disk via MediaFileUpload (a .pptx is binary, not in-memory Markdown)."""
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
    body = {"name": name or Path(path).name}
    if folder:
        body["parents"] = [folder]
    f = drive.files().create(body=body, media_body=media,
                             fields="id,name,webViewLink",
                             supportsAllDrives=True).execute()
    fid = f["id"]
    url = f.get("webViewLink") or "https://drive.google.com/file/d/%s/view" % fid
    return fid, url


def _maybe_share(drive, file_id, data, folder):
    """Share a newly-created Drive file back to a human (else it's stranded in the SA's
    Drive). Mirrors gdocs._maybe_share; kept local so we don't import another connector's
    private helper. No-op when the file already lives in a shared folder."""
    from .. import config
    share = config.get_path(data, "google.share_with")
    if not share:
        if not folder:
            print("  WARNING: new Drive file is owned by the service account and not shared.\n"
                  "  Set google.share_with (your email) or google.drive_folder in connections.yaml.")
        return
    try:
        drive.permissions().create(
            fileId=file_id, sendNotificationEmail=False,
            body={"type": "user", "role": "writer", "emailAddress": share},
            fields="id", supportsAllDrives=True).execute()
        print("  shared with %s (Editor)" % share)
    except Exception as e:
        from .. import _google
        status, detail = _google.http_error(e)
        print("  WARN could not share with %s [%s]: %s" % (share, status, detail))


def _docreg_row_page_id(data, artifact):
    """The Notion page id of the docs-registry's CURRENT row for `artifact`, or None.
    `log_instance` doesn't return the page id, so we look the row up by artifact via the
    docs_registry DB (reusing docreg._query/_row_get/_db — read-only)."""
    from . import docreg
    if not docreg._db(data).get("database_id"):
        return None
    try:
        rows = docreg._query(data, filt={"property": "Artifact",
                                         "rich_text": {"equals": artifact}})
    except Exception:
        return None
    current = [r for r in rows if docreg._row_get(r, "Status") == "current"]
    chosen = current[0] if current else (rows[-1] if rows else None)
    return chosen.get("id") if chosen else None


def _attach_pptx_to_notion(page_id, pptx, marker, caption):
    """Upload the .pptx and append a downloadable file block to the registry row page.
    Idempotent: if a file block already carries this build's `marker` in its caption, skip.
    Returns a human status string. Never aborts the push — the caller wraps failures."""
    from . import notion
    # Idempotency: scan existing children for our marker so re-pushing the same build
    # doesn't pile up duplicate file blocks on the row page.
    try:
        for b in notion.get_children(page_id):
            if b.get("type") != "file":
                continue
            cap = "".join(r.get("plain_text", "")
                          for r in b.get("file", {}).get("caption", []))
            if marker and marker in cap:
                return "already attached (%s)" % marker
    except Exception:
        pass                                         # can't list -> just try to attach
    fid = notion.upload_file(Path(pptx))
    block = notion.media_block("file", fid, caption)
    notion.attach(page_id, [block])
    return "attached %s" % Path(pptx).name


def cmd_push(cfg, args):
    """Publish the built deck to Drive, log a docs-registry version row, and ATTACH the
    .pptx onto that row's Notion page (so the PowerPoint is downloadable from the doc-output
    page). Operates ONLY on the already-built files — no PowerPoint/COM interaction.

    Requires `deck build` (the .pptx) and `deck preview` (the PDF) to have run first.

      deck push [--folder ID] [--version V] [--new] [--notes T] [--no-pdf] [--no-notion]
    """
    from .. import config
    from .. import _google
    from . import docreg

    # 1) Require the built deck (.pptx) and its PDF (from preview).
    pptx = cfg.ap(cfg.g("output", "final/deck.pptx"))
    pv = cfg.g("preview", {}) or {}
    pdf = cfg.ap(pv.get("pdf_path", "final/deck.pdf"))
    if not pptx.exists():
        sys.exit("no built deck at %s — run `deck build` first." % pptx)
    want_pdf = ("--no-pdf" not in args)
    if want_pdf and not pdf.exists():
        sys.exit("no PDF at %s — run `deck preview` first (or pass --no-pdf to skip it)." % pdf)

    version = _push_opt(args, "--version")
    folder_override = _push_opt(args, "--folder")
    new = "--new" in args
    notes = _push_opt(args, "--notes", "")

    data, conn_path = config.require_connections()
    root = conn_path.parent
    try:
        artifact = pptx.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        artifact = pptx.name
    content_sha = _content_sha256(pptx)
    git_sha = _git_sha(root)

    # 2) Upload the .pptx (and PDF) to Drive (reuse the gdocs/_google Drive path +
    #    connections.yaml google.drive_folder, scoped to the active deliverable).
    drive = _google.build("drive", "v3", [_google.DRIVE],
                          subject=_google.impersonation_subject(data))
    folder = _folder_id(folder_override) or _folder_id(config.scoped(data, "google.drive_folder"))
    suffix = (" — %s" % version) if version else ""
    try:
        pptx_id, pptx_url = _drive_upload(
            drive, pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            folder, name="%s%s.pptx" % (pptx.stem, suffix))
        _maybe_share(drive, pptx_id, data, folder)
        pdf_url = None
        if want_pdf:
            pdf_id, pdf_url = _drive_upload(drive, pdf, "application/pdf", folder,
                                            name="%s%s.pdf" % (pdf.stem, suffix))
            _maybe_share(drive, pdf_id, data, folder)
    except Exception as e:
        status, detail = _google.http_error(e)
        sys.exit("Drive upload failed [%s]: %s" % (status, detail))

    print("pushed deck to Drive:")
    print("  pptx: %s" % pptx_url)
    if pdf_url:
        print("  pdf:  %s" % pdf_url)

    # 3) Log/refresh a docs-registry row for the deck artifact (reuse docreg.log_instance):
    #    version, the Drive URL, git sha, content hash, status=current. No-op if unbound.
    reg_notes = " | ".join(x for x in [notes, ("PDF: %s" % pdf_url) if pdf_url else ""] if x)
    page_id = None
    try:
        msg = docreg.log_instance(data, root, artifact=artifact, doc_id=pptx_id,
                                  doc_url=pptx_url, version=version, content_sha=content_sha,
                                  git_sha=git_sha, notes=reg_notes, new=new)
        if msg:
            print("  registry: %s" % msg)
            page_id = _docreg_row_page_id(data, artifact)
        else:
            print("  registry: no docs registry bound (notion.docs_registry) — "
                  "skipping row + Notion attach. `bizconnect docreg init` to enable.")
    except Exception as e:
        print("  (doc registry not updated: %s)" % e)

    # 4) ATTACH the .pptx onto the docreg row's Notion page so it's downloadable there.
    #    Wrapped so a Notion hiccup (or an oversized .pptx) never loses the Drive upload
    #    or the registry row that already succeeded above.
    if "--no-notion" in args:
        print("  notion: --no-notion -> skipped attaching the .pptx")
    elif page_id:
        marker = "[deck:%s]" % (version or content_sha[:12])
        caption = "Deck %s %s" % (version or "", marker)
        try:
            print("  notion: %s" % _attach_pptx_to_notion(page_id, pptx, marker, caption))
        except SystemExit as e:                      # notion.upload_file size-limit guard
            print("  (notion attach skipped: %s)" % e)
        except Exception as e:
            print("  (notion attach failed: %s)" % e)
    elif docreg._db(data).get("database_id"):
        print("  notion: could not locate the registry row page — .pptx not attached "
              "(it is on Drive; re-run after the row exists).")


VERBS = {"build": cmd_build, "preview": cmd_preview, "status": cmd_status, "push": cmd_push}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit("unknown deck verb %r. One of: %s" % (verb, ", ".join(VERBS)))
    fn(Cfg(), rest)
    return 0
