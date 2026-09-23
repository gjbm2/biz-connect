# biz-connect — install / enable / use (for agents and humans)

**This repository *is* a Claude Code plugin.** It connects any repo to Google
Drive/Docs, Notion, Google Sheets, and a standardised git flow. If you are an agent
that just opened this repo and the user wants its tooling, here is exactly what to do.

## 1. Enable the plugin (once per machine)

In the Claude Code REPL:

```text
/plugin marketplace add gjbm2/biz-connect
/plugin install biz-connect@biz-connect
```

(For local development on a clone, use the path instead: `/plugin marketplace add .`)

This makes the plugin's skills available in **every** project after the next session start:
`gdoc-sync`, `notion-sync`, `notion-notes`, `sheet-io`, `workbook-diff`, `git-flow`, `doc-pipeline`,
`feedback-ingest`, `register`, `new-submission`, `biz-connect-setup`. Skills (and the
`bizconnect` command) load only at session start, so start a new session after installing or
updating.

The non-interactive equivalent (e.g. from a script):

```bash
claude plugin marketplace add gjbm2/biz-connect
claude plugin install biz-connect@biz-connect
```

## Running commands

Commands are `bizconnect <service> <verb> ...`: the plugin's `bin/` is on PATH inside Claude
Code. If `bizconnect` isn't found (plugin installed in this session, or outside Claude Code),
run the launcher, `scripts/bizconnect.py` in the plugin's folder, with the same arguments. The
folder is the `installPath` for `biz-connect` in `~/.claude/plugins/installed_plugins.json`
(usually `~/.claude/plugins/cache/biz-connect/biz-connect/<version>/`). For example,
`python3 <that folder>/scripts/bizconnect.py doctor` (`py` on Windows). `${CLAUDE_PLUGIN_ROOT}`
works only inside skill text. In a clone of this repo, use `bin/bizconnect` or
`scripts/bizconnect.py`.

## 2. Credentials (once per user) — these NEVER live in any repo

```bash
bizconnect init      # creates ~/.config/biz-connect/secrets.env
#   Notion: put NOTION_TOKEN=... in that file (an internal integration from
#           https://www.notion.so/profile/integrations with Read, Update and Insert content;
#           share the hub page with it via ••• → Connections)
#   Google (only for gdoc/sheet): drop your service-account.json into ~/.config/biz-connect/
#           (or point GOOGLE_SERVICE_ACCOUNT_FILE at it)
bizconnect doctor    # reports each part; a missing service account only matters for Google
bizconnect notion whoami   # verifies the Notion token
```

The **biz-connect-setup** skill walks through this and its errors.

## 3. Connect a repo (once per repo)

From the repo root:

```bash
bizconnect init      # writes connections.yaml + .gitignore guards
# edit connections.yaml: google.share_with, google.drive_folder, notion.notes_page, ...
```

`connections.yaml` is committed and holds only IDs/URLs (no secrets).

## 4. Use it

```bash
bizconnect gdoc    push|pull|status|link <file.md>          # local Markdown <-> Google Doc
bizconnect notion  link|map|outline|locate|diff <args>      # set up / inspect a folder's Notion mapping (notion.yaml)
bizconnect notion  status|pull|push [<folder or file>]      # two-way file <-> Notion sync
bizconnect notion  whoami|check|read|upload|fill <page|.>   # . = this repo's notion.notes_page
bizconnect sheet   read|write|append <sheet-url>            # service-account Sheets r/w
bizconnect xlsx    diff OLD.xlsx NEW.xlsx                   # structural workbook diff
bizconnect git     save|sync|pr                             # safe, standardised git flow
bizconnect compose status|run|accept|graph                  # doc-composition pipeline (needs pipeline.yaml); `run inputs` syncs sources
bizconnect doctor                                           # diagnose setup
bizconnect update                                           # check for a newer version
```

**Notion sync** (needs biz-connect 0.13 or later; full guide: the **notion-sync** skill): a
folder's committed `notion.yaml` maps its Markdown files to Notion pages, sections, folders of
pages or databases. `pull` first in each session, edit the files (not Notion), `push`, then
commit the files together with `notion.yaml`: it carries the ids and the last-sync
fingerprints every machine relies on. Never `--force` over other people's Notion edits; on
`conflict` or `no-baseline`, `bizconnect notion diff <path>` first.

If a connector complains about credentials or Google Docs ownership, run
`bizconnect doctor` and read the **biz-connect-setup** skill. Full reference: `README.md`.

## Developing this plugin

- Connectors: `bizconnect/connectors/*.py`; shared engine: `config.py`, `_google.py`,
  `cli.py`, `notion_md.py`; launcher (bootstraps the central-store venv):
  `scripts/bizconnect.py`, exposed as `bin/bizconnect` / `bin/bizconnect.cmd`.
- Tests: `python -m pytest -q` (the Notion sync runs against `tests/fake_notion.py`, never the
  live API).
- Bump `version` in `.claude-plugin/plugin.json` on every release — it drives the daily
  auto-update nudge (`bizconnect update`). `scripts/release.sh` is what ships a change to
  other machines and users; commit first (it commits only the bump). Steps:
  `docs/maintainers.md`.
- Keep skills and docs generic (no one user's people, companies or repos in examples); see
  `docs/maintainers.md`.
- Never commit secrets; the per-user central store (`~/.config/biz-connect`) is their
  only home. `.gitignore` guards key/secret files as a backstop.
