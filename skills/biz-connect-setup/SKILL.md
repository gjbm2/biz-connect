---
name: biz-connect-setup
description: Set up or troubleshoot biz-connect for a repo — create connections.yaml, configure the per-user central credential store, run the doctor check, and fix Google Docs ownership/quota errors. Use for first-time setup ("connect this repo to Google/Notion"), onboarding a new user, or when a connector errors about missing credentials or Drive storage quota.
allowed-tools: Bash(python *), Read, Edit, Write
---

# biz-connect setup & troubleshooting

Two locations, cleanly separated:

- **Central store** (per-user, machine-level, never committed): `~/.config/biz-connect/`
  holds `secrets.env` (NOTION_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE, …) and
  `service-account.json`, plus the dependency `.venv` (auto-created).
- **Per-repo `connections.yaml`** (committed, no secrets): this repo's attachpoints —
  which Google Doc/Drive folder/Notion page it binds to.

## Commands

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor   # check store, creds, deps, connections.yaml
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init     # scaffold connections.yaml here + ensure the store exists
```

Start with `doctor`. It tells you exactly what's missing.

> **Windows note:** if `python` opens the Microsoft Store (the App Execution Alias stub)
> or isn't found, use `py` instead — e.g. `py "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor`.
> The launcher re-execs through its own venv either way.

## First-time central store

1. `init` creates `~/.config/biz-connect/secrets.env` if absent.
2. Put `NOTION_TOKEN=...` in it (a Notion internal-integration token).
3. Drop the Google `service-account.json` into `~/.config/biz-connect/`
   (or point `GOOGLE_SERVICE_ACCOUNT_FILE` at it).
4. `doctor` should go green.

## Onboarding to a repo whose creds live in Secret Manager (no manual CLI)

If the repo's `connections.yaml` has a `secrets:` block, the team's scoped credentials
live in Google Secret Manager and **you (the agent) fetch them FOR the user** — they
should not have to run CLIs. From the repo root:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" secrets pull
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor
```

`secrets pull` signs the user in on first run (a browser window opens — the only human
step), then writes the scoped `NOTION_TOKEN` + `service-account.json` into the central
store. Prereqs: the Google Cloud SDK (`gcloud`) installed, and the user added to the
repo's access group. A 403 from `secrets pull` means they aren't in that group yet (see
the repo's `tooling/README.md`). `secrets status --check` verifies access without pulling.

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
