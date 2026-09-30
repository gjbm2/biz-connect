---
name: slides-edit
description: "Read and edit Google Slides decks: outline a deck (slide ids, layouts, element geometry in inches, text, fonts, colours, notes), render a slide to PNG or the deck to PDF, hide/unhide/move/duplicate/insert slides, set speaker notes, replace text, apply a raw batchUpdate, and build a styled slide from a simple inch-based spec in one atomic, revision-guarded write. Use when the user wants to change, add to, check or look at a Google Slides presentation (docs.google.com/presentation link)."
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Write, Edit
---

# Google Slides: outline, render, edit

Every write reads the deck first and sends **one** `batchUpdate` guarded by
`writeControl.requiredRevisionId`. If anyone changed the deck in between, Google rejects the
whole batch, nothing is written, and the command exits 3 with `REVISION MISMATCH`. Never retry
blindly: re-run `outline`, check your plan still fits the deck as it now is, then write again.

```bash
bizconnect slides check    <deck>                        # access pre-flight: title, revision, size
bizconnect slides outline  <deck> [--slides 26-28] [--full] [--json]
bizconnect slides layouts  <deck>                        # masters, layouts, placeholders
bizconnect slides thumb    <deck> 27 --out slide27.png   # LARGE PNG (1600 px) for visual QA
bizconnect slides pdf      <deck> --out deck.pdf
bizconnect slides build    <deck> slide.yaml [--dry-run] [--out requests.json]
bizconnect slides apply    <deck> requests.json          # raw batchUpdate: [..] or {"requests": [..]}
bizconnect slides hide     <deck> 5,7                    # unhide likewise
bizconnect slides move     <deck> 12 --to 3
bizconnect slides duplicate <deck> 12 [--at 13] [--id new_slide_id]
bizconnect slides insert   <deck> --at 28 [--layout Blank] [--master <id>] [--id <id>]
bizconnect slides notes    <deck> 28 --text "..."        # or --file notes.txt, or --clear
bizconnect slides replace  <deck> --find "2025" --with "2026" [--slides 3-9] [--match-case]
bizconnect slides create   --title "Scratch deck"        # new empty deck (10 x 5.625 in)
```

If `bizconnect` isn't found (the plugin was installed in this session, or you're in a
clone), run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> ...` (`python` or `py`
on Windows).

`<deck>` is the presentation URL or id. **Slides are named by number as the Slides UI shows
them (1-based)** or by objectId; `outline` prints both (`slide 27 [index 26] id=...`). `--at N`
and `--to N` mean "becomes slide N". Every write verb takes `--dry-run` (print the requests,
send nothing) and `--revision R` (refuse unless the deck is still at revision R, the one your
`outline` printed; use it when you planned from an earlier read).

## Access

Same credentials as the other Google connectors. With `GOOGLE_IMPERSONATE_SUBJECT` set, calls
act as that user (domain-wide delegation for the `drive` scope; no extra scope is needed), so
any deck that user can edit works. Without it, share the deck with the service-account email
(`bizconnect sheet whoami`). `NO ACCESS [403/404]` means it isn't shared; `AUTH ERROR ...
unauthorized_client` means the delegation lacks the `drive` scope (the message names the client
id to authorise).

## Working on a live deck other people are editing

1. `outline` the slides around where you'll work, with `--full` to see every style. Note the
   fonts, sizes, colours, fills, margins and table geometry: that is the deck's visual language.
   Styles read `font size [bold] [italic] colour`, most-used first. Tables show each row (cells,
   fills, alignment) and a `body cN` line per column over the body rows; `vMIDDLE` etc. is the
   cells' vertical alignment.
2. Write a spec (below). `build --dry-run` first; read the warnings (overflow, off-slide).
3. `build` it, then `thumb` the slide and **look at the PNG** before saying it's done. Fix the
   spec and re-run `build`: a build replaces what the same spec built before, in place.
4. Touch only your own slide and elements. Don't restyle or move other people's content unless
   asked. New slides can be hidden with `hide` until they're ready.

## Slide specs (`build`)

YAML or JSON. Positions and sizes are **inches** from the slide's top-left (`outline` prints the
page size; widescreen decks are 13.33 x 7.5). Colours are `#RRGGBB` or `theme:ACCENT1` etc.

```yaml
new: {at: 28, layout: Blank, id: bc_policy_options}   # or `slide: 28` (number or objectId) to add to a slide
defaults: {font: Arial, size: 11, color: "#1C1917"}
notes: Speaker notes for this slide (set on rebuilds of an existing slide)
elements:
  - id: title
    x: 0.69
    y: 0.94
    w: 11.95
    h: 0.5
    text: Options for the next phase
    style: {size: 28, bold: true}
  - id: intro
    x: 0.69
    y: 1.45
    w: 11.95
    h: 0.6
    text: "Three routes we tested against ***first principles***, with where each fits best."
    style: {size: 14, color: "#6B6663"}
  - id: grid
    type: table
    x: 0.69
    y: 2.25
    col_widths: [3.6, 4.2, 4.15]
    row_heights: [0.45, 0.9, 0.9]
    borders: {color: "#9E9E9E", weight: 1.5}
    header: {fill: "#FDE6BC", align: center, style: {size: 13, color: "#000000"}}
    rows:
      - ["Route", "When it fits", "Risk if applied too early"]
      - ["Outcome standards", "Results can be **measured** and linked to the firm", "Harm shows up late"]
      - [{text: "Fixed rules", fill: "#EAE4DC"}, "The activity is stable and well understood", "Locks in today's models"]
  - id: callout
    type: round_rect
    x: 0.69
    y: 4.9
    w: 5.6
    h: 1.3
    fill: "#FCB747"
    outline: none
    valign: middle
    text:
      - {text: "What we propose", bold: true, size: 13}
      - {text: "Start with outcome standards", bullet: true}
      - {text: "Review after twelve months", bullet: true, level: 1}
  - id: rule
    type: line
    x: 0.69
    y: 6.5
    w: 11.95
    h: 0
    color: "#9E9E9E"
    weight: 1
```

- **Target**: `slide: N|id` adds to an existing slide; `new: {at, layout, master, id}` creates one
  from the deck's own layouts (`layouts` lists them; the default `Blank` comes from the master of
  the neighbouring slide). Give `new.id` so a re-run rebuilds that slide instead of adding another.
- **Element types**: `text` (default, a text box), `rect`, `round_rect`, `ellipse`, `table`,
  `line` (from (x, y) to (x+w, y+h); w, h >= 0), `image` (`url`, publicly fetchable). Every
  element needs a unique `id` (letters, digits, `_`, `-`).
- **Text**: a string (a newline starts a new paragraph) or a list of paragraphs. A paragraph is a
  string or `{text|runs, align, bullet, level, space_before, space_after, line_spacing, <style>}`;
  a run is a string or `{text, <style>}`. Style keys: `font size bold italic underline strike
  color link`, given directly or under `style:`, and inherited defaults -> element -> header ->
  cell -> paragraph -> run. Inline `**bold**`, `*italic*`, `***both***`; `\*` is a literal
  asterisk; `markup: false` turns this off.
- **Alignment**: `align: start|center|end|justified`, `valign: top|middle|bottom`. Text boxes
  start-align; Slides centres text in shapes unless you set `align`.
- **Bullets**: `bullet: true` (discs), or a preset such as `NUMBERED_DIGIT_ALPHA_ROMAN`;
  `level: 1` indents.
- **Shapes**: `fill: "#hex" | {color, alpha} | none`, `outline: "#hex" | {color, weight (pt), dash}
  | none`. Shapes default to no fill and no outline.
- **Tables**: `rows` (a cell is text or `{text, fill, align, valign, <style>}`), `col_widths` or `w`,
  `row_heights` (minimums; rows grow to fit text), `header: {fill, align, <style>}` for the first
  `header_rows` (default 1), `fill` for all cells, `borders: "#hex" | none | {color, weight,
  position}` (position `ALL`, `OUTER`, `INNER`, `TOP`, ...; a list for several),
  `merge: [[row, col, rowspan, colspan]]` (0-based).
- **Ownership**: built elements get objectIds `bc_<prefix>_<id>` (the prefix derives from the
  slide, or set `prefix:`). A rebuild deletes the slide's elements with that prefix, then creates
  the spec's elements, in the same atomic batch. Nothing else on the slide is touched.
- **Overflow**: Slides never shrinks text to fit. `build` estimates each box's wrapped text height
  and each table's grown height and warns when text runs past the box or off the slide. It is an
  estimate: confirm with `thumb`.

## Raw requests (`apply`)

For anything the spec doesn't cover (images by URL with cropping, charts, group moves,
`updatePageElementTransform`, `updateParagraphStyle` on existing text...), write the Slides API
requests yourself and `apply` them. Units are EMU (914400 per inch, 12700 per point). Use the
objectIds from `outline`; new objectIds must be 5-50 characters, unique in the deck. `build --out
requests.json` gives a starting point you can edit.

## Limits

- The API can't change a deck's page size, text-box padding, autofit or corner radius.
- Thumbnail URLs are fetched straight away; a LARGE thumbnail is 1600 px wide.
- `create` makes an empty 10 x 5.625 in deck owned by the impersonated user.
