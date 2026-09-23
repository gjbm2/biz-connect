---
name: notion-sync
description: Keep a project folder's Markdown files and a Notion hub in two-way sync through a mapping file (notion.yaml) — each file mapped to a whole Notion page, to the SECTION of a page under a heading, or (a folder of front-matter files) to a Notion database. Use when the user wants project notes/state/research "in Notion", to publish local Markdown into sections of an existing hand-built Notion page, to pull people's Notion edits back into the repo, to add or change a mapping, or to check what's out of sync. Guarded both ways; never touches unmapped Notion content.
allowed-tools: Bash(python *), Read, Edit, Write
---

# Notion ⇄ local files (mapped sync)

Agents work on Markdown in the repo (versioned by git); people read and edit in Notion. A
`notion.yaml` in the project folder says which file lives where in Notion, and `push` / `pull`
move changes across — **guarded in both directions**, so neither side silently overwrites the
other.

## The mapping file

`<folder>/notion.yaml` (committed). Paths are relative to that folder:

```yaml
hub: https://www.notion.so/...Project-Page-3e4e...      # default container
link_base: https://github.com/org/repo/blob/main/proj/  # optional: where links out of the folder point
map:
  - path: thesis.md
    section: What does Josh think & care about?  # blocks under that heading, up to the next
                                                 # heading of the same or higher level
  - path: hub/insights.md
    page: https://www.notion.so/...Insights-3e4e...  # the whole page (title = the file's `# H1`)
  - path: research/muse.md
    page: new                                    # created as a child page on first push
    in: thesis.md                                # container: another page entry, a URL, or the hub
  - path: log                                    # folder: each .md is a DATABASE ROW
    database: new
    title: Public log
    schema: {date: date, verification: select, themes: multi_select, url: url}
  - path: context/*.pdf                          # files (glob) uploaded into a section/page
    section: Background
```

- The tool writes each target's `id` back into the file. Mappings survive renaming or moving
  pages and headings in Notion. Don't hand-edit `id` / `ids` / `media`.
- **Sections** belong to their file only below the heading. The heading text stays Notion's.
  Everything else on the page (other sections, child pages, databases, uploaded files, embeds)
  is never touched. The file's own `# H1` is its title locally, and its `##` headings nest under
  the section heading.
- **Database rows:** YAML front-matter becomes the properties and the body becomes the row page.
  Identity is a `Key` property (the file's stem). Rows added in Notion are pulled as new files.
  Types come from the live database, then `schema`, then inference. Select options are created
  on first use.
- Pages and databases created by the API land at the **end** of their container. Tell the user
  they can drag them into place in Notion (ids keep the mapping intact).
- Images pulled from Notion are downloaded to `media/<file-stem>/` next to the file.

## How to run

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion'
$B link    projects/x <hub-url>                                  # create projects/x/notion.yaml
$B outline projects/x                                            # the hub's headings / sub-pages / dbs, with ids
$B map     projects/x/thesis.md --section "What we think"        # add entries (or edit notion.yaml)
$B map     projects/x/hub/insights.md --page <url>
$B map     projects/x/notes/a.md --page new [--in thesis.md]
$B map     projects/x/log --database new --title "Log"
$B status  projects/x [--deep]                                   # verdict per item (no writes)
$B pull    projects/x [--dry-run] [--force]                      # Notion -> files
$B push    projects/x [--dry-run] [--force] [--prune]            # files -> Notion
```

A path argument can be the folder, a sub-folder or a single mapped file (that scopes the run).
With no path, the command covers every `notion.yaml` in the repo. The exit code is 1 when
something was refused (a conflict, remote-ahead on push, local-ahead on pull) or errored.

Verdicts: `in-sync`, `local-ahead` (push it), `remote-ahead` (pull it), `conflict` (changed
both sides), `new-local`, `new-remote`, `local-deleted`, `remote-deleted`, and `missing`
(the section heading or page wasn't found).

## Workflow

1. **Start of a session on the project: `pull` first.** This takes people's Notion edits
   before you change anything. If `pull` reports `local-ahead`, local work hasn't been pushed
   yet. That's fine, but push it when you're done.
2. Edit the Markdown files, then `push`, then commit the files **and `notion.yaml`** (the ids
   it recorded). "Done" means pushed **and** committed.
3. **First mapping onto a section people already wrote in:** `pull` it first to get their text.
   `push` refuses to overwrite hand-written content it has never synced. Use `push --force` only
   when the Notion text is a placeholder meant to be replaced (e.g. "[populate from research]"),
   and say so to the user.
4. **Conflicts:** never `--force` blindly. Run `status`, look at both versions, merge the
   Notion changes into the file by hand (or ask the user), then `push --force` that one path.
5. `--prune` archives database rows whose files were deleted locally. It's off by default.
6. Row verdicts use a cheap timestamp check. Use `status --deep` to re-read every row body.

## What round-trips

Headings, paragraphs, bold/italic/strike/code, links, bulleted/numbered/to-do lists (nested),
quotes, `> [!NOTE]` callouts, code blocks, dividers, tables and images all round-trip. Other
blocks (toggles, embeds, columns, synced blocks, files, child pages/databases) show as
`<!-- notion:... -->` comments on pull. They are **never** changed by push. Colours, mentions
and comments survive on any block whose text didn't change, because unchanged blocks are left
alone.

## Troubleshooting

- `missing`: the heading was deleted or renamed before an `id` was recorded. Fix the `section:`
  text, or run `outline` to find it.
- `cannot read page` / 404: share the page with the integration (••• → Connections).
  `notion whoami` shows which integration the token is.
- Credentials are in the central store (`bizconnect doctor`). No Notion MCP is needed.
