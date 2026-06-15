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

This makes five skills available in **every** project after the next session start:
`gdoc-sync`, `notion-notes`, `sheet-io`, `git-flow`, `biz-connect-setup`.

The non-interactive equivalent (e.g. from a script):

```bash
claude plugin marketplace add gjbm2/biz-connect
claude plugin install biz-connect@biz-connect
```

## 2. Credentials (once per user) — these NEVER live in any repo

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init     # creates ~/.config/biz-connect/secrets.env
#   then: put NOTION_TOKEN=... in that file, and drop your Google service-account.json
#         into ~/.config/biz-connect/  (or point GOOGLE_SERVICE_ACCOUNT_FILE at it)
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" doctor    # should print OK
```

`${CLAUDE_PLUGIN_ROOT}` is set when running as the installed plugin. If you are working
in a clone of this repo directly, use `./scripts/bizconnect.py` instead. On Windows, if
`python` opens the Microsoft Store, use `py`.

## 3. Connect a repo (once per repo)

From the repo root:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" init      # writes connections.yaml + secret guards
# edit connections.yaml: google.share_with, google.drive_folder, notion.notes_page, ...
```

`connections.yaml` is committed and holds only IDs/URLs (no secrets).

## 4. Use it

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'
$B gdoc   push|pull|status|link <file.md>   # local Markdown <-> Google Doc
$B notion check|read|upload|fill .          # . = this repo's notion.notes_page; text via the Notion MCP
$B sheet  read|write|append <sheet-url>     # service-account Sheets r/w
$B git    save|sync|pr                      # safe, standardised git flow
$B doctor                                   # diagnose setup
$B update                                   # check for a newer version
```

If a connector complains about credentials or Google Docs ownership, run
`bizconnect doctor` and read the **biz-connect-setup** skill. Full reference: `README.md`.

## Developing this plugin

- Connectors: `bizconnect/connectors/*.py`; shared engine: `config.py`, `_google.py`,
  `cli.py`; launcher (bootstraps the central-store venv): `scripts/bizconnect.py`.
- Bump `version` in `.claude-plugin/plugin.json` on every release — it drives the daily
  auto-update nudge (`bizconnect update`).
- Never commit secrets; the per-user central store (`~/.config/biz-connect`) is their
  only home. `.gitignore` guards key/secret files as a backstop.
