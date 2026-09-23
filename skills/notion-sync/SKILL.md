---
name: notion-sync
description: "Keep a project folder's Markdown files and Notion in two-way sync through a mapping file (notion.yaml) — each file mapped to a whole Notion page, to the SECTION of a page under a heading, a folder of notes to child pages, or a folder of front-matter files to a Notion database. Use when the user wants project notes/research/state kept \"in Notion\", to publish local Markdown into sections of an existing hand-built Notion page, to pull people's Notion edits back into the repo, to set up or change a mapping, to fill in or update one block the user links to (a URL ending in #<block-id>: fill in that slot only, via `notion locate`), or to check or compare what's out of sync. Guarded both ways; never touches unmapped Notion content."
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Edit, Write
---

# Notion ⇄ local files (mapped sync)

Agents work on Markdown in the repo (versioned by git); people read and edit in Notion. A
`notion.yaml` in the project folder says which file lives where in Notion, and `push` / `pull`
move changes across — **guarded in both directions**, so neither side silently overwrites the
other.

Every command here is `bizconnect notion <verb> ...`. Inside Claude Code the plugin puts
`bizconnect` on PATH. If it isn't found (the plugin was installed or updated in this session, or
you are working in a clone), run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion status projects/x` (use `python`
or `py` on Windows).

## Prerequisites (check once per machine and repo)

- **biz-connect 0.13 or later.** `bizconnect update` shows the installed version. To upgrade:
  `/plugin marketplace update biz-connect`, then `/plugin update biz-connect`, then start a new
  session (skills added by an update only load at session start). Older versions fail with
  `unknown notion verb` or `map entry ... needs one of page / section / database`, and have no
  `bizconnect` command (it still isn't found after a restart).
- **`NOTION_TOKEN`** in `~/.config/biz-connect/secrets.env` (`bizconnect init` creates the file).
  It is the secret of a Notion internal integration made at
  https://www.notion.so/profile/integrations with **Read content, Update content and Insert
  content**. `bizconnect notion whoami` names the integration.
- **The hub page shared with that integration:** in Notion, page ••• menu → Connections → add
  it. Sub-pages inherit the access. `bizconnect notion check <hub-url>` prints `OK` when it can
  read the page.
- **`bizconnect init` once at the repo root** (recommended). It writes `connections.yaml`, which
  marks the repo root (the sync needs no keys in it), and git-ignores `.bizconnect/`, where each
  machine keeps its sync state. With no `connections.yaml`, the sync uses the git top-level as
  the root; then add `.bizconnect/` to `.gitignore` yourself.
- Not needed: the Notion MCP (only for ad-hoc search or editing by hand) and a Google service
  account (only for the Google connectors).

## Quick path: "fill in / update this block" (a link ending in `#<block-id>`)

The link points at **one block**, usually a placeholder like "[to follow]". The scope is that
slot. Replace it, and touch nothing else on the page: not the rest of its section, and not the
page's other placeholders. "One block" limits **where** you write, not **how**. The replacement
can be a bold lead line plus a few short bullets if that reads better. Context the user gives
("given project X") is what you write *from*.

Aim for about five tool calls:

```bash
bizconnect notion locate "<link>"                              # block text, its section, the file that maps it
bizconnect notion locate "<link>" --map projects/x/<name>.md   # only if it says "mapped no": maps the section and pulls it
#   write the replacement into the file, changing only that block's lines
bizconnect notion push projects/x/<name>.md                    # prints "N added, 1 removed, M unchanged — only under '<heading>'"
```

Then commit the file and `notion.yaml` following this repo's conventions (see the git-flow
skill). Push to the git remote only if the user or the repo's rules say so.

- **Already mapped?** `locate` names the file. `pull` it, edit only the target block's lines,
  then `push`.
- **No `--force`.** `--map` pulls the placeholder first, so replacing it is an ordinary push.
- **The push line is the check.** Don't re-read the page unless it reports something odd.
- **Research in proportion.** For an internal background block (a bio, a definition, context),
  use what's already in the project folder plus one or two good sources, unless the repo's own
  rules set a different bar. The verbatim, primary-source bar is for quotes and outward-facing
  deliverables.
- **If the tool can't do it, say so.** Don't read the tool's source or call the API by hand.
- **Commit only your own file and your `notion.yaml` entry.** Other sessions may have
  uncommitted work in the same folder.

## Writing style

Follow the repo's house style if it has one (e.g. a "House style" section in its CLAUDE.md).
Otherwise:

- Lead with the answer, in one bold line.
- Keep paragraphs to 3 sentences or about 60 words. Put parallel facts (a career, a list of
  options) in bullets, one line each.
- Label inference once, on its own line ("Our reading: ...").
- Put sources on one line at the end, not after every clause.
- Match the form of the page around it.

`push` warns about any paragraph over `style.max_para_words` (default 80; set it in `notion.yaml`
as `style: {max_para_words: 60}`, or repo-wide in connections.yaml under `notion.style`).

## The mapping file

`<folder>/notion.yaml` (committed). Paths are relative to that folder. A complete example for a
folder `projects/x/`:

```yaml
hub: https://www.notion.so/acme/Project-Hub-<page-id>       # default container
link_base: https://github.com/org/repo/blob/main/projects/x/  # optional: where links out of the folder point
map:
  - path: thesis.md
    section: Current thinking            # blocks under that heading on the hub, up to the next
                                         # heading of the same or higher level
  - path: open-questions.md
    section: Open questions
    create: true                         # push may add this heading if the page doesn't have it yet
  - path: hub/insights.md
    page: https://www.notion.so/acme/Insights-<page-id>  # the whole page (title = the file's `# H1`)
  - path: notes/overview.md
    page: new                            # created as a child page on first push ...
    in: hub/insights.md                  # ... inside another page entry (or a URL; default: the hub)
  - path: research                       # folder of NOTES: each .md (e.g. research/competitor-a.md)
    pages: new                           # is its own page under a folder page (new, or a URL);
    title: Research notes                # a new file is published on the next push
  - path: log                            # folder: each .md is a DATABASE ROW
    database: new
    title: Decision log
    schema: {date: date, status: select, tags: multi_select, url: url}
  - path: context/*.pdf                  # files (glob) uploaded into a section/page
    section: Background
```

- Each file is mapped **once**. A file inside a `pages:` or `database:` folder belongs to that
  folder's entry; don't list it again. `in:` names another **page** entry by its path in this
  file (a `page:` entry, or a `pages:` folder), a Notion page URL, or is left out for the hub.
- The tool writes each target's `id` back into the file, so mappings survive renaming or moving
  pages and headings in Notion. It also writes `synced`: a fingerprint of the content both sides
  held at the last sync. That is how any machine or clone knows which side has changed since, so
  **commit `notion.yaml` with the files**. Don't hand-edit `id` / `ids` / `media` / `synced`.
- **Sections** belong to their file only below the heading. The heading text stays Notion's.
  Everything else on the page (other sections, child pages, databases, uploaded files, embeds)
  is never touched. The file's own `# H1` is its title locally, and its `##` headings nest under
  the section heading.
- **The heading must exist,** unless the entry has `create: true`. Then `push` adds it at the
  end of the page, at the page's outermost heading level (or `level: N`). Drag it into place in
  Notion; the id keeps the mapping. Until that first push, `status` and `pull` show the entry as
  `new-local` (or `skipped` while the file doesn't exist); neither blocks.
- `map --section` writes `create: true` only for a heading that is genuinely new. If the page
  has a close match (a typo) or the heading sits inside a column or toggle, `map` says so and
  leaves `create` out, so push reports `missing` rather than adding a near-duplicate. Re-run
  `map` with `--create` if you really want a new heading next to a similar one.
- A mapped section can't contain another mapped section (a lower-level heading inside it): both
  would own the same blocks. The sync refuses both with the reason. Give the headings the same
  level in Notion, or map only one.
- **Database rows:** YAML front-matter becomes the properties and the body becomes the row page.
  Identity is a `Key` property: the file's name without `.md`. Rows added in Notion are pulled as
  new files, and pull writes a Key onto them. A row file name (so a Key) can be up to 120
  characters. It can't start with `.` or a space, or use any of `\ / : * ? " < > |`. A local
  file that breaks this is refused with "rename …", and a Notion row whose Key breaks it is
  pulled under a safe name. A file that can't be read (not UTF-8, broken front-matter) is an
  error and is never treated as deleted. If someone changes a synced row's Key in Notion, the
  sync says so rather than creating a duplicate. Types come from the live database, then
  `schema`, then inference. Select options are created on first use.
- Pages and databases created by the API land at the **end** of their container. Tell the user
  they can drag them into place in Notion (ids keep the mapping intact).
- **Folders of notes** (`pages:`) are the easy path for research: an agent writes a new `.md`
  into the folder, and the next `push` publishes it as a page. Pages people add under the
  folder page in Notion are pulled as new files.
- Images pulled from Notion are downloaded to `media/<file-stem>/` next to the file.

## How to run

```bash
bizconnect notion link    projects/x <hub-url>                            # create projects/x/notion.yaml
bizconnect notion outline projects/x                                      # the hub's headings / sub-pages / dbs, with ids
bizconnect notion locate  "<page-url>#<block-id>" [--map projects/x/f.md]  # where a linked block lives (+ map/pull its section)
bizconnect notion map     projects/x/thesis.md --section "Current thinking" [--level N] [--create]  # add entries (or edit notion.yaml)
bizconnect notion map     projects/x/hub/insights.md --page <url>
bizconnect notion map     projects/x/notes/overview.md --page new --in hub/insights.md
bizconnect notion map     projects/x/research --pages new --title "Research notes"
bizconnect notion map     projects/x/log --database new --title "Decision log"
bizconnect notion status  projects/x [--deep]                             # verdict per item; makes no writes
bizconnect notion diff    projects/x/thesis.md                            # unified diff of Notion vs the local file
bizconnect notion pull    projects/x [--dry-run] [--force]                # Notion -> files
bizconnect notion push    projects/x [--dry-run] [--force] [--prune]      # files -> Notion
```

`map` takes the file's path from where you run it (here, the repo root). `--in` is written into
`notion.yaml` as-is, so give it relative to the folder that holds `notion.yaml`. `link` also
prints a short snippet for the repo's CLAUDE.md (see below).

A path argument can be the folder, a sub-folder or a single mapped file (that scopes the run).
A path that `notion.yaml` doesn't map is an error (exit 1), not a silent no-op. With no path,
`status` / `pull` / `push` cover every `notion.yaml` in the repo. The exit code is 1 when
something was refused (a conflict, no-baseline, remote-ahead on push, local-ahead on pull,
missing) or errored. `diff` takes one mapped file.

Verdicts: `in-sync`, `local-ahead` (push it), `remote-ahead` (pull it), `conflict` (changed on
both sides since the last sync), `no-baseline` (no record of any earlier sync and the two sides
differ; see "New machine" below), `new-local`, `new-remote`, `local-deleted`, `remote-deleted`,
`missing` (a section heading or `database:` folder wasn't found, or an upload's target), `skipped`,
`re-keyed` (pull set a row's cleared or mangled Key back to its file's name), and `error`.

## Workflow

1. **Start of a session on the project: `pull` first.** This takes people's Notion edits
   before you change anything. If `pull` reports `local-ahead`, local work hasn't been pushed
   yet. That's fine, but push it when you're done.
2. Edit the Markdown files, then `push`, then commit the files **and `notion.yaml`** (the ids
   and `synced` fingerprints it recorded) following the repo's git conventions. "Done" means
   pushed to Notion **and** committed.
3. **First mapping onto a section people already wrote in:** map it and `pull` **before** you
   create the local file. The pull writes their text into the new file (`locate --map` does
   both steps). If you already wrote the file, the item is `no-baseline`; handle it as below.
4. **Replacing a placeholder:** use `push --force <path>` only when the Notion text is a
   placeholder meant to be replaced (e.g. "[populate from research]"), and say so to the user.
5. **Conflicts:** never `--force` blindly. `bizconnect notion diff <path>` shows the Notion
   side against the file. Either merge the Notion changes into the file by hand and then
   `push --force <path>` for that one path, or: commit the file, `pull --force <path>` (takes
   Notion's version and records it as the baseline), re-apply your own changes from
   `git diff`, then a plain `push`. If it's unclear whose edit should win, ask the user. A pull
   that overwrites uncommitted local changes first copies the file to `.bizconnect/backup/`
   (the pull line names the copy).
6. **`--prune`** (off by default) archives notes, database rows and uploaded files whose local
   file was deleted, but only items this machine has synced. An item this clone never synced
   (say, a file that isn't checked out here) is never treated as deleted.
7. Row verdicts use a cheap timestamp check. Use `status --deep` to re-read every row body.

## New machine / teammate's clone

The ids and the `synced` fingerprints live in the committed `notion.yaml`, so a fresh clone, or
the same repo on a second machine, knows what both sides held at the last sync. It can tell a
Notion edit (`remote-ahead`) from a commit that was never pushed (`local-ahead`), and both from
a `conflict`. Run `bizconnect notion status <folder>`, then `bizconnect notion pull <folder>`,
before editing anything, as usual.

A per-machine record (`.bizconnect/state.json`, git-ignored) backs this up. It is only used for
items last synced by a biz-connect older than 0.13, which wrote no fingerprint. An item with
neither record whose two sides differ is `no-baseline`. `push` and `pull` both refuse it,
because nothing says which side moved. Compare with `bizconnect notion diff <path>`, then run
`bizconnect notion pull --force <path>` to take Notion's version, or
`bizconnect notion push --force <path>` to publish the file. If both sides have changes worth
keeping, merge them as for a conflict. Ask the user when unsure. Every sync writes the
fingerprint, so this happens at most once per item.

## Keep the rules always on: paste into the repo's CLAUDE.md

This skill loads only when a request mentions Notion. An agent asked to "update the research
notes" would otherwise edit without pulling first. Offer the user this block for the repo's
CLAUDE.md (`bizconnect notion link` prints a similar one):

```markdown
## Notion sync (biz-connect)
Folders that contain a `notion.yaml` are kept in two-way sync with Notion (notion-sync skill).
- Before editing files in such a folder, run `bizconnect notion pull <folder>`.
- Edit the Markdown files, not the mapped content in Notion (people edit there; agents edit the files).
- When done: `bizconnect notion push <folder>`, then commit the changed files together with `notion.yaml`.
- Never `--force` over other people's Notion edits. On a conflict or no-baseline, run
  `bizconnect notion diff <path>` and merge, or ask.
```

## What round-trips

Headings, paragraphs, bold/italic/strike/code, links, bulleted/numbered/to-do lists (nested),
quotes, `> [!NOTE]` callouts, code blocks, dividers, tables and images all round-trip. Other
blocks (toggles, embeds, columns, synced blocks, files, child pages/databases) show as
`<!-- notion:... -->` comments on pull. They are **never** changed by push. Colours, mentions
and comments survive on any block whose text didn't change, because unchanged blocks are left
alone.

Limits:

- A block with more than 100 styled pieces or links in its text is refused with an error that
  names it, rather than truncated. Split the paragraph and push again.
- The sync pins the Notion API version it needs (2022-06-28) for databases and rows. Don't set
  `NOTION_VERSION` in `secrets.env` or the environment.
- On that API version Notion can't use a database that has more than one data source. If a
  synced database starts failing after someone added a data source to it in Notion, remove the
  extra source; don't clear the database's `id` (that would create a second database).

## Troubleshooting

- `no-baseline`: see "New machine / teammate's clone".
- `conflict`: see Workflow step 5.
- Push refuses `remote-ahead`: `pull` first, then push. Pull refuses `local-ahead`: push first
  (or `pull --force <path>` to discard the local edit, after asking).
- `missing`: the heading wasn't found (a typo, it sits inside a column or toggle, or it was
  renamed or deleted before an `id` was recorded). The message names the nearest headings. Fix
  the `section:` text, or run `outline`. Add `create: true` only if the heading really is new.
- `… is not mapped in …/notion.yaml`: the path is mistyped, or the file isn't mapped yet
  (`map` it).
- `lies inside … section`: two mapped sections overlap. See "The mapping file".
- A row reported `remote-deleted` with "its Key in Notion is now …": someone changed the Key.
  Set it back in Notion, or rename the file to match. Don't `push --force` it.
- `cannot read page` / 404 / "is it shared with the integration?": share the page with the
  integration (••• → Connections). `bizconnect notion whoami` shows which integration the token
  belongs to, and `bizconnect notion check <url>` tests one page.
- `no connections.yaml or git repository found`: you're outside a git repo and no
  `connections.yaml` is above the working directory. Run `bizconnect init` at the repo root.
- `bizconnect: Permission denied` (macOS/Linux): use the launcher,
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" …`, and update the plugin.
- Windows PowerShell splits a URL containing `&` (e.g. a side-peek link `…?v=…&p=…`) when it
  runs `bizconnect.cmd`. Pass the page id alone, or quote the URL as `'"<url>"'`, or run the
  command from Git Bash.
- `unknown notion verb 'diff'` or `needs one of page / section / database`: the plugin is older
  than 0.13. Update it and start a new session (see Prerequisites).
- Credentials live in the central store (`bizconnect doctor`). No Notion MCP is needed.
- Several sessions syncing one folder: `notion.yaml` and the sync state are merged on save, so
  nobody's ids get dropped. Still, don't run two pushes on the same folder at the same moment.
