---
name: gdoc-sync
description: Edit, update or sync a Google Doc. Use for ANY request to edit, update, add to, fill in or fix a Google Doc (a docs.google.com link, or a Doc bound in connections.yaml), including one someone is editing live; and to push a repo Markdown file to a Doc, pull a Doc back as Markdown, link a file to a Doc, or check sync. The claude.ai Google Drive connector cannot edit a Doc's content (it changes only a file's title and folder), so never use it, or a copy of the Doc, to make an edit.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Edit, Write
---

# Google Docs: edit in place, and Markdown sync

Two ways to change a Doc:

- **Passage edits** (`insert`, `replace`) change one passage through the Docs API and leave
  everything else untouched: the Doc's formatting, comments and other people's text. Use these
  for **any Doc that people edit**, and for any edit to a Doc you were handed as a link.
- **`push`** replaces the Doc's **whole body** with a fresh import of a local Markdown file
  (Drive imports Markdown natively → real headings, bold, lists, tables, links). Use it only
  for a Doc that the repo file alone produces. On a Doc people edit it resets their formatting.

Never make a copy of a Doc to get round an edit, and never rename one without asking. If an
in-place edit isn't possible, say so and stop.

`pull` exports the Doc back to Markdown. The binding (which file ↔ which Doc) lives in the
repo's `connections.yaml` under `google.docs`, filled in automatically on first push.

## Passage edits (in place)

```bash
bizconnect gdoc insert <file|doc-url> --after  "<anchor text>" --from snippet.md
bizconnect gdoc insert <file|doc-url> --before "<anchor text>" --md "**One paragraph.**"
bizconnect gdoc replace <file|doc-url> "<exact text in the Doc>" "<new text>"
# add --dry-run to print the planned requests and send nothing
```

- **Read first:** `gdoc pull` (bound file) or `gdoc diff`, so you edit against the Doc as it is.
- **insert** places Markdown paragraphs before or after the one paragraph that contains the
  anchor (smart quotes, dashes, case and spacing don't matter; table cells aren't searched).
  It takes headings, paragraphs and top-level bullets with **bold**, *italic* and links. New
  paragraphs copy the style of the nearest existing paragraph of the same kind, and a bullet
  next to an existing list joins it. Put the anchor where the new text belongs: `--after` the
  last bullet of a list to extend it, `--before` a heading to add a section above it.
- **replace** swaps text that occurs **exactly once** in the Doc, table cells included, and
  keeps its formatting. Use the Doc's own characters (it says so if quotes or dashes differ).
  Lengthen the text until it is unique.
- Both are **one guarded write**: if anyone edits the Doc between the read and the write,
  nothing is written ("REVISION MISMATCH"): re-run. Both refuse to apply the same edit twice.
- Several edits are several calls; check the result with `gdoc diff` at the end.
- Passage edits change the Doc only. To bring a bound local file level with it afterwards,
  `gdoc pull` (with `--force` once you have checked that the diff shows only your edits).

## How to run

All verbs go through the launcher (it bootstraps its own venv — nothing to install):

```bash
bizconnect gdoc push  response/draft.md   # create or update the Doc
bizconnect gdoc pull  response/draft.md   # overwrite local from the Doc
bizconnect gdoc status                    # drift: local vs last push
bizconnect gdoc link  response/draft.md <doc-url>   # bind an existing Doc
bizconnect gdoc list                      # all bindings in this repo
bizconnect gdoc push final/response.md --new --version v2.0  # NEW Doc instance for a major build
bizconnect gdoc comments final/response.md --out build/feedback/feedback.bundle.md  # capture review comments
bizconnect gdoc diff final/response.md     # direct edits (Doc vs local)
bizconnect gdoc docx  response/draft.md [--out P] [--save]  # Markdown -> .docx (Drive convert; refreshes a Word-open target LIVE via COM, unsaved unless --save)
```

If `bizconnect` isn't found (the plugin was installed in this session, or you're in a
clone), run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> ...` (`python` or `py`
on Windows).

Run from inside the repo (the tool finds `connections.yaml` by walking up from the
cwd). Paths are repo-relative.

> **Umbrella repos:** if the repo hosts deliverables under `deliverables/<slug>/`, run from
> inside the deliverable folder. The Doc binding key is workspace-relative (e.g.
> `deliverables/<slug>/final/response.md`), and `--new` Docs + the `docs_registry` log are
> scoped to that deliverable's `deliverables.<slug>` block in `connections.yaml`.

## Workflow guidance

- **Editing a Doc only the repo writes:** edit the local `.md` with normal file tools,
  then `gdoc push` to update the Doc (same URL, same sharing). Don't edit
  the Doc and the Markdown in the same cycle — pick one direction.
- **If someone edited the Doc in Google:** `gdoc pull` to bring changes back into the
  `.md`, then continue locally. `pull` refuses to clobber un-pushed local edits unless
  you pass `--force`. From then on, change that Doc with passage edits, not `push`.
- **The drift guard compares content.** `pull` and `push` record what the Doc held; `push`
  refuses only if the Doc's content has changed since. Drive's modified time alone moves without
  edits, so pull, edit, push needs no `--force`. Never `--force` over someone's edits.
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
