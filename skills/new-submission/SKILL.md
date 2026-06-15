---
name: new-submission
description: Stand up a new automated-document deliverable in an umbrella repo — scaffold deliverables/<slug>/, designate a Notion hub page as the submission (mark it + provision its open-points register and docs-registry databases), and wire it into connections.yaml. Use when the user wants to start a new consultation response / report / submission, "add a deliverable", "set up a new automated document", or designate a Notion page as a submission. Pairs with doc-pipeline (to then build it) and register/docreg (the equipment it provisions).
allowed-tools: Bash(python *), Read, Edit, Write
---

# Start a new automated document (a deliverable / submission)

An umbrella repo hosts many deliverables under `deliverables/<slug>/`, each its own document with
its own Notion hub, open-points register, docs-registry and output Doc. **The repo is the
registry** — `connections.yaml` (`deliverables.<slug>`) plus the `deliverables/<slug>/` folder are
the source of truth for what exists. There is **no central "submissions" database in Notion**; a
Notion page is "a submission" because we provisioned its equipment onto it and recorded it in the
repo. Designating a page is opt-in: only the pages you run this on become submissions.

## What "a submission" gets

1. A repo folder `deliverables/<slug>/` — its own `pipeline.yaml` (`deliverable: <slug>`),
   `response/` tree, `index/`, `final/`, scaffolded ready to author.
2. A `deliverables.<slug>` block in the umbrella `connections.yaml` (its register/docs-registry
   bindings + optional Drive subfolder).
3. On its **Notion hub page**: the open-points **register** DB + the **docs-registry** DB (as
   inline child databases), a 📨 icon + a marker callout, and links to the getting-started pages.

## The sequence

Let `<slug>` be a short kebab id (e.g. `cma-energy-data-2027`) and `<hub>` the Notion page that
will be this submission's home (an existing planning page, or one you create under the umbrella
"Consultations and regulation" page first).

```bash
BC='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'

# 1. Scaffold the repo folder + connections.yaml stub, and mark the hub page as a submission
#    (📨 icon + callout + getting-started links). Run from the UMBRELLA repo root.
$BC deliverable new <slug> --title "Human title" --hub <hub> [--drive-folder <subfolder-id>]

# 2. Provision the equipment ON the hub page — run these from INSIDE the new folder so the
#    bindings land under deliverables.<slug> in connections.yaml:
cd deliverables/<slug>
$BC register init --parent <hub>      # open-points register DB (inline child DB on the hub)
$BC docreg   init --parent <hub>      # docs-registry DB (inline child DB on the hub)
```

> **Notion access is a prerequisite for step 2.** `register init` / `docreg init` create databases
> *on* `<hub>`, and the build later scrapes `<hub>` as a research input — both need the `nous-reg
> pipeline` Notion connection to reach `<hub>`. If that connection is attached at the umbrella
> *Consultations and regulation* page (the recommended setup — see `tooling/README.md` §1), every
> hub beneath it is already covered and there's nothing to do. If it's scoped per-hub instead, open
> `<hub>` → ••• → **Add connections** → `nous-reg pipeline` *before* running step 2, or init fails
> with a Notion permission error. `deliverable new --hub <hub>` records `<hub>` as each database's
> `parent` in the stub, so `register init` / `docreg init` target the right page even if you omit
> `--parent`.

`deliverable new` is idempotent-ish: it refuses to overwrite an existing `deliverables/<slug>/`.
`register init` / `docreg init` skip creation if already bound. To designate an **existing** hub
that already has its planning content, just pass it as `--hub`; to start from scratch, create the
Notion page first (under the umbrella page) and pass its URL.

## Then author the content (these are inputs the build reads, never regenerates)

Inside `deliverables/<slug>/`:
- `index/questions.json` — the items (the consultation's questions / the report's sections).
- `response/01.context/house-position.md` + `response/01.context/questions/<id>.md` — the global
  guide and per-item guides.
- `response/02.prompts/*` — the stage prompts (copy from a sibling deliverable and adapt the house
  style, or author fresh).
- `response/05.submission/front-matter-template.md` — the front-matter skeleton.
- Optionally `index/corpus.json` (evidence index) and a `deliverables.<slug>.inputs` block in
  `connections.yaml` for source docs to sync. If one of those inputs is the hub itself (a `notion`
  input), list **this deliverable's own** `register_db` + `docs_registry` database ids under its
  `exclude:` so the scrape doesn't recurse into the output databases it already round-trips. (The
  ids are written into `deliverables.<slug>` by `register init` / `docreg init`.)

Then build it with the **doc-pipeline** skill (`compose status` from inside the folder). Shared
umbrella assets (e.g. company background) are referenced with a `//` path
(`intro: //nous-background.md`).

## Notes

- `deliverable list` shows every deliverable in the repo (slug · title).
- Keep the umbrella docs in sync (the **Notion mirror** rule): a new deliverable should appear in
  the repo `README.md` active-deliverables list; the umbrella's "how to start a new automated
  document" guidance is the team-facing version of this skill.
- Capabilities deliberately NOT used: a central Notion "submissions" database (the repo is the
  registry), Notion template buttons / linked views (not reliably API-creatable). If you later
  want a cross-repo dashboard, add it as a linked view by hand in Notion.
