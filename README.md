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
3. **Connect each repo** (once per repo) — a committed `connections.yaml` holding only IDs/URLs (the *attachpoints*).

Rotate a credential once in the central store and every repo picks it up. The launcher
bootstraps its own dependency `.venv` on first run — no manual `pip install`, ever.

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

After the next session start, eight skills are available in **every** project:
`gdoc-sync`, `notion-notes`, `sheet-io`, `git-flow`, `doc-pipeline`, `feedback-ingest`,
`register`, `biz-connect-setup`.

> **Restart Claude Code (start a new session) before running steps 2–3.**
> `${CLAUDE_PLUGIN_ROOT}` is only set once the plugin is loaded; in the same session it is
> empty and the commands below won't resolve. (Working in a clone? Use `./scripts/bizconnect.py`
> and you can skip the restart.)

### 2. Set up your credentials (once per user)

These NEVER live in any repo — they go in the per-user central store at `~/.config/biz-connect/`.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init     # creates ~/.config/biz-connect/secrets.env
#   then: put NOTION_TOKEN=... in secrets.env
#         drop your Google service-account.json into ~/.config/biz-connect/
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor    # should print OK
```

On Windows, if `python` opens the Microsoft Store, use `py`.

**For Notion text read/write, also connect the Notion MCP** (separate from the token —
`doctor` does *not* check this):

```text
claude mcp add --transport http notion https://mcp.notion.com/mcp
```

then `/mcp` in the REPL and authenticate. The token-only verbs (`notion read` / `upload` /
`check`) work without it.

### 3. Connect a repo (once per repo)

From the repo root:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init      # writes connections.yaml + secret guards
# edit connections.yaml attachpoints: google.share_with, google.drive_folder, notion.notes_page, ...
```

`connections.yaml` is committed and holds only IDs/URLs (no secrets); if an ancestor
`connections.yaml` already exists, `init` leaves it as-is. Then ask Claude, or run
`bizconnect gdoc push <file.md>`, `bizconnect notion …`, etc. Agents: see `CLAUDE.md`.

### Setup checklist

- [ ] Plugin installed (`/plugin install biz-connect@biz-connect`) and a **new session started**
- [ ] `bizconnect init` run once — `~/.config/biz-connect/secrets.env` exists
- [ ] `NOTION_TOKEN=...` set in `secrets.env` (and each target Notion page shared with the integration)
- [ ] `service-account.json` in `~/.config/biz-connect/` (or `GOOGLE_SERVICE_ACCOUNT_FILE` set)
- [ ] `bizconnect doctor` prints **OK**
- [ ] Notion MCP connected (`claude mcp add --transport http notion https://mcp.notion.com/mcp` + `/mcp` authenticate) — only if you need Notion text read/write
- [ ] `bizconnect init` run in the repo root — `connections.yaml` created and attachpoints edited
- [ ] Files/Docs/Sheets you'll touch shared with the service-account email (printed by `doctor`)

### What isn't automatic

The plugin is shared; **your credentials and access grants are not**. Each user must:

- **Notion** — supply their own `NOTION_TOKEN` (a Notion internal-integration token) in
  `secrets.env`, and **share each Notion page with that integration** (Page → ••• →
  Connections → add it), or reads return 404.
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
| `doctor` reports a check failed | `secrets.env` or `service-account.json` missing/unreadable, or a dependency didn't import | Read doctor's per-line output; re-run `init`; confirm `service-account.json` is in the store; always run via the launcher so the venv bootstraps the deps |
| `python` opens the Microsoft Store (Windows) | App-execution-alias stub, no real `python` on PATH | Use `py`, e.g. `py "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor` |
| `${CLAUDE_PLUGIN_ROOT}` empty / `scripts/bizconnect.py` not found | Plugin installed but session not restarted | Start a new Claude Code session (or, in a clone, run `./scripts/bizconnect.py` directly) |
| `gdoc push` of a new file: "storage quota exceeded" | The service account has no Drive storage, so it can't *own* a new Doc | **(A)** domain-wide delegation + `GOOGLE_IMPERSONATE_SUBJECT`; **(B)** point `google.drive_folder` at a Shared Drive; **(C)** create the Doc yourself, share it with the SA, then `gdoc link` (see [Google Docs ownership](#google-docs-ownership)) |
| Google 403/404 on an existing Doc/Sheet | File not shared with the service account | Share it with the service-account email (printed by `doctor`) as Editor, then re-run |
| Notion read returns 404 | Page not shared with the integration | In Notion: open the page → ••• → Connections → add the integration that owns `NOTION_TOKEN`; confirm `NOTION_TOKEN` is set |
| Notion text tools missing | Notion MCP not connected | `claude mcp add --transport http notion https://mcp.notion.com/mcp`, then `/mcp` and authenticate |
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

Collaborators still set up their **own** central store (step 2) — secrets are never shared via the repo.

## The model: three layers

| Layer | Where | Contains | Committed? |
|------|-------|----------|------------|
| **Secrets** | `~/.config/biz-connect/` (the *central store*) | `secrets.env`, `service-account.json`, dependency `.venv` | **No** — per user, machine-level |
| **Toolkit** | this repo / installed plugin | connectors + CLI + skills | Yes (no secrets) |
| **Attachpoints** | `connections.yaml` in each consuming repo | which Doc/folder/page this repo binds to (ids/URLs only) | Yes (no secrets) |

Rotate a credential once in the central store and every repo picks it up. Share the
plugin with a colleague; they add their own central store and per-repo
`connections.yaml`. No secret ever lives in a project repo.

```
consuming repo (e.g. nous-reg)          central store (~/.config/biz-connect)
  connections.yaml  ── attachpoints ─┐     secrets.env        (NOTION_TOKEN, …)
  .bizconnect/state.json (sync state)│     service-account.json
                                     └───▶ .venv/             (auto-bootstrapped)
        ▲
        │  skills shell out to
   biz-connect plugin ── scripts/bizconnect.py ── bizconnect.cli ── connectors/*
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
launcher hashes `requirements.txt`). The check is throttled, fail-open, and never
blocks a command; disable it with `BIZCONNECT_UPDATE_CHECK=off` in `secrets.env`.

**Maintainers — cutting a release:** `scripts/release.sh <version>` (e.g. `0.2.0`) bumps
`plugin.json`, commits, tags `vX.Y.Z`, and pushes. The version bump is what triggers
everyone's update nudge, so bump it on every meaningful change.

## Connectors (today)

| Service | Verbs | Notes |
|---------|-------|-------|
| `gdoc` | `push pull status link unlink list comments diff resolve` | local Markdown ↔ Google Doc; `comments`/`diff`/`resolve` capture review feedback |
| `notion` | `whoami check read upload fill` | media upload + headless read; text via the Notion MCP |
| `sheet` | `whoami check read write append clear create` | service-account Sheets r/w |
| `git` | `status save sync pr` | branch-off-protected, co-author trailer, rebase-sync, PR |
| `compose` | `status run accept scaffold graph` | config-driven document-composition pipeline (`pipeline.yaml`); `inputs` syncs external sources; `assimilate`/`digest` close the feedback loop |
| `register` | `init pull upsert open status resolve journal` | Notion-DB open-points register for review feedback (the feedback roundtrip's spine) |

Plus `bizconnect doctor` / `init` / `update` / `version`. Skills (`/biz-connect:gdoc-sync`,
`notion-notes`, `sheet-io`, `git-flow`, `doc-pipeline`, `feedback-ingest`, `register`,
`biz-connect-setup`) wrap these for Claude.

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

## Roadmap (from a survey of existing notion-bot tooling)

Prioritised by reuse-cleanliness and the stated near-term needs:

- **PPTX / XLSX** — `mcp-servers/pptx-xlsx-mcp` is a clean, secret-free MCP server (47
  COM tools). Bundle it as a plugin MCP server (`office` connector). Windows + Office
  required.
- **DOCX** — *not yet covered anywhere*; add a Word COM server alongside the PPTX/XLSX
  one (`win32com` → `Word.Application`), or a cross-platform `python-docx` path.
- **PPTX/XLSX building primitives** — extract the reusable shape/timeline/chart and
  formula-generation helpers from `pptx-pipeline` + the root build scripts into
  `connectors/pptx` and `connectors/xlsx` (python-pptx / openpyxl; cross-platform).
- **Omni (BI) → Excel** — `omni-pipeline`'s `excel_write.py` (COM-safe live edit) and
  `omni_fetch.py` are reusable once secrets move to the central store.
- **Gmail → Notion** — `investor-comms` proves the domain-wide-delegation auth pattern;
  the filer core generalises (the matcher/CRM schema does not).
- **Notion ⇄ Excel round-trip** — `notion_excel_sync.py`, once made schema-agnostic.

## Migrating notion-bot onto biz-connect

notion-bot's tools each hardcode `ROOT = parents[2]` and load a repo-root `.env` — the
exact share-blockers this design removes. To migrate (incrementally, low-risk):

1. Move secrets from `notion-bot/.env` into `~/.config/biz-connect/secrets.env` (Notion
   token, Google SA path, Omni keys). notion-bot keeps running on its own `.env` until
   each tool is cut over.
2. Replace per-tool `ROOT`/`.env` loading with `bizconnect.config`.
3. Point notion-bot's `.claude/skills` at the installed plugin's launcher.
4. Leave Nous-specific narrative scripts (slide builders, investor CRM schema) in
   notion-bot — only the *connectors* generalise.

## Layout

```
.claude-plugin/{plugin,marketplace}.json   plugin + self-hosted marketplace manifests
bizconnect/
  config.py        central store + connections.yaml resolution
  cli.py           `bizconnect <service> <verb>` dispatch + doctor/init
  _google.py       shared service-account auth (+ optional impersonation)
  connectors/      gdocs.py  notion.py  gsheets.py  git.py  compose.py  register.py
scripts/bizconnect.py   self-bootstrapping launcher (creates the central-store venv)
skills/                 plugin skills (one dir per affordance)
examples/connections.example.yaml
requirements.txt
```

## Security

- Secrets live only in the central store; the repo's `.gitignore` also blocks
  `service-account*.json` / `secrets.env` / `.env` as a backstop.
- `connections.yaml` holds ids/URLs only (not sensitive in a private repo).
- Access to any Google file is gated by sharing it with the service-account email;
  broad scopes don't widen the blast radius beyond what's shared.
