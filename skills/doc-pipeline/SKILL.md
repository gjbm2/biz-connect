---
name: doc-pipeline
description: Build a structured document (e.g. a consultation response, a multi-section report) from a corpus — either end-to-end in one pass ("create/build/assemble the whole document/submission") or by commissioning any single stage or item (incremental/partial rebuilds). Use when a repo has a pipeline.yaml and the user wants to create the whole doc, or draft, review, rebuild per-item answers, ladder them into front matter, lint, render, or publish. Deterministic steps run as code; the per-item writing steps fan out to parallel high-reasoning subagents (or run inline), and the rendered document is pushed to a Google Doc for review. Keep the human-owned files (context/, answers/, submission/) as the source of truth.
allowed-tools: Agent, Bash(python *), Read, Edit, Write
---

# Document-composition pipeline (`compose`)

Treat the document as a **compiled artifact**: corpus → per-item evidence pack → per-item
spec (argument guide) → per-item draft → critique → ladder-up to front matter → lint →
render. Deterministic glue (`assemble`, `lint`, `render`, staleness) runs as code; the
judgement steps (`spec`, `draft`, `critique`, `ladder`) are **yours** to write, using the
repo's own `prompts/<stage>.md` templates. The engine is content-free; everything specific
lives in the repo's `pipeline.yaml` and the files it names.

**Clobber-safe:** `compose` writes proposals into `build/…gen.md`; the human-owned files
(`context/`, `answers/`, `submission/`) are only changed when you promote a draft. Never
overwrite them blindly.

## How to run

All verbs go through the launcher (it bootstraps its own venv — nothing to install). Run
from inside the consuming repo (it finds `pipeline.yaml` by walking up from the cwd):

```bash
BC='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose'
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose status        # FRESH/STALE/MISSING per target
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose graph         # the dependency model
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose run  inputs    # refresh external source docs (read-only)
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose scaffold      # create missing per-item local guides
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose run  <stage> <id|all>
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose accept <stage> <id|all>
```

Stages: `inputs` `assemble` (code) · `spec` `draft` `critique` `ladder` `assimilate` `digest` (llm) · `lint` `render` (code).

`inputs` (code) refreshes local Markdown copies of external source documents declared in
the repo's `connections.yaml` under `inputs:` (e.g. a Google Doc someone drafted). It is
**read-only** — it pulls each source into its `extract_to` path and never writes back —
and idempotent (only rewrites a copy that changed). Run it first so downstream steps build
off fresh inputs.

## Build the whole document (one pass)

"Create the doc" = run the whole pipeline end-to-end. It is **one overall pipeline, but you can
commission only parts of it** — any single stage or item — whenever you need to. The default
for "build/assemble/create the document" is the full sequence:

1. `compose run inputs` — refresh external source docs (if the repo declares any).
2. `compose scaffold` — create any missing per-item guides.
3. **Draft every answer.** `compose run draft all` writes one draft prompt per item into
   `build/`. Execute them — ideally **one subagent per item, fanned out in parallel** (Agent
   tool) — writing each answer to `answers/<id>.md`. Run `spec` first for items whose guide is
   thin, if you want an argument plan before drafting.
4. `compose run ladder` — distil the front matter from the answers.
5. `compose run lint` — provenance / marker / register checks.
6. `compose run render` — assemble `final/<doc>.md`.
7. **Publish** (gdoc-sync skill): `gdoc push <final> --new --version vX.Y` for a major build (a
   fresh Doc instance + a registry row), or a plain `gdoc push <final>` to update the current
   instance in place.

Then report the flagged `[DECISION]`/`[VERIFY]` markers and any open register points for review
— on the assembled draft, rather than gating each of the per-item steps.

**Commission only part of it** whenever you need to: `compose run draft Q7` redoes one answer,
`compose run render` just reassembles, `compose run ladder` just the front matter. `compose
status` shows what's actually stale, so you rebuild exactly what changed and nothing more.

## Producing one item (the happy path)

For an item id (e.g. `Q7`):

1. **`compose run spec Q7`** → writes `build/Q07.spec.prompt.md` (auto-assembling the
   evidence pack first). **You** run that prompt: read the file, write the spec, save it to
   `build/Q07.spec.gen.md`, then merge the good parts into the human-owned guide
   `context/.../Q07.md`. Then **`compose accept spec Q7`**.
2. **`compose run draft Q7`** → `build/Q07.draft.prompt.md`. You write the answer → save to
   `build/Q07.draft.gen.md` → promote into `answers/Q07.md`. Then **`compose accept draft Q7`**.
3. **`compose run critique Q7`** → `build/Q07.critique.prompt.md`. You produce the review →
   `build/Q07.critique.gen.md`; revise `answers/Q07.md` if needed.
4. When several answers are ready: **`compose run ladder`** — front matter (cover · intro ·
   position · executive summary), distilled from the answers. It follows the repo's
   `front_matter_template` (`{{TEMPLATE}}`) and weaves in `intro` (`{{INTRO}}`). Then
   **`compose run lint`** (completeness/provenance) and **`compose run render`** (assembled doc).

## Full build: fan out to high-reasoning agents, deliver a GDoc

A full build runs the per-item LLM work as **parallel high-reasoning subagents** (the
deterministic steps stay code). For a stage across all items (`spec` / `draft` / `critique`):

1. `… compose run <stage> all` — writes every item's prompt into `build/<id>.<stage>.prompt.md`.
2. **Fan out:** launch one subagent per item *in parallel* with the **Agent** tool, each on a
   high-reasoning model (`model: opus`). Tell each to read its `build/<id>.<stage>.prompt.md`,
   do the writing, and save **only** to `build/<id>.<stage>.gen.md` — never a human-owned file.
3. **Promote + record:** merge each `…gen.md` into its canonical human-owned file
   (`spec`→`context/<id>.md`, `draft`→`answers/<id>.md`), then `… compose accept <stage> all`.

Then the single-shot steps (no fan-out): `… compose run ladder` → promote the front matter;
`… compose run lint`; `… compose run render`.

**Deliver for review:** `… gdoc push <final>` pushes the rendered document to a Google Doc
(created and bound in `connections.yaml` on first run). That Doc is what reviewers mark up —
the input to the **feedback roundtrip** (`feedback-ingest` skill). So one build goes: fan-out
drafts → ladder → render → **GDoc for review** → roundtrips.

For a one-off or a single id, run a stage inline (you, now) instead of fanning out — the
prompt files are identical either way.

## Partial / incremental builds

Each target hashes its declared inputs; `status` shows **FRESH ✓ / STALE ~ / MISSING ·**.
Editing one item's guide marks only that item's `assemble`/`draft` stale — nothing else.
Editing the global guide marks every spec/draft stale (it's a shared input). Editing any
answer marks `ladder`/`lint`/`render` stale. Rebuild exactly the targets you name; there is
no "rebuild all".

## Feedback roundtrip (assimilate · digest)

Two llm stages close the loop from a reviewed document back into the pipeline (full playbook:
the **feedback-ingest** skill):

- **`assimilate`** — a high-reasoning pass over captured reviewer feedback (`gdoc comments` +
  `gdoc diff` → `<feedback_dir>/feedback.bundle.md`). It lifts each comment into a triaged
  **open point** — by *disposition* (finesse / tonal / rethink / research / discussion) and
  *layer* (answer / spec / house-position / prompt) — and emits register deltas. Persist them
  with `register upsert` (the **register** skill).
- **`digest`** — reduces the open *gated* points into a deliberation brief for the team.

The register feeds back into generation: `spec`/`draft`/`critique` (and `ladder`, for points
routed to `front-matter`) inject the open points via `{{OPEN_POINTS}}`, so the next turn is
made *with respect to* them, and `lint`
cross-checks every `[…: ISS-nnn …]` marker against the register. Bind the register and its
paths in `pipeline.yaml` (`register`, `brief`, `feedback_dir`) and `connections.yaml`
(`notion.register_db`).

## Setup

The repo needs a `pipeline.yaml` (see `examples/pipeline.example.yaml` in the plugin) and
the files it points at: a global guide, a `prompts/` dir with `spec.md`/`draft.md`/
`critique.md`/`ladder.md` (and, for the feedback loop, `assimilate.md`/`digest.md`) using the
`{{GLOBAL_CONTEXT}}`, `{{ITEM_TEXT}}`, `{{EVIDENCE}}`, `{{LOCAL_CONTEXT}}`, `{{OUTPUT}}`,
`{{ALL_OUTPUTS}}`, `{{INTRO}}`, `{{TEMPLATE}}`, `{{OPEN_POINTS}}`, `{{FEEDBACK}}`, `{{REGISTER}}`
placeholders, and an items JSON (optionally a structured corpus index). For richer front
matter, also set `paths.intro` and `paths.front_matter_template`. `compose scaffold` seeds the
per-item local guides.
