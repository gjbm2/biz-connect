"""_deck_assets — pure-Python asset generation for the `deck` connector.

Renders the image-backed payloads of slide-specs (charts, the matrix grid, and
maps/diagrams) to PNG files BEFORE the COM pass in deck.cmd_build. This module is
**pure Python**: it imports NO COM / win32com — it runs anywhere (incl. off-Windows,
which is how charts + the matrix grid are smoke-tested without PowerPoint).

Single entry point:

    generate_assets(specs, assets_dir) -> {spec_id: {kind: path}}

`specs` is an iterable of (id, spec, path) tuples (deck.load_specs' shape) OR of raw
spec dicts; `assets_dir` is the directory PNGs are written into (the caller passes
`<deck.yaml dir>/05.deck/assets`). Returned paths are absolute Path objects, keyed by
spec id then by kind ("chart" | "matrix" | "image"). Specs that need no generated asset
contribute nothing to the result.

What is generated, per spec.archetype + payload (matches spec §4):
  * chart  (chart.render == "image")  -> matplotlib bar / stacked-bar / line / pie / scatter
  * matrix (matrix.render == "image") -> matplotlib grid/heatmap of the cell glyphs
  * map  (image.kind == "map")        -> outline/centroid render IF a lightweight bundled
                                          states table is enough; else fall back to a
                                          provided asset path (image.spec) and log it.
  * diagram/screenshot/generated      -> pass through a provided image.spec path; else log
                                          that the asset must be supplied (clean fallback,
                                          no hard dep beyond matplotlib).

matplotlib is used head-less (Agg backend) so it never needs a display.
"""
from __future__ import annotations

import sys
from pathlib import Path


# ----------------------------------------------------------------- matplotlib
def _plt():
    """Import matplotlib with the headless Agg backend (no display needed)."""
    try:
        import matplotlib
    except ImportError:
        sys.exit("matplotlib missing — run via the launcher (scripts/bizconnect.py "
                 "bootstraps the central-store venv; matplotlib is in requirements.txt).")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _log(msg):
    sys.stderr.write("[deck-assets] %s\n" % msg)


# A comfortable board-slide aspect at print resolution. The COM pass scales the
# PNG into the anchor box, so the exact size only sets the rasterised detail.
_FIGSIZE = (10.0, 6.0)
_DPI = 150


def _norm_specs(specs):
    """Accept either (id, spec, path) tuples or raw spec dicts; yield (sid, spec)."""
    for item in specs:
        if isinstance(item, dict):
            spec = item
            sid = str(spec.get("id") or "slide")
        else:                                        # (id, spec, path) — deck.load_specs
            sid, spec = str(item[0]), item[1]
        if isinstance(spec, dict):
            yield sid, spec


# ----------------------------------------------------------------- charts
def _chart_numbers(series):
    """[{name, data:[..]}] -> ([names], [[float..]]). Non-numeric/None -> 0.0."""
    names, data = [], []
    for s in series or []:
        if not isinstance(s, dict):
            continue
        names.append(str(s.get("name", "")))
        row = []
        for v in s.get("data") or []:
            try:
                row.append(float(v))
            except (TypeError, ValueError):
                row.append(0.0)
        data.append(row)
    return names, data


def _unit_suffix(unit):
    return "" if not unit else (" (%s)" % unit if unit not in ("%",) else " (%)")


def render_chart_png(chart, out_path):
    """Render a chart{} payload to out_path (PNG). Returns out_path on success.

    chart = {type, categories, series:[{name,data}], unit, note}. Supported types:
    bar, stacked-bar, line, pie, scatter (anything else -> grouped bar)."""
    plt = _plt()
    ctype = str(chart.get("type") or "bar").lower()
    cats = [str(c) for c in (chart.get("categories") or [])]
    names, data = _chart_numbers(chart.get("series"))
    note = chart.get("note")
    unit = chart.get("unit")

    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    try:
        if ctype == "pie":
            # Pie shows a single series across the categories (first series wins).
            values = data[0] if data else []
            labels = cats or [str(i + 1) for i in range(len(values))]
            if values and sum(abs(v) for v in values) > 0:
                ax.pie([abs(v) for v in values], labels=labels, autopct="%1.0f%%",
                       startangle=90, counterclock=False)
            ax.set_aspect("equal")
        elif ctype == "scatter":
            # Each series is a set of (x, y) points; x = category index unless the
            # series carries paired data. We plot data[i] against the category index.
            x = list(range(len(cats))) if cats else None
            for i, row in enumerate(data):
                xs = x[: len(row)] if x is not None else list(range(len(row)))
                ax.scatter(xs, row, label=names[i] if i < len(names) else None)
            if cats:
                ax.set_xticks(range(len(cats)))
                ax.set_xticklabels(cats, rotation=30, ha="right")
            if any(names):
                ax.legend()
        elif ctype == "line":
            x = range(len(cats)) if cats else None
            for i, row in enumerate(data):
                xs = list(x)[: len(row)] if x is not None else list(range(len(row)))
                ax.plot(xs, row, marker="o", label=names[i] if i < len(names) else None)
            if cats:
                ax.set_xticks(range(len(cats)))
                ax.set_xticklabels(cats, rotation=30, ha="right")
            if any(names):
                ax.legend()
        elif ctype in ("stacked-bar", "stacked_bar", "stackedbar"):
            x = range(len(cats)) if cats else range(max((len(r) for r in data), default=0))
            bottom = [0.0] * len(list(x))
            for i, row in enumerate(data):
                vals = (row + [0.0] * len(bottom))[: len(bottom)]
                ax.bar(list(x), vals, bottom=bottom[: len(vals)],
                       label=names[i] if i < len(names) else None)
                bottom = [b + v for b, v in zip(bottom, vals)]
            if cats:
                ax.set_xticks(range(len(cats)))
                ax.set_xticklabels(cats, rotation=30, ha="right")
            if any(names):
                ax.legend()
        else:                                        # bar (grouped) / fallback
            n = len(data) or 1
            ncat = len(cats) or (max((len(r) for r in data), default=0))
            idx = list(range(ncat))
            width = 0.8 / n
            for i, row in enumerate(data):
                vals = (row + [0.0] * ncat)[:ncat]
                offs = [j + (i - (n - 1) / 2) * width for j in idx]
                ax.bar(offs, vals, width=width, label=names[i] if i < len(names) else None)
            if cats:
                ax.set_xticks(idx)
                ax.set_xticklabels(cats, rotation=30, ha="right")
            if any(names):
                ax.legend()

        if note:
            ax.set_title(str(note))
        if unit and ctype not in ("pie",):
            ax.set_ylabel(_unit_suffix(unit).strip(" ()") or str(unit))
        fig.tight_layout()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path))
        return out_path
    finally:
        plt.close(fig)


# ----------------------------------------------------------------- matrix
def render_matrix_png(matrix, out_path):
    """Render a matrix{} payload to out_path (PNG) as a glyph grid with a header row/col.

    matrix = {rows:[...], cols:[...], cells:[[glyph,..],..], legend:{glyph:meaning}}."""
    plt = _plt()
    rows = [str(r) for r in (matrix.get("rows") or [])]
    cols = [str(c) for c in (matrix.get("cols") or [])]
    cells = matrix.get("cells") or []
    legend = matrix.get("legend") or {}

    nrows, ncols = len(rows), len(cols)
    # Table = header row (cols) + one row per data row; first column = row labels.
    table_rows = []
    for r in range(nrows):
        line = [rows[r]]
        src = cells[r] if r < len(cells) else []
        for c in range(ncols):
            line.append(str(src[c]) if c < len(src) else "")
        table_rows.append(line)
    col_labels = [""] + cols

    fig_h = max(2.0, 0.5 * (nrows + 1) + (1.2 if legend else 0))
    fig, ax = plt.subplots(figsize=(max(_FIGSIZE[0], 0.0 + 1.6 * max(ncols, 1)), fig_h),
                           dpi=_DPI)
    try:
        ax.axis("off")
        tbl = ax.table(cellText=table_rows or [[""]], colLabels=col_labels,
                       cellLoc="center", loc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(12)
        tbl.scale(1.0, 1.6)
        # Bold the header row and the row-label column.
        for (rr, cc), cell in tbl.get_celld().items():
            if rr == 0 or cc == 0:
                cell.set_text_props(fontweight="bold")
                cell.set_facecolor("#f0f0f0")
        if legend:
            text = "   ".join("%s = %s" % (k, v) for k, v in legend.items())
            fig.text(0.5, 0.04, text, ha="center", va="bottom", fontsize=10)
        fig.tight_layout()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), bbox_inches="tight")
        return out_path
    finally:
        plt.close(fig)


# ----------------------------------------------------------------- map (light)
def _resolve_provided(spec_value, base_dir):
    """If spec_value looks like an existing file path, return its resolved Path; else None."""
    if not spec_value or not isinstance(spec_value, str):
        return None
    p = Path(spec_value)
    for cand in ([p] if p.is_absolute() else [Path(base_dir) / p, p]):
        try:
            if cand.exists() and cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    return None


def render_image_asset(image, out_path, base_dir):
    """Resolve/produce the asset for an image{} payload. Returns a Path or None.

    Strategy (spec §4): map/diagram/screenshot/generated are mostly *provided* assets —
    if image.spec is an existing file we pass it through (return that path, no copy needed
    by the COM pass which AddPictures any path). A map MAY be rendered from a lightweight
    bundled states table; that bundle is optional, so absent it we fall back to the
    provided path and log when nothing is available (clean, no hard geo dep)."""
    kind = str(image.get("kind") or "generated").lower()
    spec_value = image.get("spec")

    provided = _resolve_provided(spec_value, base_dir)
    if provided is not None:
        return provided

    if kind == "map":
        rendered = _try_render_us_map(image, out_path)
        if rendered is not None:
            return rendered
        _log("map asset for spec must be supplied: no bundled states geometry and "
             "image.spec %r is not an existing file (clean fallback — provide a PNG/SVG "
             "path in image.spec)." % (spec_value,))
        return None

    _log("%s asset must be supplied: image.spec %r is not an existing file (pass-through "
         "fallback — set image.spec to a generated/exported asset path)."
         % (kind, spec_value))
    return None


def _try_render_us_map(image, out_path):
    """Optionally render a US-state map IF a lightweight bundled states table is present.

    We do NOT take a hard dependency on geopandas/plotly. A bundled GeoJSON of state
    outlines (assets/us_states.geojson alongside this module) would let us path-render a
    choropleth/outline with matplotlib only; absent that bundle we return None so the
    caller falls back to a provided asset path. This keeps deps light per spec §4."""
    geo = Path(__file__).resolve().parent / "assets" / "us_states.geojson"
    if not geo.exists():
        return None
    try:
        import json

        plt = _plt()
        data = json.loads(geo.read_text(encoding="utf-8"))
        values = image.get("values") or {}          # optional {state: number} choropleth
        fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
        try:
            ax.axis("off")
            vmin = min(values.values()) if values else 0.0
            vmax = max(values.values()) if values else 1.0
            span = (vmax - vmin) or 1.0
            for feat in data.get("features", []):
                name = (feat.get("properties") or {}).get("name") \
                    or (feat.get("properties") or {}).get("NAME")
                shade = None
                if name in values:
                    shade = (float(values[name]) - vmin) / span
                for ring in _iter_polys(feat.get("geometry") or {}):
                    xs = [pt[0] for pt in ring]
                    ys = [pt[1] for pt in ring]
                    if shade is None:
                        ax.plot(xs, ys, color="#444444", linewidth=0.5)
                    else:
                        ax.fill(xs, ys, color=plt.cm.Blues(0.2 + 0.8 * shade),
                                edgecolor="#444444", linewidth=0.4)
            cap = image.get("caption")
            if cap:
                ax.set_title(str(cap))
            ax.set_aspect("equal")
            out_path = Path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(out_path), bbox_inches="tight")
            return out_path
        finally:
            plt.close(fig)
    except Exception as e:                           # any geom issue -> clean fallback
        _log("bundled map render failed (%s) — falling back to provided asset path." % e)
        return None


def _iter_polys(geometry):
    """Yield exterior rings (list of [x,y]) from a GeoJSON Polygon/MultiPolygon."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        for ring in coords:
            yield ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield ring


# ----------------------------------------------------------------- entry point
def generate_assets(specs, assets_dir):
    """Generate every image-backed asset the given specs require.

    Returns {spec_id: {kind: Path}} for the assets actually produced/resolved. PNGs are
    written as <assets_dir>/<id>-<kind>.png. Pure Python — no COM. Per-spec failures are
    logged and skipped (the COM renderer degrades gracefully to text/empty box)."""
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for sid, spec in _norm_specs(specs):
        arch = str(spec.get("archetype") or "")
        per = {}

        chart = spec.get("chart")
        if isinstance(chart, dict) and str(chart.get("render") or "native") == "image":
            try:
                per["chart"] = render_chart_png(chart, assets_dir / ("%s-chart.png" % sid))
            except Exception as e:
                _log("%s: chart render failed (%s)" % (sid, e))

        matrix = spec.get("matrix")
        if isinstance(matrix, dict) and str(matrix.get("render") or "table") == "image":
            try:
                per["matrix"] = render_matrix_png(matrix, assets_dir / ("%s-matrix.png" % sid))
            except Exception as e:
                _log("%s: matrix render failed (%s)" % (sid, e))

        image = spec.get("image")
        if isinstance(image, dict) and arch in ("image", "map", "diagram"):
            try:
                got = render_image_asset(image, assets_dir / ("%s-image.png" % sid),
                                         base_dir=assets_dir.parent)
                if got is not None:
                    per["image"] = Path(got)
            except Exception as e:
                _log("%s: image asset resolution failed (%s)" % (sid, e))

        if per:
            out[sid] = per
    return out
