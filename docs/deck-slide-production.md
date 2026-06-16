# Deck slide-production extension (biz-connect)

**Status:** spec for implementation (fan-out). Extends the existing `deck` connector
(`bizconnect/connectors/deck.py`) and the `compose` `slide` stage.

## Goal
Turn composed operating-plan MD sections into a **varied, board-grade** PowerPoint deck. Today
`deck build` fills **text shapes only**. This extension adds **native charts, tables,
images/maps/diagrams, a matrix grid, and metric panels**, driven declaratively by an enriched
slide-spec + `deck.yaml`, plus an **asset-generation step**. Everything stays **content-free** in
the connector (no hardcoded layout/shape/content) and **COM-safe**.

### Documentary, not opinion (hard rule — see the consuming repo's OUTPUT-SPEC §Principle)
Slide-specs carry **facts, data and the plan only**. No agent opinion, hedging, advocacy or
verdicts. Any gap/uncertainty goes to the `compose` **open-points register** (the `{{OPEN_POINTS}}`
mechanism), never into the slide-spec or onto a slide.

## 1. Canonical enriched slide-spec (`<slides_dir>/<id>.slide.json`)
One idea per slide; pick the **richest appropriate** archetype (default to showing data visually).
```json
{
  "id": "S03",
  "section": "S3",
  "archetype": "exec-summary|divider|appendix-divider|bullets|two-column|metric-panel|chart|table|matrix|timeline|map|diagram|image|quote",
  "headline": "string (required)",
  "subhead": "string|null",
  "takeaway": "the one-line so-what (factual, not opinion)|null",
  "metrics": [{"label": "TAM", "value": "$X bn", "note": "source|null"}],
  "table":  {"headers": ["..."], "rows": [["..."]], "note": "source|null"},
  "chart":  {"type": "bar|stacked-bar|line|pie|scatter", "categories": ["..."],
              "series": [{"name": "...", "data": [0]}], "unit": "$|%|null", "note": "source|null",
              "render": "native|image"},
  "matrix": {"rows": ["...states"], "cols": ["...categories"], "cells": [["★","●"]],
              "legend": {"★": "...", "●": "..."}, "render": "table|image"},
  "image":  {"kind": "map|diagram|screenshot|generated", "spec": "what to render OR an asset path",
              "caption": "string|null"},
  "timeline": {"lanes": ["..."], "milestones": [{"label": "...", "when": "...", "lane": "..."}]},
  "two_column": {"left": ["..."], "right": ["..."], "left_title": "...|null", "right_title": "...|null"},
  "claims": ["..."],
  "bullets": ["...(sparingly)"],
  "speaker_notes": "string|null",
  "backup_refs": ["research-library/A/A4-...md", "DB:States"],
  "sources": [1, 2]
}
```
`validate_spec` (in deck.py) extends to **per-archetype payload checks**: `chart`→`spec.chart`,
`table`→`spec.table`, `matrix`→`spec.matrix`, `metric-panel`→`spec.metrics`,
`image|map|diagram`→`spec.image`, `timeline`→`spec.timeline`, `two-column`→`spec.two_column`.
Unknown archetype or missing required payload = hard error (matches today's fail-on-mismatch).

## 2. `deck.yaml` conventions (extension)
Keep `template`, `output`, `slides_dir`, `order`, `preview`. Per archetype, in addition to the
existing text `fields` map, allow an **`anchors`** map naming placeholder shapes whose
**geometry** (Left/Top/Width/Height) positions an inserted object; the placeholder is deleted
after insert:
```yaml
archetypes:
  chart:
    source_slide: "Chart"
    fields: {headline: "Title", subhead: "Subtitle", takeaway: "Takeaway"}
    anchors: {chart: "ChartArea"}          # AddChart2/AddPicture placed at this shape's box
  table:
    source_slide: "Table"
    fields: {headline: "Title", takeaway: "Takeaway"}
    anchors: {table: "TableArea"}
  matrix:
    source_slide: "Matrix"
    fields: {headline: "Title"}
    anchors: {matrix: "MatrixArea"}
  metric-panel:
    source_slide: "Metrics"
    fields: {headline: "Title", metrics: "Metrics"}   # metrics may also fill text
  image:
    source_slide: "Image"
    fields: {headline: "Title", subhead: "Caption"}
    anchors: {image: "ImageArea"}
```
`order` is the **deck narrative** (the three-act story shape + exec-summary front + appendix
dividers), composed FROM the section slide-specs; it is not 1:1 with the section list.

## 3. Renderer (`deck.py`)
Add, alongside `_set_shape_text`/`_fill_slide`, anchored inserts that read geometry from the named
anchor shape, insert, then delete the placeholder:
- `_render_chart(slide, box, chart)` — `Shapes.AddChart2`; map `type`→`XlChartType`; write
  `categories`+`series` into the chart's data workbook; title from `note`. If `render == "image"`
  or AddChart2 fails, `AddPicture` the pre-generated asset (see §4). 
- `_render_table(slide, box, table)` — `Shapes.AddTable(rows, cols)` at `box`; fill headers+rows;
  light styling (header row bold). 
- `_render_image(slide, box, image)` — `Shapes.AddPicture` the asset path at `box`; caption text
  if a caption shape is mapped.
- `_render_matrix(slide, box, matrix)` — `render == "table"`: an AddTable grid with header row/col
  + cell glyphs/colours; `render == "image"`: AddPicture the generated matrix asset.
- `metric-panel` — fill the mapped text shape (existing list-of-{label,value} path) or styled boxes.
`cmd_build` calls the asset step (§4) BEFORE the COM pass, then for each spec fills text fields and,
for each declared anchor, calls the matching `_render_*`. **COM-safety unchanged and absolute**
(attach/never-quit-user, template read-only + SaveAs, Quit only self-spawned, never taskkill).

## 4. Asset-generation step (`bizconnect/connectors/_deck_assets.py`, NEW)
Pure Python, **no COM**. `generate_assets(specs, assets_dir) -> {spec_id: {kind: path}}`:
- **charts** (`render == "image"`): matplotlib bar/stacked-bar/line/pie/scatter from `chart{}`.
- **matrix** (`render == "image"`): matplotlib grid/heatmap from `matrix{}`.
- **map**: US-state rendering — if a bundled lightweight states GeoJSON + matplotlib path-render is
  feasible, produce a choropleth/outline; otherwise treat `image.spec` as a **provided asset path**
  and log that the map asset must be supplied (clean fallback, no hard dep on geopandas).
- **diagram/screenshot/generated**: if `image.spec` is a path, pass through; else log a TODO asset.
Write PNGs to `<deck.yaml dir>/05.deck/assets/<id>-<kind>.png`. Add **`matplotlib`** to
`requirements.txt` (the launcher reinstalls via the sha256 marker). Keep deps light (matplotlib
only; no geopandas/plotly unless trivially bundled).

## 5. Prompts (consuming repo: `deliverables/us-launch/02.prompts/`)
- **`slide.md`** — the slide-spec contract: documentary, format-variety (choose the richest
  archetype; default to chart/table/matrix/metric over bullets), one idea/slide, emits the §1 JSON
  exactly, gaps→`open-points` block (never opinion in the spec). Reads `{{ITEM_TEXT}}`,
  `{{GLOBAL_CONTEXT}}`, `{{LOCAL_CONTEXT}}` (the built answer), `{{EVIDENCE}}`, `{{OPEN_POINTS}}`.
- **`draft.md`, `ladder.md`** — adapt the nous-reg ofgem-tpi versions for an operating PLAN:
  drop consultation-specific lines; documentary/no-opinion; gaps→open-points; `ladder.md` distils
  the exec-summary / front matter from the section answers.

## 6. Skill + smoke test (biz-connect)
- Update `skills/build-deck/SKILL.md` for the enriched flow (compose draft→slide→deck build/preview;
  the documentary discipline; the asset step).
- Add a tiny **fixture** under `examples/deck-demo/` (2–3 slide-specs incl. chart+table+matrix, a
  `deck.yaml`, and a note) so `validate_spec` + `generate_assets` can be **smoke-tested off-Windows**
  (asset PNGs render; spec validates). The COM render runs only on Windows.

## 7. File ownership (fan-out — disjoint, parallel-safe)
- **Agent DECK** → `bizconnect/connectors/deck.py`, `bizconnect/connectors/_deck_assets.py` (new),
  `requirements.txt`.
- **Agent PROMPTS** → `deliverables/us-launch/02.prompts/{slide.md,draft.md,ladder.md}` (notion-bot).
- **Agent SKILL** → `skills/build-deck/SKILL.md`, `examples/deck-demo/*` (new), this doc's “done”
  notes.
No two agents touch the same file. The enriched slide-spec schema (§1) is the shared contract.
