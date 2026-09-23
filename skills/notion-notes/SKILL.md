---
name: notion-notes
description: "Token-only Notion utilities — upload local files (images, PDFs, video, audio) onto a Notion page, swap [[img: …]] placeholders for uploaded files, read a page as text headlessly, check which integration the token is and whether it can reach a page, and keep a one-off ad-hoc notes page. Use when the user wants to attach a local file to a Notion page, read a Notion page without the MCP, or check Notion access. To keep repo Markdown files (notes, research, project state) in Notion and pull people's edits back, use the notion-sync skill instead."
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read
---

# Notion media, headless read and access checks

> To keep project **files** in Notion (whole pages, sections under a heading, folders of notes,
> databases) and pull people's edits back, use the **notion-sync** skill (`bizconnect notion
> push/pull` with a `notion.yaml` mapping). Don't hand-edit mapped content through the MCP.

What this skill covers, all with just the integration token (`NOTION_TOKEN`) and no OAuth:

- **Local file upload (image/PDF/video/audio):** `upload` / `fill` use the Notion File Upload
  API. The Notion MCP can only embed images by URL.
- **Headless read:** `read` prints a page as text; `sync` mirrors a page tree into a local
  folder (one way, read-only).
- **Access pre-flight:** `whoami` names the integration; `check` tests one page.

The Notion MCP is optional. Use it, if connected, for ad-hoc search or quick edits by hand to
pages that no `notion.yaml` maps.

## How to run

```bash
bizconnect notion whoami                          # which integration the token is
bizconnect notion check  <page|url|.>             # access pre-flight (OK, or how to fix it)
bizconnect notion read   <page|url|.> [--depth N]
bizconnect notion upload <page|url|.> chart.png --caption "..."
bizconnect notion fill   <page|url|.> --dir out/charts
bizconnect notion sync   <page|url|.> --out notion-mirror/   # one-way mirror of a page tree
```

`.` is shorthand for this repo's `notion.notes_page` (set it in `connections.yaml`).

If `bizconnect` isn't found (the plugin was installed in this session, or you're in a clone),
run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion whoami` (`python` or `py` on
Windows).

## Setup

The integration that owns `NOTION_TOKEN` must be connected to the page: in Notion, page ••• →
Connections → add it (sub-pages inherit). `bizconnect notion check <page>` says whether it is.
Creating the integration and the token is covered by the **biz-connect-setup** skill.

## An ad-hoc notes page

For one-off commentary about a piece of work, keep a dedicated Notion page and record it as
`notion.notes_page` in `connections.yaml`. Write the prose however suits (by hand in Notion, or
through the MCP if connected), and attach diagrams or exported PDFs with `bizconnect notion
upload .`. Anything that should live in the repo as well belongs in notion-sync instead.

## The placeholder image workflow

Put placeholder paragraphs like `[[img: chart_name | A caption ]]` on the page, put matching
files in a folder (a file named after each placeholder, e.g. `chart_name.png`), then run
`bizconnect notion fill <page> --dir <folder>` to swap them for uploaded image blocks.
