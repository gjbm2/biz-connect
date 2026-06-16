# Definitional-docs sync: Notion → repo files (`type: notion_pages`)

**Status:** spec for implementation. Extends `compose run inputs` (pipeline **step 00**) so the
**key definitional docs are mastered in Notion** and synced down to individual repo Markdown files.
Edit inputs in Notion → `compose run inputs` → run the pipeline via a skill.

## Why
Today `inputs:` supports `type: notion` (recursive **mirror** of a page tree into a *directory*) and
`type: gdoc` (one Doc → one file). The definitional docs (the `02.prompts/*.md` files, the
`01.context/house-position.md` global guide, optionally the per-section guides) want a **directed
page→file** sync: each is mastered as its own **Notion page**, projected to a specific repo path.

## connections.yaml schema (new input type)
Under `deliverables.<slug>.inputs` (scoped) — alongside the existing recursive `notion` mirror:
```yaml
inputs:
  # existing recursive mirror (unchanged): the research library + context, read-only into a dir
  us-build-out-plan: { type: notion, extract_to: 00.inputs/notion-hub, url: "<hub url>", ... }

  # NEW: definitional docs mastered in Notion, one page -> one repo file
  definitions:
    type: notion_pages
    pages:
      - { url: "<notion page url/id>", to: "02.prompts/slide.md" }
      - { url: "<notion page url/id>", to: "02.prompts/draft.md" }
      - { url: "<notion page url/id>", to: "02.prompts/ladder.md" }
      - { url: "<notion page url/id>", to: "01.context/house-position.md" }
      # extend with per-section guides / any definitional doc as needed
```
`to` paths resolve from the deliverable root (like the other paths). Read-only (we never write back
to Notion from the sync). Idempotent: only rewrites a file whose rendered content changed; records
each in `03.build/inputs.lock.json`.

## biz-connect code
1. **`notion.py` — add `page_to_markdown(page) -> str`.** Reuse the existing `_Scraper`
   block-renderer (`render_children`/`render_block`/`rich_md`) to render ONE page's blocks to a
   Markdown string. Flat: render the page's own blocks (not a recursive child-page mirror, no media
   downloads — the definitional docs are plain prose/prompt text). Strip the page title (the file
   body is the content). Factor the render loop so both `sync_to_dir` and `page_to_markdown` share it.
2. **`compose.py` — add a `notion_pages` branch in `run_inputs`.** For each `{url, to}` in
   `spec["pages"]`: `md = notion.page_to_markdown(url)`; write to `cfg.ap(to)` only if changed
   (mkdir parents); count synced/refreshed; `lock[handle]` lists per-page `{to, sha, refreshed}`.
   Mirror the existing gdoc/notion lock + idempotence style.
3. No new creds (reuses the `notion` connector token).

## Step 00 + the run skill
- **Step 00 (sync):** `bizconnect compose run inputs` now pulls BOTH the recursive mirror (research
  library + context) AND the definitional pages → their repo files.
- **Run skill:** a Claude skill drives the whole pipeline end-to-end:
  `compose run inputs` (step 00, Notion→files) → `compose run draft all` → `compose run slide all`
  → `deck build` → `deck preview`. The user edits the Notion definitional pages; the skill re-syncs
  and rebuilds.

## Notion master pages (the “starting materials”)
Under the hub's **"Outline shape"** page (`381e4fd08136807f93c3cc133dc131f6`), one sub-page per
definitional doc, seeded with starting content:
`slide.md`, `draft.md`, `ladder.md`, `house-position.md` (+ per-section guides later). The
connections.yaml `definitions.pages` map binds each page → its repo file.

## File ownership (when fanned out — disjoint from the deck-extension build)
- `bizconnect/connectors/notion.py` (+ `compose.py run_inputs`) — the sync code.
- `deliverables/us-launch/connections.yaml` — the `definitions` mapping (after the Notion pages exist).
- the run skill (`skills/...`) — separate file.
None of these are the deck-extension files (`deck.py`, `_deck_assets.py`, `requirements.txt`,
`02.prompts/*` content, `skills/build-deck`).
