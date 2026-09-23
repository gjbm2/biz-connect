---
name: biz-connect-setup
description: Set up or troubleshoot biz-connect for a user or repo — the per-user central credential store (Notion integration token, Google service account), Notion access (creating the integration, sharing pages, 404s), connections.yaml, the doctor check, and Google Docs ownership/quota errors. Use for first-time setup ("connect this repo to Google/Notion"), onboarding a new user or teammate, or when a connector errors about missing credentials, a Notion page it cannot read, or Drive storage quota.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Edit, Write
---

# biz-connect setup & troubleshooting

Two locations, cleanly separated:

- **Central store** (per-user, machine-level, never committed): `~/.config/biz-connect/`
  holds `secrets.env` (NOTION_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE, …) and
  `service-account.json`, plus the dependency `.venv` (auto-created).
- **Per-repo `connections.yaml`** (committed, no secrets): this repo's attachpoints —
  which Google Doc/Drive folder/Notion page it binds to. An **umbrella repo** that hosts
  several deliverables under `deliverables/<slug>/` keeps shared attachpoints (Shared Drive
  root, `secrets`, `git`, the umbrella Notion page) at the top level and per-deliverable ones
  (its register / docs-registry DBs, `inputs`, Drive subfolder) under a `deliverables.<slug>:`
  block. Credential setup below is **per-user and umbrella-wide — do it once, not per
  deliverable**; build/publish commands are run from *inside* the deliverable folder so the
  engine scopes to that `deliverables.<slug>`.
- Notion **file sync** is configured per folder, in a committed `<folder>/notion.yaml` (see the
  **notion-sync** skill), not in `connections.yaml`.

## Commands

```bash
bizconnect doctor   # check store, creds, deps, connections.yaml
bizconnect init     # ensure the store exists + scaffold connections.yaml here (and .gitignore guards)
```

Start with `doctor`. It tells you what's missing.

`bizconnect` is on PATH inside Claude Code once the plugin is loaded. If it isn't found (the
plugin was installed in this session, or you're in a clone), run the launcher with the same
arguments: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor`. On Windows use
`python`, or `py` if `python` opens the Microsoft Store. The launcher re-execs through its own
venv either way, and needs Python 3.9 or later. (That path is filled in only here, in the
skill. To give a user a command for their own terminal, use the `installPath` for `biz-connect`
in `~/.claude/plugins/installed_plugins.json`.)

## First-time central store

1. `bizconnect init` creates `~/.config/biz-connect/secrets.env` if absent.
2. **Notion** (if the user wants Notion): set up the integration and `NOTION_TOKEN` as below.
3. **Google** (only for the Google connectors: `gdoc`, `sheet`, Doc inputs): drop the
   `service-account.json` into `~/.config/biz-connect/` (or point `GOOGLE_SERVICE_ACCOUNT_FILE`
   at it).
4. `bizconnect doctor`. For a Notion-only user a missing `service-account.json` is expected:
   it only matters for the Google connectors. Verify Notion with `bizconnect notion whoami`.

## Notion setup

1. **Create an internal integration** at https://www.notion.so/profile/integrations (New
   integration → Internal → pick the workspace). Give it the capabilities **Read content**,
   **Update content** and **Insert content**. The sync and uploads create, edit and archive
   blocks and pages, so a read-only integration fails at push time.
2. **Put its secret in the store:** copy the Internal Integration Secret into
   `NOTION_TOKEN=...` in `~/.config/biz-connect/secrets.env` (one `KEY=value` line).
3. **Share the hub page with it:** open the page in Notion → ••• menu → Connections → add the
   integration. Sub-pages inherit the access; a page elsewhere in the workspace needs its own
   connection. Each person's integration must be connected to the pages they sync.
4. **Verify:** `bizconnect notion whoami` prints the integration's name, and
   `bizconnect notion check <hub-url>` prints `OK — integration can access page …`.
5. **To keep repo files in Notion**, continue with the **notion-sync** skill:
   `bizconnect notion link <folder> <hub-url>`, then `map`, `pull`, `push`. It needs
   biz-connect 0.13 or later (`bizconnect update` shows the version; start a new session after
   updating so new skills load). On a new machine or a teammate's clone, `pull` first.

The Notion MCP is **not** needed for any of this. It is only useful for ad-hoc search or
editing by hand; if the user wants it: `claude mcp add --transport http notion
https://mcp.notion.com/mcp`, then `/mcp` in the REPL to authenticate.

## Onboarding to a repo whose creds live in Secret Manager (no manual CLI)

If the repo's `connections.yaml` has a `secrets:` block, the team's scoped credentials
live in Google Secret Manager and **you (the agent) fetch them FOR the user** — they
should not have to run CLIs. From the repo root:

```bash
bizconnect secrets pull
bizconnect doctor
```

`secrets pull` signs the user in on first run (a browser window opens — the only human
step), then writes the scoped `NOTION_TOKEN` + `service-account.json` into the central
store. Prereqs: the Google Cloud SDK (`gcloud`) installed, and the user added to the
repo's access group. A 403 from `secrets pull` means they aren't in that group yet (see
the repo's own setup docs). `secrets status --check` verifies access without pulling.

## Google Docs ownership (the common gotcha)

A service account has **no Drive storage of its own**, so it cannot *own* a newly
created Doc — `gdoc push` of a new file will 403 with a storage-quota error. `update`
of an existing Doc the SA can edit is fine. To create new Docs, pick one:

- **A — Domain-wide delegation (best if you admin the Workspace).** In Google Admin →
  Security → API controls → Domain-wide delegation, authorise the SA's client id for
  scopes `https://www.googleapis.com/auth/drive` and `.../auth/documents`. Then set
  `GOOGLE_IMPERSONATE_SUBJECT=you@domain` in `secrets.env` (or `google.impersonate` in
  `connections.yaml`). New Docs are then owned by you, in your Drive.
- **B — Shared Drive.** Set `google.drive_folder` to a folder in a Shared Drive the SA
  can write to; files there are owned by the Shared Drive, not the SA.
- **C — Link existing.** Create the Doc yourself, share it with the SA email as Editor,
  then `gdoc link <file> <doc-url>` and push (updates need no SA storage).

`bizconnect doctor` prints the SA email and current `connections.yaml` settings.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `bizconnect: command not found` | Plugin not loaded in this session (just installed/updated), or running outside Claude Code | Start a new session, or use the launcher fallback above |
| `bizconnect: Permission denied` (macOS/Linux) | The shim lost its executable bit | Use the launcher fallback above, and update the plugin |
| PowerShell: a URL containing `&` is cut short ("'p' is not recognized") | `bizconnect.cmd` goes through cmd, which splits at `&` | Pass the page id alone, quote the URL as `'"<url>"'`, or run the command from Git Bash |
| `bizconnect: no Python 3.9+ found` / `python: command not found` | No suitable Python on PATH (macOS/Linux ship `python3`, not `python`) | Install Python 3.9+; or `export BIZCONNECT_PYTHON=/path/to/python3` |
| `missing required secret 'NOTION_TOKEN'` | Token not in `secrets.env` | Add `NOTION_TOKEN=...` (Notion setup step 2) |
| Notion 404, `cannot read page`, `NO ACCESS [404]`, "is it shared with the integration?" | Page not shared with the integration that owns `NOTION_TOKEN` | Page ••• → Connections → add it; check with `bizconnect notion check <url>` and `bizconnect notion whoami` (a different integration than expected means a different token) |
| Notion 401 `unauthorized` | Token wrong, revoked or regenerated | Copy the current secret from the integration's page into `NOTION_TOKEN` |
| Notion 403 on push/upload | The integration lacks Insert or Update content | Enable both capabilities in the integration's settings |
| Notion sync: `no connections.yaml or git repository found` | Outside a git repo, with no `connections.yaml` above the working directory | `bizconnect init` at the repo root |
| Notion sync: `no-baseline`, `conflict`, `missing` | Sync verdicts, not setup problems | See the **notion-sync** skill's Troubleshooting |
| `doctor` flags only the `service account` | No Google service account; expected for a Notion-only user | Ignore, or add `service-account.json` if you need the Google connectors |
| Google 403/404 on an existing Doc/Sheet | File not shared with the service account | Share it with the SA email (printed by `doctor`) as Editor |
| `gdoc push` of a new file: storage quota exceeded | The service account can't own a new Doc | See "Google Docs ownership" |
