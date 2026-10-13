---
name: gdoc-sync
description: Read, edit, or suggest changes to a Google Doc in place, and sync Docs with repo Markdown. Use for ANY request about a Doc's content (a docs.google.com/document link, or a Doc bound in connections.yaml) - "suggest/propose improvements", "review", "tighten", "edit", "update", "add", "fill in", "fix", "cut" - and to push a Markdown file to a Doc, pull a Doc back as Markdown, link a file to a Doc, or check sync. Requests to suggest, propose, review or improve are written as real Google Docs suggestions (Suggesting mode, `--suggest`) that the Doc's editors accept or reject. The claude.ai Google Drive connector cannot edit a Doc's content (it changes only a file's title and folder), so never use it, or a copy of the Doc, to make an edit.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Edit, Write
---

# Google Docs: read, edit or suggest in place; Markdown sync

Change a Doc **in place**, one passage at a time: `insert`, `replace` and `delete` touch only
the passage you name and leave the rest (formatting, comments, other people's text) alone.
Never make a copy of a Doc to get round an edit, and never rename one without asking. If an
in-place edit isn't possible, say so and stop.

`push` is different: it replaces the Doc's **whole body** with a fresh import of a local
Markdown file. Use it only for a Doc that a repo file alone produces (see "Markdown sync").

## First: suggest or edit?

Decide before the first write, from the user's words, and use the same mode for every write in
the task:

| The user says | Mode |
|---|---|
| "suggest", "propose", "recommend", "review", "improve", "tighten", "what would you change", "mark up", "with suggestions on" | **Suggest**: add `--suggest` to every write |
| "edit", "update", "change", "fix", "add", "fill in", "apply", "make it say" | **Edit**: write directly |
| unclear, or a Doc the user hasn't asked you to change | **Suggest** |

`--suggest` writes real Google Docs suggestions, as if someone had typed in Suggesting mode: the
Doc's editors see each one marked up, with Accept and Reject, and the text doesn't change until
someone accepts. Suggestions show under the name of the account biz-connect acts as (the
impersonated user, if delegation is set). Say in your reply which mode you used.

Only Google **Docs** can take suggestions. Slides, Sheets and Notion have no suggestion mode in
their APIs; see those skills before proposing changes there.

## Example: "suggest improvements to the top part of this doc <link>"

```bash
bizconnect gdoc read "<link>"     # the Doc as quotable text; find "the top part"
bizconnect gdoc replace "<link>" "<exact sentence, as read prints it>" "<better sentence>" --suggest
bizconnect gdoc insert  "<link>" --after "<anchor text>" --md "**A missing point.**" --suggest
bizconnect gdoc delete  "<link>" "<anchor text of a paragraph to cut>" --suggest
bizconnect gdoc read "<link>"     # check: your suggestions show as [+inserted+] / [-deleted-]
```

- **Scope:** change only what was asked. "The top part" means the title block and the opening
  section(s) before the body proper; leave the rest alone.
- **One improvement per call**, so each can be accepted or rejected on its own. Prefer
  sentence-level `replace` to rewriting whole paragraphs.
- **Report** each suggestion in one line (what and why), and the link.

## Reading a Doc

`gdoc read <file|doc-url>` prints the Doc one line per paragraph, in the Doc's **exact
characters**: quote these for anchors and `replace`. Headings show as `#`, bullets as `* `,
table rows as `| a | b |`, the title as `(title)`, and pending suggestions as `[+inserted+]`
and `[-deleted-]` (`--ids` adds each one's suggestion id). `--md` prints Drive's Markdown
export instead: it shows bold, italic and links, but escapes characters (`\$`, `2026\.`), so
never quote it into `replace`.

## Accepting or rejecting suggestions

```bash
bizconnect gdoc accept <file|doc-url> --all              # every pending suggestion, anyone's
bizconnect gdoc reject <file|doc-url> <id> [<id> ...]    # ids from `gdoc read <doc> --ids`
```

Only when the user asks: deciding on suggestions is their call, and `--all` takes everyone's,
not just yours. Each call is one guarded write, like the edits below.

## Passage edits (in place)

```bash
bizconnect gdoc insert  <file|doc-url> --after  "<anchor text>" --from snippet.md
bizconnect gdoc insert  <file|doc-url> --before "<anchor text>" --md "**One paragraph.**"
bizconnect gdoc replace <file|doc-url> "<exact text in the Doc>" "<new text>"
bizconnect gdoc delete  <file|doc-url> "<anchor text>"
bizconnect gdoc rows    <file|doc-url> --after "<text in a table row>" --row "Cell A | Cell B" [--row ...]
# each takes --suggest (write as suggestions) and --dry-run (print the requests, send nothing)
```

- **Read first** (`gdoc read`), so you edit against the Doc as it is now.
- **insert** places Markdown paragraphs before or after the one paragraph that contains the
  anchor (smart quotes, dashes, case and spacing don't matter; table cells aren't searched).
  It takes headings, paragraphs and top-level bullets with **bold**, *italic* and links. New
  paragraphs copy the style of the nearest existing paragraph of the same kind, and a bullet
  next to an existing list joins it. Put the anchor where the new text belongs: `--after` the
  last bullet of a list to extend it, `--before` a heading to add a section above it.
- **replace** swaps text that occurs **exactly once** in the Doc, table cells included. Use the
  Doc's own characters (it says so if quotes or dashes differ) and lengthen the text until it is
  unique. Keep the find text within one style (e.g. the words after a bold label): the new text
  takes a single style.
- **rows** adds rows to an existing table, below the one row that has a cell containing the
  anchor; give one `--row` per new row, cells separated by ` | ` (Markdown bold, italic and
  links work). To add to a table, use this; never start a second table or list instead.
- **delete** removes the one whole paragraph that contains the anchor. It can't remove the
  Doc's last paragraph or the line just before a table; `replace` their text instead.
- Each call is **one guarded write**: if anyone edits the Doc between the read and the write,
  nothing is written ("REVISION MISMATCH"): read again and re-run. A repeated edit is refused,
  and pending suggestions count as applied.
- Passage edits change the Doc only. To bring a bound local file level with it afterwards,
  `gdoc pull` (with `--force` once you have checked that `gdoc diff` shows only your edits).

## Markdown sync: how to run

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
