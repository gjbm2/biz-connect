---
name: doc-pipeline
description: Build a structured document (e.g. a consultation response, a multi-section report) from a corpus, one item at a time, with incremental/partial rebuilds. Use when a repo has a pipeline.yaml and the user wants to draft, review, or rebuild per-item answers, ladder them into front matter, lint, or render the document. Deterministic steps run as code; you (the agent) do the writing steps from the repo's own prompt templates. Keep the human-owned files (context/, answers/, submission/) as the source of truth.
allowed-tools: Bash(python *), Read, Edit, Write
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
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose scaffold      # create missing per-item local guides
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose run  <stage> <id|all>
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" compose accept <stage> <id|all>
```

Stages: `assemble` (code) · `spec` `draft` `critique` `ladder` (llm) · `lint` `render` (code).

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
4. When several answers are ready: **`compose run ladder`** (front matter), **`compose run
   lint`** (completeness/provenance), **`compose run render`** (the assembled document).

You execute the llm steps either inline (you, now) or by fanning out one agent per item
across many ids — the prompt files the tool writes are the same either way.

## Partial / incremental builds

Each target hashes its declared inputs; `status` shows **FRESH ✓ / STALE ~ / MISSING ·**.
Editing one item's guide marks only that item's `assemble`/`draft` stale — nothing else.
Editing the global guide marks every spec/draft stale (it's a shared input). Editing any
answer marks `ladder`/`lint`/`render` stale. Rebuild exactly the targets you name; there is
no "rebuild all".

## Setup

The repo needs a `pipeline.yaml` (see `examples/pipeline.example.yaml` in the plugin) and
the files it points at: a global guide, a `prompts/` dir with `spec.md`/`draft.md`/
`critique.md`/`ladder.md` (using the `{{GLOBAL_CONTEXT}}`, `{{ITEM_TEXT}}`, `{{EVIDENCE}}`,
`{{LOCAL_CONTEXT}}`, `{{OUTPUT}}`, `{{ALL_OUTPUTS}}` placeholders), and an items JSON
(optionally a structured corpus index). `compose scaffold` seeds the per-item local guides.
