---
name: build-publish
description: End-to-end build-and-publish orchestration for a biz-connect deliverable — the "user instructs the agent to commence" entry point. Use when the user says "build the plan", "commence the build", "produce the deck/operating plan", "do a full build and publish", or kicks off / iterates a major build of a deliverable that has a pipeline.yaml + deck.yaml + connections.yaml. Drives the whole lifecycle: sync inputs from Notion (definitional docs + research mirror) → fan-out draft + slide per section → assemble to final/plan.md → push a review Google Doc (docreg row) → build + preview the PowerPoint deck → publish the deck to Drive (docreg row + .pptx attached to the Notion doc-register row) → run the review roundtrip (GDoc comments → assimilate → open-points register → rebuild stale sections → v2 → re-push + re-attach) and iterate. Orchestrates the compose, gdoc, deck, docreg, register and notion connectors; defers the detail of each stage to the build-deck, doc-pipeline, gdoc-sync, feedback-ingest and register skills.
allowed-tools: Agent, Bash(python *), Read, Edit, Write
---

# Build & publish a deliverable end-to-end (`build-publish`)

This is the **orchestration** skill — the single entry point the user reaches for when they say
"commence" / "build and publish the plan". It sequences the whole lifecycle and hands the detail
of each stage to the specialist skills:

- **doc-pipeline** (`compose`) — the per-section draft/ladder/lint/render machinery.
- **build-deck** (`deck` + `_deck_assets`) — slide-specs → PowerPoint, assets, preview.
- **gdoc-sync** (`gdoc`) — push the rendered plan to a Google Doc for review; capture comments.
- **register** (`register`) + **feedback-ingest** (`assimilate`/`digest`) — the open-points loop.

Don't re-implement those here; **call them**. This skill owns the *order*, the *handoffs*, and
the *iterate* discipline.

> **Umbrella repos.** A repo may host several deliverables under `deliverables/<slug>/`, each with
> its own `pipeline.yaml`, `deck.yaml`, `connections.yaml` block, register and docs-registry. **`cd`
> into the deliverable folder first** so every verb scopes to it (the engine walks up to find the
> right config). If the deliverable isn't named: use the cwd's if you're inside one, else
> `bizconnect deliverable list` and pick the one matching the topic; only ask if genuinely ambiguous.

## Documentary, not opinion (hard rule, inherited end-to-end)

Every artifact this skill produces — section drafts, the slide-specs, the assembled plan, the deck —
carries **facts, data and the plan only**. No agent opinion, hedging, advocacy or verdicts; a
`takeaway` is the factual so-what of the data, not a view. **Every gap, uncertainty or judgement
call lives in the open-points register** (`{{OPEN_POINTS}}`), never inline in a draft, a slide-spec,
or on a slide. The whole pipeline inherits this from the consuming repo's OUTPUT-SPEC principle; the
review roundtrip is precisely the mechanism by which open points are raised, triaged and closed.

## COM safety (non-negotiable — repo CLAUDE.md)

Step 5/6 drive **PowerPoint via COM** (`deck build`/`preview`). They **attach** to a running
PowerPoint (`GetActiveObject`) and **never quit it**; they `Quit()`/`Close()` only an instance/
presentation the connector itself spawned, and open the template **read-only** (building into a
`SaveAs` copy — the template is never mutated). **Never** kill/terminate/`taskkill` any Office
process and never touch the user's other open documents. The asset step (`_deck_assets`,
matplotlib) is pure Python, no COM, safe anywhere.

## The launcher

All verbs run through the launcher (it bootstraps its own venv — nothing to install):

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'
```

Run from inside the deliverable folder. Connectors used below: `compose`, `gdoc`, `deck`,
`docreg`, `register`, `notion`.

---

## The lifecycle

### 0. Commence (the user trigger)

The user says "build/commence/publish the plan". Resolve the deliverable (above), `cd` into it,
then `$B compose status` and `$B deck status` to see what's actually stale — a "build" rebuilds
exactly what changed, so a re-commence is cheap and incremental, not a from-scratch redo.

### Step 00 — Sync inputs (Notion → repo + mirror)

```bash
$B compose run inputs
```

`compose run inputs` is **read-only** and idempotent. In one pass it pulls **both** halves of the
deliverable's `connections.yaml inputs:` block:

- the **`type: notion_pages` definitional docs** — each Notion master page projected to a specific
  repo file (`{url, to}`): the `02.prompts/*.md` prompt contracts, `01.context/house-position.md`,
  per-section guides. The user edits these *in Notion*; this step syncs them down. (See
  `docs/notion-defs-sync.md`.)
- the **`type: notion` recursive mirror** — the research library / context page-tree → a directory
  under `00.inputs` (read-only context, with media + an external-links catalogue).

It only rewrites a file whose rendered content changed and records each in `inputs.lock.json`.
**Assemble context:** with inputs fresh, the per-section evidence packs (`compose assemble`, run as
a code dependency of `draft`) read the mirror + guides. Confirm the lock shows the expected pages
synced before fanning out.

### Step 1 — Fan out per section: draft, then slide

The two per-section LLM stages. Run drafts first (prose), then slide-specs (the deck contract).
Both fan out — **one subagent per section, in parallel** (Agent tool, `model: opus`) — writing only
to `build/` scratch, then promote forward and `accept`.

```bash
$B compose run draft all        # writes build/<id>.draft.prompt.md per section
# fan out: one agent per section -> build/<id>.draft.gen.md -> promote to answers/<id>.md
$B compose accept draft all

$B compose run slide all        # writes build/<id>.slide.prompt.md per section
# fan out: one agent per section -> build/<id>.slide.gen.json -> promote to <slides_dir>/<id>.slide.json
$B compose accept slide all     # validates each is JSON / matches the schema
```

The **draft** prompt reads the global guide + per-item guide + evidence pack + `{{OPEN_POINTS}}`;
the **slide** prompt emits the enriched slide-spec JSON (richest appropriate archetype — default to
chart/table/matrix/metric over bullets; one idea per slide; documentary only; gaps → open-points
block, never opinion in the spec). Detail of both: **doc-pipeline** and **build-deck**. The
guides under `context/` are **inputs** — the build reads them and never rewrites them; never
auto-promote `spec` over an existing guide.

### Step 2 — Assemble → review Google Doc

```bash
$B compose run ladder           # distil the front matter from the answers
$B compose run lint             # provenance / marker / register cross-checks
$B compose run render           # assemble -> final/plan.md
$B gdoc push final/plan.md --new --version vX.Y   # NEW Doc instance for a major build
```

`render` assembles the section answers + front matter into **`final/plan.md`** (the rendered plan,
an output the build creates). `gdoc push --new` creates a **fresh Google Doc instance** for review —
the previous instance and its reviewer comments stay untouched — and **docreg logs the instance
row** automatically when `notion.docs_registry` is bound (artifact, version, Doc URL, git commit,
content hash, status). A plain `$B gdoc push final/plan.md` (no `--new`) updates the current Doc in
place and refreshes its row. Detail: **gdoc-sync**.

### Step 3 — Build the PowerPoint deck

```bash
$B deck build                   # assets (no COM) + template + slide-specs -> <output> deck.pptx
$B deck preview                 # -> final/previews/*.png (+ final/deck.pdf)
```

`deck build` first runs the **asset step** (`_deck_assets.generate_assets` — matplotlib, no COM:
image-rendered charts/matrices/maps → PNGs), then a **single COM pass** that fills the template's
text shapes and inserts native objects (`AddChart2`/`AddTable`/`AddPicture`) at the declared
anchors → the deck. `deck preview` renders per-slide PNGs (+ PDF) for review. The deck `order` is
the **deck narrative** (exec-summary front, three-act, appendix dividers) composed *from* the
section slide-specs — not 1:1 with the section list. Detail: **build-deck**.

> COM rule reminder: this is the only step that touches PowerPoint. Attach, never quit; template
> read-only; never `taskkill`. If a file lock or an open document blocks the build, **stop and tell
> the user** — do not resolve it by closing anything.

### Step 4 — Publish + attach

```bash
$B deck push                    # deck/PDF -> Drive + docreg version row + attach .pptx to the Notion doc-register row
```

`deck push` publishes the built deck to **Drive** (under `connections.yaml google.drive_folder`),
logs a **docreg** version row (mirroring `gdoc push --new --version` + `docreg log`), and **attaches
the `.pptx` to the Notion doc-register row** for that instance — via the `notion` connector's
`upload_file` + `attach`/`media_block` (the File Upload API). So a reviewer opening the doc-register
in Notion finds the live deck next to its Doc URL, version and commit.

> Wiring note: at time of writing `deck push` may surface "Drive/docreg wiring not enabled yet". If
> so, this step is the **target shape** of the publish: until it lands, publish the deck/PDF via the
> `gdoc`/vdr path and log the row with `docreg log --artifact <deck> --version vX.Y --new`, then
> attach the `.pptx` to the row with the `notion` connector. Don't fabricate success — report the
> fallback used.

### Step 5 — Review roundtrip (iterate to v2, v3, …)

Reviewers comment **in the Google Doc**. Lift that feedback back into the pipeline, rebuild only the
stale sections with the open points in hand, and re-publish. Full playbook: **feedback-ingest** +
**register**.

```bash
FB=<feedback_dir>                                  # e.g. final/build/feedback
$B gdoc comments final/plan.md --out $FB/feedback.bundle.md   # anchored comments + threads
$B gdoc diff     final/plan.md >> $FB/feedback.bundle.md      # direct edits (unified diff)
$B register pull                                   # refresh projection so assimilate dedupes
$B compose run assimilate                          # writes the assimilate prompt into build/
# you: one high-reasoning pass -> triage by disposition (finesse/tonal/rethink/research/discussion)
#      + layer (answer/spec/house-position/prompt) -> save plan + ```json deltas to $FB/cycle.gen.md
$B register upsert $FB/cycle.gen.md                # create/update open-points rows (Notion), dedupe by comment-id
```

Then **rebuild the stale sections incorporating `{{OPEN_POINTS}}`** and ship **v2**:

```bash
$B compose status                                  # what the feedback made stale
$B compose run draft <id>      # for each stale section; the draft prompt now injects its open points
$B compose accept draft <id>
$B compose run slide <id>      # refresh affected slide-specs
$B compose accept slide all
$B compose run ladder ; $B compose run lint ; $B compose run render   # reassemble final/plan.md
$B gdoc push final/plan.md --new --version vX.(Y+1)   # re-push the review GDoc (new instance + docreg row)
$B deck build ; $B deck preview ; $B deck push        # rebuild + re-attach the deck to the new row
```

`assimilate` triages each comment by **disposition** and **layer**; `register upsert` is
field-level clobber-safe (ingest owns the machine fields, humans own `Status`/`Owner`/`Agreed
steps`/Commentary — always `pull` before `upsert`). The register feeds back into generation:
`draft`/`slide`/`ladder` inject the open points via `{{OPEN_POINTS}}`, and `lint` cross-checks every
`[…: ISS-nnn …]` marker against the register so a point and its marker can't drift. Gated points
(`rethink`/`research`/`discussion`) go to the team via `compose run digest` → the deliberation
brief; their agreed source edits make the right targets stale next cycle. **Iterate** until the open
points are closed (`register resolve ISS-nnn` + `gdoc resolve`).

---

## At a glance

| step | command | connector | produces |
|---|---|---|---|
| 00 sync | `compose run inputs` | compose · notion | repo defs (`notion_pages`) + research mirror (`notion`) + `inputs.lock.json` |
| 1 draft | `compose run draft all` → accept | compose (fan-out) | `answers/<id>.md` |
| 1 slide | `compose run slide all` → accept | compose (fan-out) | `<slides_dir>/<id>.slide.json` |
| 2 assemble | `ladder` · `lint` · `render` | compose | `final/plan.md` |
| 2 review Doc | `gdoc push final/plan.md --new --version vX.Y` | gdoc · docreg | Google Doc + docreg instance row |
| 3 deck | `deck build` · `deck preview` | deck (+ `_deck_assets`) | `deck.pptx` + previews/PDF |
| 4 publish | `deck push` | deck · docreg · notion | Drive copy + docreg row + `.pptx` attached to Notion row |
| 5 roundtrip | `gdoc comments`/`diff` → `assimilate` → `register upsert` → rebuild stale → v2 → re-push/re-attach | gdoc · compose · register · notion | open points + the next version |

## Discipline

- **Documentary, not opinion.** Gaps live in the open-points register — never inline, never on a slide.
- **Incremental by default.** `compose status` / `deck status` drive what rebuilds; a re-commence
  rebuilds only what changed. There is no "rebuild everything".
- **Inputs are read-only.** `compose run inputs` and the `context/` guides are inputs the build
  reads, never writes. Edit the definitional docs **in Notion**, then re-sync.
- **Never close Office.** If PowerPoint/Drive blocks a build, stop and tell the user.
- **Don't claim more than you ran.** If `deck push` wiring isn't live, report the fallback you used.
