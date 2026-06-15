---
name: notion-notes
description: Read Notion pages, maintain working notes/commentary about this project in Notion, and upload local files (images, PDFs, video, audio) onto Notion pages. Use when the user wants to record or read notes in Notion, attach a local file to a Notion page, or check Notion access. Text read/write is best done via the Notion MCP; this skill adds the things the MCP cannot do.
allowed-tools: Bash(python *), Read
---

# Notion notes & media

Division of labour:

- **Text — search / read / create / edit pages:** done with the **Notion MCP** tools
  (`notion-search`, `notion-fetch`, `notion-create-pages`, `notion-update-page`), which
  the main agent calls directly (they are richer and live outside this skill's helpers).
  If the MCP isn't connected, see setup below.
- **Local file upload (image/PDF/video/audio):** the MCP's image syntax is URL-only, so
  this skill's `upload`/`fill` cover it via the Notion File Upload API.
- **Headless read / access pre-flight:** this skill's `read`/`check` work with just the
  integration token — no OAuth.

The `allowed-tools` below cover only this skill's own helpers (the python launcher +
Read); the Notion MCP text operations are performed by the main agent, not gated here.

## How to run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion whoami           # which integration the token is
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion check  .         # access pre-flight for the repo notes page
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion read   <page|url|.>
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion upload <page|url|.> chart.png --caption "..."
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" notion fill   <page|url|.> --dir out/charts
```

`.` is shorthand for this repo's `notion.notes_page` (set it in `connections.yaml`).

## Maintaining project notes

For commentary/notes about an ongoing piece of work, keep a dedicated Notion page and
record it as `notion.notes_page` in `connections.yaml`. Write prose via the MCP
(append/update the page); attach diagrams or exported PDFs with `notion upload`. The
integration that owns the token must be connected to the page (Page → ••• →
Connections → add it) — `notion check .` tells you if it isn't.

## The placeholder image workflow

Write the page via the MCP with placeholder paragraphs like
`[[img: chart_name | A caption ]]`, put matching files in a folder named after each
placeholder, then run `notion fill <page> --dir <folder>` to swap them for uploaded
image blocks.

## If the Notion MCP isn't connected

`claude mcp add --transport http notion https://mcp.notion.com/mcp`, then `/mcp` in the
REPL and authenticate. The token-based tools above work regardless.
