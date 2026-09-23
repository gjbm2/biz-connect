# biz-connect — maintainer notes

Internal notes for people developing biz-connect itself. Users need only the
[README](../README.md) and the skills.

## Developing

- Connectors live in `bizconnect/connectors/*.py`; the shared engine is `config.py`,
  `_google.py` and `cli.py`; `notion_md.py` is the pure Markdown ⇄ Notion-blocks layer used by
  the sync (`connectors/notiontree.py`). The launcher `scripts/bizconnect.py` bootstraps the
  central-store venv, and `bin/bizconnect` / `bin/bizconnect.cmd` expose it as the `bizconnect`
  command (Claude Code puts a plugin's `bin/` on PATH).
- Tests: `python -m pytest -q` from a dev venv with `requirements-dev.txt` installed. The Notion
  sync tests run against an in-memory fake of the API (`tests/fake_notion.py`) and never call
  the live API. `tests/test_docs_examples.py` checks that the example `notion.yaml` files in the
  notion-sync skill and the `notiontree` docstring load, so documentation examples can't rot.
- Skills and docs must stay generic: no people, companies or repos from any one user's
  projects in examples. Commands are written as `bizconnect <service> <verb> ...`, with the
  launcher fallback (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" ...`) noted once per
  skill. Skills that run commands list `Bash(bizconnect *)`, `Bash(python *)`,
  `Bash(python3 *)` and `Bash(py *)` in `allowed-tools`.
- The original author's workstation loads the plugin (skills and scripts) straight from a
  clone, so an edit there is live locally at once. `scripts/release.sh` is what ships changes
  to other machines and users.

## Cutting a release

1. Commit the change first: `scripts/release.sh` commits **only** the version bump, and it
   refuses to run on a dirty tree or with untracked files.
2. `bin/bizconnect` must be committed executable (mode 100755), or `bizconnect` fails with
   "Permission denied" on macOS/Linux. Windows clones often have `core.filemode=false`, which
   stages new files as 100644, so run `git update-index --chmod=+x bin/bizconnect` after adding
   it. release.sh checks this, and so does a test.
3. `scripts/release.sh <version>` (e.g. `0.13.0`) bumps `.claude-plugin/plugin.json`, commits,
   tags `vX.Y.Z`, and pushes. Make the bump the last step: a machine that loads the plugin from
   a clone picks up whatever the tree holds.

The version bump is what triggers everyone's daily update nudge (`bizconnect update`), so bump
it on every meaningful change. When a release adds a skill, say in the notes that users must
start a new session after `/plugin update biz-connect`.

## Notion sync: how the guard decides

Each map entry's `synced` field (written by the tool, committed with `notion.yaml`) is a
fingerprint of the canonical content both sides held at the item's last sync. It is one value
per entry, or a per-file map for `pages:` / `database:` folders. `l:r` means Notion normalised
the content differently from the file. Comparing both sides with it gives local-ahead,
remote-ahead or conflict on any machine. `.bizconnect/state.json` is the per-machine fallback,
for items synced before `synced` existed. With neither, differing sides are `no-baseline`.
Nothing is inferred from git history.

## Roadmap

- **Office documents**: bundle a PPTX/XLSX COM server as a plugin MCP server (Windows +
  Office); extract reusable python-pptx / openpyxl building helpers into `connectors/pptx` and
  `connectors/xlsx` (`xlsx diff` has shipped).
- **DOCX**: Markdown sources are covered (`bizconnect gdoc docx <file.md> [--out P]`, via
  Drive). Editing native DOCX is still open.
- **BI → Excel**: a COM-safe live-edit writer plus a BI fetcher, with secrets in the central
  store.
- **Gmail → Notion**: a filer on domain-wide delegation; the generic core, not any one CRM
  schema.
- **Notion ⇄ Excel round-trip**, schema-agnostic.
