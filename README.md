# biz-connect

Business-service connectors for Claude Code, shareable across projects and users.
Connect any repo to **Google Drive/Docs**, **Notion**, **Google Sheets**, and a
standardised **git** flow — with per-repo bindings and a single per-user credential
store. Packaged as a Claude Code **plugin** (skills + CLI).

## Getting started

biz-connect is a Claude Code **plugin** — there is **nothing to clone** to use it.

**Mental model — set up in this order** (see [The model: three layers](#the-model-three-layers) below for the canonical table):

1. **Install the plugin** (once per machine) — from GitHub, no clone needed.
2. **Set up *your own* credentials** (once per user) — in the *central store* at `~/.config/biz-connect/`. Secrets never live in any repo.
3. **Connect each repo** (once per repo) — a committed `connections.yaml` holding only IDs/URLs (the *attachpoints*), plus a `notion.yaml` in each folder you sync with Notion.

Rotate a credential once in the central store and every repo picks it up. The launcher
bootstraps its own dependency `.venv` on first run — no manual `pip install`, ever. It needs
Python 3.9 or later.

### 1. Install the plugin (once per machine)

In the Claude Code REPL:

```text
/plugin marketplace add gjbm2/biz-connect
/plugin install biz-connect@biz-connect
```

Or, from a clone, run the installer — if the `claude` CLI is on your PATH it runs the two
commands above and then `doctor`; otherwise it prints them for you to paste into the REPL:

```text
scripts/install.sh      # macOS/Linux
scripts\install.ps1     # Windows
```

(For local development on a clone, install from a path: `/plugin marketplace add C:/path/to/biz-connect`.)

After the next session start, these skills are available in **every** project:
`gdoc-sync`, `notion-sync`, `notion-notes`, `sheet-io`, `workbook-diff`, `git-flow`, `doc-pipeline`,
`feedback-ingest`, `register`, `new-submission`, `biz-connect-setup`.

> **Restart Claude Code (start a new session) before running steps 2–3.** Skills and the
> `bizconnect` command load only at session start.

### Running commands

Inside Claude Code the plugin's `bin/` is on PATH, so every command is `bizconnect <service>
<verb> ...` (for example `bizconnect doctor`, `bizconnect notion status projects/x`). The
`bizconnect` shim runs the launcher with the first working Python 3.9+ it finds (set
`BIZCONNECT_PYTHON` to choose one).

If `bizconnect` isn't found (the plugin was installed in the current session, or you are in
your own terminal outside Claude Code), call the launcher directly with the same arguments. It
is `scripts/bizconnect.py` in the installed plugin. The plugin's folder is the `installPath`
listed for `biz-connect` in `~/.claude/plugins/installed_plugins.json`, usually
`~/.claude/plugins/cache/biz-connect/biz-connect/<version>/`:

```bash
python3 ~/.claude/plugins/cache/biz-connect/biz-connect/<version>/scripts/bizconnect.py doctor   # macOS/Linux
py "%USERPROFILE%\.claude\plugins\cache\biz-connect\biz-connect\<version>\scripts\bizconnect.py" doctor   # Windows
```

(The skills write this as `"${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"`. Claude Code fills
that in only inside skill text; in a shell it is empty.) In a clone of this repo, use
`bin/bizconnect` or `scripts/bizconnect.py`.

### 2. Set up your credentials (once per user)

These NEVER live in any repo — they go in the per-user central store at `~/.config/biz-connect/`.

```bash
bizconnect init      # creates ~/.config/biz-connect/secrets.env (and connections.yaml in the current dir, see step 3)
#   Notion: put NOTION_TOKEN=... in secrets.env (below)
#   Google: drop your service-account.json into ~/.config/biz-connect/
bizconnect doctor    # reports each part of the setup
```

**Notion** (for `bizconnect notion …`, including the two-way file sync):

1. Create an internal integration at https://www.notion.so/profile/integrations (New
   integration → Internal). Give it the capabilities **Read content**, **Update content** and
   **Insert content**: the sync and uploads create, edit and archive blocks and pages, so a
   read-only integration fails at push time.
2. Copy its secret into `NOTION_TOKEN=...` in `~/.config/biz-connect/secrets.env`.
3. Share the hub page with the integration: open the page → ••• menu → Connections → add it.
   Sub-pages inherit the access.
4. Check it: `bizconnect notion whoami` prints the integration's name, and
   `bizconnect notion check <page-url>` prints `OK` when the integration can read that page.

Everything under `bizconnect notion` needs only that token plus page sharing: the sync verbs
(`link map outline locate diff status push pull`) and `whoami check read upload fill sync`.

**Google** (only for the Google connectors: `gdoc`, `sheet`, Doc inputs to `compose`): drop
your `service-account.json` into `~/.config/biz-connect/` (or set `GOOGLE_SERVICE_ACCOUNT_FILE`),
and share each Doc/Sheet/folder with the service-account email that `doctor` prints. If you
only use Notion, a missing service account is expected.

**Notion MCP (optional).** biz-connect doesn't use it. Connect it only if you want ad-hoc
search or editing by hand in a session (`doctor` does not check it):

```text
claude mcp add --transport http notion https://mcp.notion.com/mcp
```

then `/mcp` in the REPL and authenticate.

### 3. Connect a repo (once per repo)

From the repo root:

```bash
bizconnect init      # writes connections.yaml + .gitignore guards for secrets and sync state
# edit connections.yaml attachpoints you use: google.share_with, google.drive_folder, notion.notes_page, ...
```

`connections.yaml` is committed and holds only IDs/URLs (no secrets); if an ancestor
`connections.yaml` already exists, `init` leaves it as-is. Notion file sync is configured per
folder in a committed `notion.yaml` (below); for the sync, `connections.yaml` only marks the
repo root. Then ask Claude, or run `bizconnect gdoc push <file.md>`, `bizconnect notion …`, etc.
Agents: see `CLAUDE.md`.

### Notion sync quick start

Two-way sync of a folder's Markdown files with Notion — a file ↔ a whole page, a section under
a heading, a folder of notes ↔ child pages, or a folder of front-matter files ↔ a database.
Needs **biz-connect 0.13 or later** (`bizconnect update` shows your version) and the Notion
setup above. Full guide: the **notion-sync** skill.

```bash
bizconnect init                                                  # once per repo (recommended)
bizconnect notion link projects/x https://www.notion.so/...      # writes projects/x/notion.yaml bound to the hub page
bizconnect notion outline projects/x                             # the hub's headings, sub-pages and databases
bizconnect notion map projects/x/thesis.md --section "Current thinking"
bizconnect notion map projects/x/research --pages new --title "Research notes"
bizconnect notion pull projects/x                                # bring existing Notion text down first
#   ...edit the Markdown files...
bizconnect notion push projects/x                                # publish; refuses anything changed in Notion meanwhile
git add projects/x && git commit -m "..."                        # commit the files AND notion.yaml (ids + sync fingerprints)
```

- `status` shows a verdict per item and makes no writes; `diff <path>` shows Notion vs the file.
- `notion.yaml` records a fingerprint of what both sides held at each item's last sync
  (`synced`). Because it is committed, a new machine or a teammate's clone can tell a Notion
  edit from a commit that was never pushed. `pull` before editing, as always.
- An item with no record of any sync (last synced by biz-connect < 0.13) whose two sides differ
  is `no-baseline`: compare with `bizconnect notion diff <path>`, then `pull --force <path>` to
  take Notion's version or `push --force <path>` to publish the file. A pull that overwrites
  uncommitted changes keeps a copy in `.bizconnect/backup/`.
- Don't set `NOTION_VERSION`: the sync pins the API version it needs (2022-06-28).

Paste this into your repo's `CLAUDE.md` so every session follows the rules, not only the ones
that mention Notion (`bizconnect notion link` prints a similar snippet):

```markdown
## Notion sync (biz-connect)
Folders that contain a `notion.yaml` are kept in two-way sync with Notion (notion-sync skill).
- Before editing files in such a folder, run `bizconnect notion pull <folder>`.
- Edit the Markdown files, not the mapped content in Notion (people edit there; agents edit the files).
- When done: `bizconnect notion push <folder>`, then commit the changed files together with `notion.yaml`.
- Never `--force` over other people's Notion edits. On a conflict or no-baseline, run
  `bizconnect notion diff <path>` and merge, or ask.
```

### Setup checklist

- [ ] Plugin installed (`/plugin install biz-connect@biz-connect`) and a **new session started**; 0.13 or later for Notion sync
- [ ] `bizconnect init` run once — `~/.config/biz-connect/secrets.env` exists
- [ ] Notion: integration created (Read, Update and Insert content), `NOTION_TOKEN=...` in `secrets.env`, hub page shared with it; `bizconnect notion whoami` names it
- [ ] Google only: `service-account.json` in `~/.config/biz-connect/` (or `GOOGLE_SERVICE_ACCOUNT_FILE` set), and the Files/Docs/Sheets you'll touch shared with the service-account email (printed by `doctor`)
- [ ] `bizconnect doctor` shows OK for the parts you use
- [ ] `bizconnect init` run in the repo root — `connections.yaml` created and the attachpoints you use edited
- [ ] Optional: Notion MCP connected, only for ad-hoc search or editing by hand

### What isn't automatic

The plugin is shared; **your credentials and access grants are not**. Each user must:

- **Notion** — supply their own `NOTION_TOKEN` (the secret of a Notion internal integration
  with Read, Update and Insert content; see step 2) in `secrets.env`, and **share each Notion
  hub page with that integration** (page → ••• → Connections → add it), or reads return 404.
- **Notion sync** — the shared baseline travels in the committed `notion.yaml`, so commit it
  with the files. Each machine also keeps a local record in `.bizconnect/state.json`
  (git-ignored). On a fresh clone or second machine, run `bizconnect notion pull` on each synced
  folder before editing (see the quick start).
- **Google** — supply their own `service-account.json` in the central store, and **share
  each Doc/Sheet/folder with the service-account email** (Editor). Access is gated entirely
  by sharing — broad scopes don't widen the blast radius.
- **Creating *new* Google Docs** additionally needs domain-wide delegation +
  `GOOGLE_IMPERSONATE_SUBJECT=you@domain` in `secrets.env` (a service account has no Drive
  storage and can't own a new Doc). See [Google Docs ownership](#google-docs-ownership) and
  the **biz-connect-setup** skill.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `bizconnect: command not found` | Plugin not loaded in this session (just installed or updated), or you're outside Claude Code | Start a new session; or run the launcher directly (see [Running commands](#running-commands)) |
| `bizconnect: no Python 3.9+ found` / `python: command not found` | No suitable Python on PATH (macOS and many Linux distros ship `python3`, not `python`) | Install Python 3.9+, use `python3` in the launcher fallback, or `export BIZCONNECT_PYTHON=/path/to/python3` |
| `bizconnect: Permission denied` (macOS/Linux) | The shim lost its executable bit | Run the launcher directly (see [Running commands](#running-commands)) and update the plugin |
| `python` opens the Microsoft Store (Windows) | App-execution-alias stub, no real `python` on PATH | Use `py` with the launcher path (see [Running commands](#running-commands)) |
| `${CLAUDE_PLUGIN_ROOT}` empty / `/scripts/bizconnect.py` not found | That variable is filled in only inside skill text, never in a shell | Use the plugin's real path (see [Running commands](#running-commands)); in a clone, run `bin/bizconnect` |
| PowerShell: a URL with `&` is cut short ("'p' is not recognized") | `bizconnect.cmd` goes through cmd, which splits at `&` | Pass the page id alone, quote the URL as `'"<url>"'`, or run the command from Git Bash |
| `doctor` says some checks failed | A FAIL line: Notion token rejected, `secrets.env` / service account / `connections.yaml` unreadable, or a required dependency missing | Read doctor's per-line output and fix that line; always run via the launcher so the venv bootstraps the deps. A missing `secrets.env` or service account is only a WARNING (the service account matters only for the Google connectors) |
| `gdoc push` of a new file: "storage quota exceeded" | The service account has no Drive storage, so it can't *own* a new Doc | **(A)** domain-wide delegation + `GOOGLE_IMPERSONATE_SUBJECT`; **(B)** point `google.drive_folder` at a Shared Drive; **(C)** create the Doc yourself, share it with the SA, then `gdoc link` (see [Google Docs ownership](#google-docs-ownership)) |
| Google 403/404 on an existing Doc/Sheet | File not shared with the service account | Share it with the service-account email (printed by `doctor`) as Editor, then re-run |
| `missing required secret 'NOTION_TOKEN'` | Token not in `secrets.env` | Add `NOTION_TOKEN=...` (step 2) |
| Notion 404, `cannot read page`, or "is it shared with the integration?" | Page not shared with the integration that owns `NOTION_TOKEN` | In Notion: open the page → ••• → Connections → add the integration; check with `bizconnect notion check <url>` and `bizconnect notion whoami` |
| Notion 401 / 403 on push | Token revoked or regenerated (401); integration lacks Insert or Update content (403) | Copy the current secret into `NOTION_TOKEN`; enable the capabilities in the integration's settings |
| Notion sync: `no connections.yaml or git repository found` | Outside a git repo, with no `connections.yaml` above the working directory | `bizconnect init` at the repo root |
| Notion sync: `… is not mapped in …/notion.yaml` | A mistyped path, or a file not mapped yet | Check the path, or `bizconnect notion map` it |
| Notion sync: `no-baseline` | No record of any earlier sync (last synced before 0.13, or never), and Notion and the file differ | `bizconnect notion diff <path>`, then `pull --force <path>` (take Notion's) or `push --force <path>` (publish the file) |
| Notion sync: `conflict` | Changed both in Notion and locally since the last sync | `bizconnect notion diff <path>`, merge Notion's changes into the file, then `push --force <path>`; never force over other people's edits blindly |
| Notion sync: push refuses `remote-ahead`, pull refuses `local-ahead` | The other side has changes you haven't taken yet | `pull` then `push` (or `push` then `pull`) |
| Notion sync: `missing` | The section heading wasn't found (typo, inside a column or toggle, renamed or deleted); the message names the nearest headings | Fix `section:` in `notion.yaml`, or run `bizconnect notion outline <folder>`; add `create: true` only for a genuinely new heading |
| `unknown notion verb` / `needs one of page / section / database` | Plugin older than 0.13 | `/plugin marketplace update biz-connect`, `/plugin update biz-connect`, then a new session |
| Notion MCP tools missing | The MCP isn't connected; biz-connect doesn't need it | Only if you want ad-hoc search/editing: `claude mcp add --transport http notion https://mcp.notion.com/mcp`, then `/mcp` |
| Impersonation 403 / `unauthorized_client` | SA client id not authorised for the scopes | In Workspace Admin → Domain-wide delegation, authorise the SA client id for the `drive` and `documents` scopes |
| `update` says you're behind right after a release | freshness check is cached (24h), version-driven by `plugin.json` on `main` | `bizconnect update` (forces a check), then `/plugin update biz-connect`; silence with `BIZCONNECT_UPDATE_CHECK=off` |

### Team note

To onboard collaborators without the install commands, commit `connections.yaml` and add a
`.claude/settings.json` that auto-enables the plugin:

```json
{
  "extraKnownMarketplaces": { "biz-connect": { "source": { "source": "github", "repo": "gjbm2/biz-connect" } } },
  "enabledPlugins": { "biz-connect@biz-connect": true }
}
```

Collaborators still set up their **own** central store (step 2) — secrets are never shared via
the repo — and run `bizconnect notion pull` on each synced folder before their first edit.

## The model: three layers

| Layer | Where | Contains | Committed? |
|------|-------|----------|------------|
| **Secrets** | `~/.config/biz-connect/` (the *central store*) | `secrets.env`, `service-account.json`, dependency `.venv` | **No** — per user, machine-level |
| **Toolkit** | this repo / installed plugin | connectors + CLI + skills | Yes (no secrets) |
| **Attachpoints** | `connections.yaml` at the root of each consuming repo | which Doc/folder/page this repo binds to (ids/URLs only); marks the repo root | Yes (no secrets) |
| **Notion sync mappings** | `notion.yaml` in each synced folder | which file maps to which Notion page / section / folder of pages / database; ids and last-sync fingerprints | Yes (no secrets) |
| **Sync state** | `.bizconnect/state.json` in the repo | this machine's own record of each sync (a fallback for items synced before 0.13) | **No** — per machine, git-ignored |

Rotate a credential once in the central store and every repo picks it up. Share the
plugin with a colleague; they add their own central store and use the repo's committed
`connections.yaml` and `notion.yaml` files. No secret ever lives in a project repo.

```
consuming repo (any project)             central store (~/.config/biz-connect)
  connections.yaml  ── attachpoints ─┐     secrets.env        (NOTION_TOKEN, …)
  <folder>/notion.yaml (sync mapping)│     service-account.json
  .bizconnect/state.json (sync state)└───▶ .venv/             (auto-bootstrapped)
        ▲
        │  skills run
   bizconnect (bin/) ── scripts/bizconnect.py ── bizconnect.cli ── connectors/*
```

## Keeping it up to date (self-maintaining)

biz-connect checks **once a day** whether a newer version exists (it compares the
installed `plugin.json` version against the repo's `main`) and prints a one-line nudge
to stderr when you're behind — so it stays current without you remembering to look. It
also nudges if it hasn't been able to verify freshness in a while (offline). On demand:

```text
bizconnect update                          # show installed vs latest + how to apply
/plugin marketplace update biz-connect     # fetch latest from GitHub
/plugin update biz-connect                 # apply (offered when plugin.json version bumps)
```

Updates self-heal: code takes effect on the next launcher run (it imports from the
installed plugin), and dependency changes trigger an automatic venv re-install (the
launcher hashes `requirements.txt`). **Skills added or changed by an update appear only in a
new session**, so start one after updating. The check is throttled, fail-open, and never
blocks a command; disable it with `BIZCONNECT_UPDATE_CHECK=off` in `secrets.env`.

Maintainers: releasing, the roadmap and internal notes are in
[docs/maintainers.md](docs/maintainers.md).

## Connectors (today)

| Service | Verbs | Notes |
|---------|-------|-------|
| `gdoc` | `push pull status link unlink list comments diff resolve docx` | local Markdown ↔ Google Doc; `comments`/`diff`/`resolve` capture review feedback |
| `notion` | `link map outline locate diff status push pull` · `whoami check read upload fill sync` | two-way sync of project Markdown with Notion via a per-folder `notion.yaml` (file ↔ page / section under a heading; `pages:` folder of notes ↔ child pages; `database:` folder of front-matter files ↔ database rows), guarded both ways; plus media upload, headless read, access check and a one-way page-tree mirror (`sync`). Token only, no MCP |
| `sheet` | `whoami check read write append clear create` | service-account Sheets r/w |
| `xlsx` | `diff OLD NEW [--json J] [--summary S] [-o MD] [--formulas] [--values]` · `verify NARRATIVE.md DIFF.json` | row+column-aligned diff of two `.xlsx` workbooks -> a deterministic JSON fact graph (headline metrics, roles, causal links) + capped Markdown; `verify` is the anti-hallucination gate. Local; no credentials. The `workbook-diff` skill turns this into a verified narrative |
| `git` | `status save sync pr` | branch-off-protected, co-author trailer, rebase-sync, PR |
| `compose` | `status run accept scaffold graph` | config-driven document-composition pipeline (`pipeline.yaml`); `inputs` syncs external sources; `assimilate`/`digest` close the feedback loop |
| `deliverable` | `list new` | stand up / list deliverables in an umbrella repo |
| `register` | `init pull upsert open status resolve journal` | Notion-DB open-points register for review feedback (the feedback roundtrip's spine) |
| `docreg` | `init log list pull` | Notion catalogue of produced Doc instances/versions; `gdoc push --new --version` logs each major build |
| `secrets` | `pull status` | pull a repo's shared scoped credentials (GCP Secret Manager) into the central store |

Plus `bizconnect doctor` / `init` / `update` / `version`. Skills (`/biz-connect:gdoc-sync`,
`notion-sync`, `notion-notes`, `sheet-io`, `workbook-diff`, `git-flow`, `doc-pipeline`, `feedback-ingest`,
`register`, `new-submission`, `biz-connect-setup`) wrap these for Claude.

### Google Docs ownership

A service account has no Drive storage, so it can't *own* a new Doc. To create Docs,
either (A) enable domain-wide delegation and set `GOOGLE_IMPERSONATE_SUBJECT` (new Docs
owned by you), (B) point `google.drive_folder` at a Shared Drive, or (C) create the Doc
yourself + `gdoc link`. Updating an existing Doc the SA can edit always works. See the
**biz-connect-setup** skill.

## The feedback roundtrip (review → register → next turn)

`compose` builds a document; this loop closes feedback on it back into the pipeline so the
*next* draft is made with respect to reviewer comments — not patched ad hoc. It turns a
reviewed Google Doc into triaged, referenced **open points** held in a Notion database (the
stateful spine), and feeds those points back into generation.

```
  render ─▶ Google Doc ─▶ reviewers comment / suggest / edit
                                  │
                gdoc comments + gdoc diff          (capture)
                                  ▼
                compose run assimilate             (one high-reasoning pass)
                  • lift each comment → an open point WITH references
                  • triage by DISPOSITION: finesse | tonal | rethink | research | discussion
                  • route to LAYER: answer | spec | house-position | prompt
                  • cluster across items; emit register deltas
                                  ▼
                register upsert ─▶ Notion open-points DB ◀─ team works the gated rows
                  (dedupe by comment-id; field-ownership safe; journalled)
                                  │  register pull
                                  ▼
                local projection ─▶ {{OPEN_POINTS}} into the next spec/draft/critique
                compose run digest ─▶ deliberation brief ─▶ review Doc (the gated points)
                                  ▼
                agreed steps → source edits → staleness → rebuild → re-render → re-push
```

**Disposition decides re-entry.** `finesse`/`tonal` the pipeline clears automatically;
`rethink`/`research`/`discussion` are *gated* — they surface in the deliberation brief and
wait on a human or external input. Each point carries a stable `ISS-nnn` id threading the
in-text marker (`[DECISION: ISS-nnn …]`), the register row, the brief, and the source edit;
`lint` cross-checks markers against the register both ways.

**Where it lives.** The register's home is a **Notion database** (the team's live table); a
committed Markdown **projection** is what the pipeline reads. Bind it per-repo in
`connections.yaml` under `notion.register_db`; the consultation-specific prompts
(`assimilate.md`, `digest.md`) and register schema live in the consuming repo — the engine
stays generic and content-free. See the **feedback-ingest** and **register** skills.

## Layout

```
.claude-plugin/{plugin,marketplace}.json   plugin + self-hosted marketplace manifests
bin/bizconnect, bin/bizconnect.cmd         the `bizconnect` command (runs the launcher with a Python 3.9+)
bizconnect/
  config.py        central store + connections.yaml resolution
  cli.py           `bizconnect <service> <verb>` dispatch + doctor/init
  update.py        daily freshness check (`bizconnect update`)
  _google.py       shared service-account auth (+ optional impersonation)
  notion_md.py     Markdown <-> Notion blocks (pure functions, used by the Notion sync)
  xlsxdiff.py      workbook-diff engine: fact graph, headlines, causal links (openpyxl)
  xlsxverify.py    mechanical narrative verifier (anti-hallucination gate)
  connectors/
    gdocs.py  gsheets.py  xlsx.py  git.py
    notion.py      Notion API helpers + token verbs (whoami/check/read/upload/fill/sync)
    notiontree.py  two-way file <-> Notion sync via notion.yaml (link/map/outline/locate/diff/status/push/pull)
    compose.py  register.py  docreg.py  deliverable.py  secrets.py
scripts/bizconnect.py   self-bootstrapping launcher (creates the central-store venv)
scripts/install.*, scripts/release.sh
skills/                 plugin skills (one dir per affordance)
tests/                  pytest unit + integration tests (tests/fake_notion.py fakes the Notion API)
docs/                   maintainer notes and specs
examples/connections.example.yaml, examples/pipeline.example.yaml
requirements.txt, requirements-dev.txt
```

## Security

- Secrets live only in the central store; the repo's `.gitignore` also blocks
  `service-account*.json` / `secrets.env` / `.env` as a backstop.
- `connections.yaml` and `notion.yaml` hold ids/URLs only (not sensitive in a private repo).
- Access to any Google file is gated by sharing it with the service-account email;
  broad scopes don't widen the blast radius beyond what's shared. Access to Notion is gated
  the same way, by connecting pages to the integration.
