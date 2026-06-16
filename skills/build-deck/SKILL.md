---
name: build-deck
description: Render a board-grade slide deck from per-section slide-specs + a PowerPoint template, via the biz-connect `deck` connector. Use when a deliverable has a deck.yaml and slide-spec JSON files (produced by compose's `slide` stage) and the user wants to build/assemble/render the deck, preview slides as PNG/PDF, or check which slides are stale. The enriched slide-spec carries native charts, tables, a matrix grid, metric panels and images — not just text. Windows + PowerPoint (COM): it edits a copy of the template, never mutates the template, and never quits the user's Office.
allowed-tools: Agent, Bash(python *), Read, Edit, Write
---

# Build a slide deck from slide-specs (`deck`)

The slide layer of the build-by-section pipeline. `compose` builds each section's prose
and a **slide-spec** (`compose run slide` → `<slides_dir>/<id>.slide.json`); **`deck`**
assembles those specs into a `.pptx` from a template, generates any non-text assets, and
renders previews. Content-free: the template, the archetype→template-slide map, the
field→shape map, the anchor→placeholder map and the slide order all live in the
deliverable's **`deck.yaml`** — the connector hardcodes no layout, shape, or content.

## Documentary, not opinion (hard rule)
Slide-specs carry **facts, data and the plan only** — no agent opinion, hedging, advocacy
or verdicts. The `takeaway` is the factual so-what of the data on the slide, not a view.
Any gap, uncertainty or judgement call goes to compose's **open-points register** (the
`{{OPEN_POINTS}}` mechanism), **never** into a slide-spec or onto a slide. This mirrors the
consuming repo's OUTPUT-SPEC principle; the slide layer inherits it unchanged.

## COM safety (non-negotiable)
`deck build`/`preview` drive PowerPoint via COM. They **attach** to a running PowerPoint
(`GetActiveObject`) and **never quit it**; they `Quit()` only an instance the connector
spawned, `Close()` only their own presentation, and open the template **read-only**
(building into a `SaveAs` copy). They never kill/terminate any Office process and never
touch the user's other open documents. (Repo CLAUDE.md hard rule.) The **asset step** (§
below) is pure Python with **no COM** and is safe to run anywhere, including off-Windows.

## The flow
1. **Build sections** (compose): `compose run draft all` → promote → `compose accept draft all`.
2. **Build slide-specs** (compose, fanned out): `compose run slide all` writes one prompt per
   section; run them — ideally **one subagent per section in parallel** (Agent tool, `model:
   opus`) — each emitting an enriched slide-spec JSON to `build/<id>.slide.gen.json`, promoted
   to `<slides_dir>/<id>.slide.json`; then `compose accept slide all` (validates each is JSON).
   The `slide` prompt picks the **richest appropriate archetype** (default to showing data
   visually — chart/table/matrix/metric over bullets), one idea per slide, documentary only.
3. **Assemble the deck** (deck): `deck build` generates assets (charts/matrices/maps as PNGs),
   then runs a **single COM pass** that fills text shapes and inserts native objects at the
   declared anchors → `<output>`.
4. **Preview**: `deck preview` → per-slide PNGs (+ PDF) for review.
5. **Status / rebuild**: `deck status` shows FRESH/STALE per slide vs the spec hashes; edit a
   section → re-run `compose run slide <id>` → `deck build` rebuilds the whole deck in place.

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'
$B compose run slide all      # writes build/<id>.slide.prompt.md per section
# fan out: one agent per section -> build/<id>.slide.gen.json -> <slides_dir>/<id>.slide.json
$B compose accept slide all
$B deck build                 # assets (no COM) + template + specs -> <output> deck.pptx
$B deck preview               # -> final/previews/*.png (+ final/deck.pdf)
$B deck status                # FRESH/STALE/MISSING per slide
```

## The enriched slide-spec (`<slides_dir>/<id>.slide.json`)
One idea per slide; the spec is the **shared contract** with compose's `slide` stage. Required
keys: `id`, `archetype`, `headline`. The `archetype` must be declared in `deck.yaml`. Per
archetype, a payload block carries the data:

| archetype       | required payload | shape on the slide |
|-----------------|------------------|--------------------|
| `bullets` / `two-column` / `exec-summary` / `divider` / `quote` | text only (`claims`, `bullets`, `two_column`) | text shapes |
| `metric-panel`  | `metrics: [{label, value, note}]` | metric lines / styled boxes |
| `chart`         | `chart: {type, categories, series:[{name,data}], unit, note, render}` | native `AddChart2`, or a PNG when `render:"image"` |
| `table`         | `table: {headers, rows, note}` | native `AddTable` |
| `matrix`        | `matrix: {rows, cols, cells, legend, render}` | `AddTable` grid (`render:"table"`) or a PNG (`render:"image"`) |
| `image`/`map`/`diagram` | `image: {kind, spec, caption}` | `AddPicture` of the asset (`spec` is a path or a render brief) |
| `timeline`      | `timeline: {lanes, milestones:[{label, when, lane}]}` | as the template archetype renders it |

Optional everywhere: `subhead`, `takeaway` (factual so-what), `speaker_notes`, `claims`,
`bullets`, `backup_refs`, `sources`. `validate_spec` (in `deck.py`) enforces the required keys
**and** the per-archetype payload; an unknown archetype or a missing required payload is a hard
error (matching the existing fail-on-mismatch behaviour). See the canonical schema in
`docs/deck-slide-production.md §1` — it is the source of truth across agents.

## The asset step (no COM, runs anywhere)
Before the COM pass, `deck build` runs `_deck_assets.generate_assets(specs, assets_dir)` (pure
Python, matplotlib only — **no PowerPoint, no COM**). It renders the non-text payloads that need
a bitmap and writes them to `<deck.yaml dir>/05.deck/assets/<id>-<kind>.png`:

- **charts** with `render:"image"` → matplotlib bar / stacked-bar / line / pie / scatter.
- **matrix** with `render:"image"` → a matplotlib grid / heatmap.
- **map** → a US-states render if feasible; otherwise `image.spec` is treated as a **provided
  asset path** and a note is logged (clean fallback — no hard geopandas dependency).
- **diagram / screenshot / generated** → pass through if `image.spec` is a path; else log a TODO.

Because the asset step is COM-free, it (plus `validate_spec`) is exactly what the off-Windows
**smoke test** exercises (`examples/deck-demo/`): specs validate and asset PNGs render without
ever launching PowerPoint. Native charts/tables/matrices (`render` not `"image"`) are built by
COM at `deck build` time and need no asset.

## deck.yaml (in the deliverable)
```yaml
template: 05.deck/template.pptx        # REQUIRED — the slide-template formats
output:   final/deck.pptx
slides_dir: slides                     # where compose's slide stage wrote <id>.slide.json
order: [S01, S02, S03]                 # the DECK NARRATIVE (exec-summary front, three-act, appendix
                                       #   dividers) composed FROM the section specs — not 1:1 with sections
preview: {width: 1920, height: 1080, pdf: true}
archetypes:                            # one entry per slide archetype the template supports
  divider:      {source_slide: "Divider",     fields: {headline: "Title"}}
  bullets:      {source_slide: "Bullets",      fields: {headline: "Title", subhead: "Subtitle", claims: "Body"}}
  metric-panel: {source_slide: "MetricPanel",  fields: {headline: "Title", metrics: "Metrics"}}
  chart:
    source_slide: "Chart"
    fields:  {headline: "Title", subhead: "Subtitle", takeaway: "Takeaway"}
    anchors: {chart: "ChartArea"}      # the inserted object takes this placeholder's geometry; placeholder deleted
  table:
    source_slide: "Table"
    fields:  {headline: "Title", takeaway: "Takeaway"}
    anchors: {table: "TableArea"}
  matrix:
    source_slide: "Matrix"
    fields:  {headline: "Title"}
    anchors: {matrix: "MatrixArea"}
  image:
    source_slide: "Image"
    fields:  {headline: "Title", subhead: "Caption"}
    anchors: {image: "ImageArea"}
```
`source_slide` is the **Name** of the example slide in `template.pptx` (set names in
PowerPoint's Selection Pane). `fields` maps slide-spec keys to **shape Names** on that slide;
a spec list (e.g. `claims`) fills as bullet paragraphs, a list of `{label,value}` (e.g.
`metrics`) fills as lines, and an unmapped/missing value clears that shape's text.

**`anchors`** is the convention for native objects (spec §2): it maps a slide-spec payload key
(`chart`, `table`, `matrix`, `image`) to the **Name of a placeholder shape** on the template
slide. `deck build` reads that placeholder's geometry (Left/Top/Width/Height), inserts the
object there (`AddChart2` / `AddTable` / `AddPicture`), then **deletes the placeholder**. So the
template author positions a box, names it, and the connector drops the live object into its box.

`order` is the **deck narrative** — the three-act story shape with an exec-summary up front and
appendix dividers — composed *from* the section slide-specs; it is not 1:1 with the section list.

## Notes
- One builder per **archetype**: a genuinely new slide layout = a new template slide + a new
  `archetypes` entry (with `fields` and any `anchors`), not new code.
- `deck push` (publish to Drive + docs-registry version row) is not wired yet — publish the
  PDF via the gdoc/vdr path for now.
- The fixture under `examples/deck-demo/` validates specs + generates assets **off-Windows** (no
  COM); the full COM render runs only on Windows with PowerPoint and a real `template.pptx`.
