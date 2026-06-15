---
name: gdoc-sync
description: Sync a local Markdown file to or from a Google Doc. Use when the user wants to push/update a Google Doc from a repo file, pull a Doc back into the repo as Markdown, link a repo file to an existing Doc, or check whether a doc is in sync. Keep the local Markdown as the source of truth and treat the Google Doc as a rendered, shareable copy.
allowed-tools: Bash(python *), Read, Edit, Write
---

# Google Docs ↔ Markdown sync

A local Markdown file is the source of truth. `push` converts it into a Google Doc
(Drive imports Markdown natively → real headings, bold, lists, tables, links);
`pull` exports the Doc back to Markdown. The binding (which file ↔ which Doc) lives
in the repo's `connections.yaml` under `google.docs`, filled in automatically on
first push.

## How to run

All verbs go through the launcher (it bootstraps its own venv — nothing to install):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc push  response/draft.md   # create or update the Doc
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc pull  response/draft.md   # overwrite local from the Doc
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc status                    # drift: local vs last push
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc link  response/draft.md <doc-url>   # bind an existing Doc
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc list                      # all bindings in this repo
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc push final/response.md --new --version v2.0  # NEW Doc instance for a major build
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc comments final/response.md --out build/feedback/feedback.bundle.md  # capture review comments
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" gdoc diff final/response.md     # direct edits (Doc vs local)
```

Run from inside the repo (the tool finds `connections.yaml` by walking up from the
cwd). Paths are repo-relative.

## Workflow guidance

- **Editing a submission/document:** edit the local `.md` with normal file tools,
  then `gdoc push` to update the Doc in place (same URL, same sharing). Don't edit
  the Doc and the Markdown in the same cycle — pick one direction.
- **If someone edited the Doc in Google:** `gdoc pull` to bring changes back into the
  `.md`, then continue locally. `pull` refuses to clobber un-pushed local edits unless
  you pass `--force`.
- **First push of a brand-new doc** needs somewhere to live (the service account has no
  Drive of its own). The repo's `connections.yaml` should set `google.share_with`
  and/or `google.drive_folder`. If `push` fails with a storage-quota error, see the
  **biz-connect-setup** skill (enable domain-wide delegation, use a Shared Drive, or
  link an existing Doc).

## Versioned instances & the doc registry

`gdoc push` **updates the bound Doc in place** by default (same URL, same comments). For a
**major build**, publish a *new* instance instead — `… gdoc push final/response.md --new
--version v2.0`. `--new` creates a fresh Google Doc (the previous instance, and its reviewer
comments, is left untouched) and rebinds it as current.

If the `docs_registry` Notion DB is bound (`notion.docs_registry`, created by `bizconnect
docreg init`), every push logs a row there: artifact, version, Doc URL, git commit, content
hash, date, status (current | superseded) — so the team has one Notion table of every produced
version, each pointing at its own Doc. `bizconnect docreg list` shows them; a plain `gdoc push`
refreshes the current row rather than creating an instance.

## Feedback capture (the review roundtrip)

`gdoc comments <file>` dumps the Doc's comments (anchored, with quoted text + threads) and
`gdoc diff <file>` shows direct edits (Doc vs local) — both feed the `assimilate` stage (see
the **feedback-ingest** skill). `gdoc resolve <file> <commentId>` closes a thread once actioned.

## Conversion notes

Headings, bold/italic, lists, tables, links, blockquotes and code convert cleanly
both ways. Embedded images by URL and very exotic formatting may not round-trip
perfectly — keep structure in Markdown and use the Doc for review/sharing.
