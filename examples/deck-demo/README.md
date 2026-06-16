# deck-demo — off-Windows smoke-test fixture for the `deck` connector

A minimal, self-contained fixture for validating the **enriched slide-spec** schema and the
**COM-free asset-generation step** of the `deck` connector — **without** launching PowerPoint.
It is deliberately runnable off-Windows (Linux/macOS CI, a dev box without Office).

## What's here

| File | Archetype | Demonstrates |
|------|-----------|--------------|
| `S01-market.slide.json` | `chart` | a stacked-bar chart payload with `render: "image"` |
| `S02-unit-economics.slide.json` | `table` | a headers + rows table payload |
| `S03-state-readiness.slide.json` | `matrix` | a rows × cols matrix with a glyph legend, `render: "image"` |
| `deck.yaml` | — | matching `archetypes` with `fields` + `anchors` and a narrative `order` |

The specs conform to the canonical enriched slide-spec in `docs/deck-slide-production.md §1`:
required `id` / `archetype` / `headline`, the per-archetype payload block (`chart` / `table` /
`matrix`), and the documentary optional fields (`takeaway`, `subhead`, `speaker_notes`,
`backup_refs`, `sources`). The content is **documentary** — facts and data only, no opinion;
each `takeaway` is the factual so-what of the data on the slide.

## What it does and does NOT cover

- **Covered (any OS):** schema validation (`deck.validate_spec`) and asset generation
  (`_deck_assets.generate_assets` — matplotlib PNGs for the chart and matrix, both `render:
  "image"`). Pure Python, **no COM**.
- **Not covered here:** the COM render. `deck build` / `deck preview` drive PowerPoint via COM
  and run **only on Windows** with PowerPoint installed and a real `template.pptx`. That's why no
  `template.pptx` ships in this fixture, and why these commands must **not** be run as part of the
  smoke test (repo CLAUDE.md: never drive/quit the user's Office). Native `table` rendering and
  native chart/matrix rendering (where `render` is not `"image"`) are produced by COM at build
  time and need no asset.

## Run the smoke test (off-Windows)

`deck.yaml` sets `slides_dir: .`, so the connector finds the three `*.slide.json` beside it.
The asset step writes PNGs under `05.deck/assets/` relative to this directory.

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'

# 1. Schema validation — must report every fixture FRESH/MISSING (no validation errors).
#    `deck status` parses + validates each spec without touching PowerPoint.
( cd examples/deck-demo && $B deck status )

# 2. Asset generation only (no COM). Drive the asset step directly:
python - <<'PY'
import json, pathlib
from bizconnect.connectors._deck_assets import generate_assets   # pure Python, no COM
here = pathlib.Path("examples/deck-demo")
specs = [json.loads(p.read_text("utf-8")) for p in sorted(here.glob("*.slide.json"))]
out = generate_assets(specs, here / "05.deck" / "assets")
print("rendered:", out)   # expect a PNG for S01 (chart) and S03 (matrix)
PY
```

The schema-only check needs nothing beyond `json`/`ruamel.yaml`; asset generation needs
`matplotlib` (declared in the connector's `requirements.txt`; the launcher installs it). A bare
parse check uses no third-party deps at all:

```bash
python -c "import json,glob; [json.load(open(f,encoding='utf-8')) for f in glob.glob('examples/deck-demo/*.slide.json')]; print('all specs parse')"
```
