---
name: workbook-diff
description: Structural, human-readable diff of two Excel .xlsx workbooks. Use when the user wants to compare two spreadsheets/workbooks, see what changed between two versions of an Excel model, diff two .xlsx files, or review someone's edits to a financial model. Aligns rows so an inserted/deleted row reads as one move, not thousands of shifted-formula changes.
allowed-tools: Bash(python *), Read
---

# Workbook diff (.xlsx)

A naive cell-by-cell diff of two spreadsheets drowns in noise: insert one row and
Excel rewrites thousands of formula references (`=A227` -> `=A228`), so a real
two-line edit looks like 15,000 changes. This tool diffs **structurally** —
it aligns the rows of each sheet first, then reports only what a human cares about.

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'

# default: causal changes only (structure + hand-edited inputs)
$B xlsx diff OLD.xlsx NEW.xlsx -o diff.md

# everything: also formula-logic rewrites and the computed-value ripple
$B xlsx diff OLD.xlsx NEW.xlsx --formulas --values -o diff-full.md

# to stdout (omit -o); cap rows per section with --max N
$B xlsx diff OLD.xlsx NEW.xlsx --max 50
```

(If `python` opens the Microsoft Store on Windows, use `py`. Quote paths with spaces.)

## What the report contains

| Section | Meaning |
|---|---|
| **Rows moved** | A line relocated (e.g. pushed down by an insert above). |
| **Rows inserted / deleted** | Genuinely new or removed lines, with a value preview. |
| **Inputs changed** | Hand-typed numbers/text the author changed (`old => new`) — the high-signal "what did they actually do". |
| **Values moved** *(`--values`)* | Cells whose computed result moved — the *ripple* downstream of the edits. Off by default (large, and an effect not a cause). |
| **Formulas changed** *(`--formulas`)* | Formula-logic rewrites, shift-corrected so a pure row-move is **not** reported. |

## How to use the output

1. Run the default (no flags) first and read it — it's short and shows the causal edits.
2. After writing the report with `-o`, **Read** it back to summarise for the user.
3. Add `--values` only when the user wants the numeric impact (it can be thousands of lines on a large model); the per-sheet **Values moved** counts in the summary table tell you how big it will be before you opt in.

## Caveats

- Reads cached computed values, so the workbooks must have been **saved by Excel**;
  a workbook written purely by a library may lack them, degrading row alignment.
- Shift-correction of formulas is per-formula and uniform; a formula that straddles
  an insert boundary may still show as a (benign) `--formulas` change. Treat that
  section as advisory.
- Reads the files read-only; it never modifies the workbooks.
