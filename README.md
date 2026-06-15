# biz-connect

Business-service connectors for Claude Code, shareable across projects and users.
Connect any repo to **Google Drive/Docs**, **Notion**, **Google Sheets**, and a
standardised **git** flow — with per-repo bindings and a single per-user credential
store. Packaged as a Claude Code **plugin** (skills + CLI).

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

## Install (as a Claude Code plugin)

This repo is public and self-contained (no secrets). Install it from GitHub:

```text
/plugin marketplace add gjbm2/biz-connect
/plugin install biz-connect@biz-connect
```

(or, for local development, `/plugin marketplace add C:/path/to/biz-connect`.)

The skills then appear in **every** project. Set up your per-user credential store
**once**:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init      # creates ~/.config/biz-connect/secrets.env
# edit secrets.env: NOTION_TOKEN=...; drop your service-account.json in ~/.config/biz-connect/
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor     # should go green
```

The launcher bootstraps its own venv (Google client libs + ruamel.yaml) on first run —
no manual `pip install`, ever.

## Using it in a repo

One step per repo: scaffold and fill in the attachpoints.

```bash
cd my-project
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init   # writes connections.yaml here
# edit connections.yaml: google.share_with, notion.notes_page, etc.
```

For a team, commit `connections.yaml` and add this to the repo's `.claude/settings.json`
so collaborators get the plugin automatically:

```json
{
  "extraKnownMarketplaces": { "biz-connect": { "source": { "source": "github", "repo": "gjbm2/biz-connect" } } },
  "enabledPlugins": { "biz-connect@biz-connect": true }
}
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

## Connectors (today)

| Service | Verbs | Notes |
|---------|-------|-------|
| `gdoc` | `push pull status link unlink list` | local Markdown ↔ Google Doc (Drive native Markdown conversion) |
| `notion` | `whoami check read upload fill` | media upload + headless read; text via the Notion MCP |
| `sheet` | `whoami check read write append clear create` | service-account Sheets r/w |
| `git` | `status save sync pr` | branch-off-protected, co-author trailer, rebase-sync, PR |

Plus `bizconnect doctor` / `init` / `version`. Skills (`/biz-connect:gdoc-sync`,
`notion-notes`, `sheet-io`, `git-flow`, `biz-connect-setup`) wrap these for Claude.

### Google Docs ownership

A service account has no Drive storage, so it can't *own* a new Doc. To create Docs,
either (A) enable domain-wide delegation and set `GOOGLE_IMPERSONATE_SUBJECT` (new Docs
owned by you), (B) point `google.drive_folder` at a Shared Drive, or (C) create the Doc
yourself + `gdoc link`. Updating an existing Doc the SA can edit always works. See the
**biz-connect-setup** skill.

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
  connectors/      gdocs.py  notion.py  gsheets.py  git.py
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
