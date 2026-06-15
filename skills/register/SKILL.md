---
name: register
description: Maintain the open-points register — the Notion-database-backed status table for review feedback (one row per point raised on a draft, with status / disposition / owner / references). Use to create the register, pull it into the local projection, list open or gated points, mark a point resolved, or read the ingestion journal. The register is the stateful spine of the feedback roundtrip; the `compose` assimilate stage writes triaged deltas into it via `register upsert`. See the `feedback-ingest` skill for the full loop.
allowed-tools: Bash(python *), Read, Edit, Write
---

# Open-points register (`register`)

A Notion database is the live home of the register; a local Markdown **projection** (read-only)
is what the pipeline reads. Every point has a stable `ISS-nnn` id that threads the whole
feedback cycle: the in-text marker (`[DECISION: ISS-014 …]`), the assimilate deltas, the
deliberation brief, and the eventual source edit all reference it.

Bound per-repo in `connections.yaml` under `notion.register_db` (`database_id`, `url`,
`project_to`, `journal`). Credentials reuse the `notion` connector's token — nothing extra.

## Verbs

```bash
BC='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" register'
$BC init [--parent <page-url/id>]      # create the Notion DB + write the binding into connections.yaml
$BC pull                               # query rows -> rewrite the local projection (project_to)
$BC upsert <deltas.json|cycle.gen.md>  # create/update rows from assimilate output (dedupe by comment-id)
$BC open  [--question Q07]             # the OPEN points slice (what compose injects as {{OPEN_POINTS}})
$BC status                             # counts by status/disposition; open-&-gated count; reachability
$BC resolve ISS-014 [--note "..."]     # mark a point resolved + append History
$BC journal                            # the per-cycle ingestion journal (audit trail)
```

## Field ownership (do not clobber)

`upsert` is **clobber-safe at the field level**, mirroring how `compose` never overwrites a
human-owned file:

- **Ingest owns** — `ISS`, `Disposition`, `Questions`, `Layer`, `Targets`, `Marker`,
  `DocLink`, `Author`, `SourceCommentId`, and the Input/Interpretation prose.
- **Humans own** — `Status`, `Owner`, `Agreed steps`, and Commentary they add in Notion.

On an existing row, `upsert` fills only machine fields and **appends** to the History block;
it never overwrites a human decision. Always `pull` before `upsert`.

## Lifecycle

`open → triaged → in-discussion → agreed → actioned → rebuilt → resolved`
(plus `parked` / `superseded` / `wont-do`). `finesse`/`tonal` points usually skip straight to
`actioned`; `rethink`/`research`/`discussion` are gated and surface in the deliberation brief.

## First-time setup

1. Set `notion.notes_page` (the parent) in `connections.yaml`, and a `notion.register_db`
   block with `project_to` + `journal` paths (the repo's register doc + journal dir).
2. `register init` — creates the DB under the parent page and writes back `database_id`/`url`.
3. The Notion integration that owns `NOTION_TOKEN` must be connected to the parent page.
