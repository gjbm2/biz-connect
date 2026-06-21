---
name: workbook-diff
description: Structural, grounded diff of two Excel .xlsx workbooks, turned into a human-readable account of WHAT changed. Use when the user wants to compare two spreadsheets/workbooks, see what changed between two versions of an Excel model, diff two .xlsx files, or review someone's edits to a financial model. Runs a deterministic code-based diff (the ground truth) then writes a verified narrative that explains the actual edits — structural changes, assumption/value changes, formula-logic changes — with downstream effect as a brief footnote.
allowed-tools: Bash(python *), Read, Write
---

# Workbook diff -> grounded "what changed" narrative

**The point is to let a reader understand WHAT changed** — every edit, clearly — not to
sell the effect. Two stages. **Stage 1 is code** — a deterministic diff that aligns rows
AND columns (so an inserted row/column is one fact, not thousands of shifted formulas),
captures number formats, and separates the substantive edits (assumptions/values) from the
relabel cascade a restructure produces. It emits a JSON *fact graph* — the single source of
truth. **Stage 2 is you** — you turn those facts into a clear account of the changes, then a
**mechanical verifier proves every figure is grounded**.

The cardinal rule: **the narrative may only state numbers that appear in the JSON, and
must cite the fact each one comes from. You never do arithmetic — all deltas are
precomputed.** A separate verifier enforces this; a narrative that invents or miscites a
figure is rejected.

## Stage 1 — run the deterministic diff

```bash
B='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'
$B xlsx diff OLD.xlsx NEW.xlsx --json diff.json --summary diff.summary.json -o diff.md
```

- `diff.summary.json` (small) is what **you read** to author — header, diagnostics, totals,
  routing, named ranges, **headline_metrics**, **causal_links**, and the cause/structural
  facts. The value-ripple is summarised, not listed.
- `diff.json` (full) is the verifier's ground truth. `diff.md` is a human preview.
- (If `python` opens the Microsoft Store on Windows, use `py`. Quote paths with spaces.)

## Stage 2 — gate, then author

**Read `diff.summary.json` and check the header FIRST:**
- `status == "aborted"` → relay `diagnostics.errors[].detail` **verbatim** and STOP. Do not
  guess a narrative (the file was unreadable / encrypted / legacy `.xls`).
- `diagnostics.warnings` with `LOW_CACHE_COVERAGE` (the workbook wasn't saved by Excel, so
  computed values are stale) or `LOW_COL_CONFIDENCE` or `caps_hit` → open the narrative with
  an explicit **hedge banner** sourced from those warnings; if cache coverage is near zero,
  hard-stop and tell the user to open + save the file in Excel and re-run.
- `routing.recommend`: `inline` (small diff) → author in one pass. `fanout_per_sheet` →
  spawn one subagent per changed sheet (give each only that sheet's facts slice plus the
  global `headline_metrics` + `causal_links`), then a reducer stitches them. Never silently
  drop facts; un-narrated ripple becomes an appendix **count** from `totals`.

**Write `narrative.md`** with this structure. Front-matter MUST echo the run id:

```markdown
---
diff_run_id: <copy diff_run_id from diff.json verbatim>
---
# What changed: OLD.xlsx -> NEW.xlsx

## Summary
<2-4 sentences on the actual edits at a high level: the structural rework and the key
 assumption/value changes. About WHAT was done, not the P&L effect.>

## What changed   <- THE CORE; be complete and concrete>
### Assumptions & values changed
<every `input` fact with change_kind value/value_set/value_cleared — old => new, cited
 {Fnnnn}. The substantive edits (an assumption raised/cut, an input swapped/cleared).>
### Structure
<rows/columns/sheets inserted/deleted/moved and named-range changes — say what each is.>
### Labels & formula logic
<text/label changes: summarise as the relabel cascade if they come from the restructure
 (give the count), don't enumerate all. Formula-logic changes only if you ran --formulas.>

## Net effect (brief, secondary)
<A short paragraph or small table from headline_metrics — the downstream movement (Revenue,
 EBITDA, margins), each figure cited {Hnn}. Tight; this is context, not the point. You MAY
 note proven drivers from causal_links ({Cnn}->{Hnn}), hedged by confidence.>

## Caveats / what to verify
<low-confidence regions, advisory formula changes, low-cache sheets, omitted ripple count.>
```

**Grounding rules (the verifier checks these):**
- Every `$`, `%`, `pp`, scaled (`m`/`bn`) or thousands-grouped figure — and any bare large
  number — must be **copied verbatim** from a fact's `*_display` / `delta_display` (not
  `*_raw`, not reworded, not re-rounded, not a unit word like "million").
- Every such figure carries an inline citation token — `{H02}`, `{F0369}`, `{C03}` — that
  resolves to an id in `diff.json`. Put the figure and its citation on the **same line**.
- **Do no arithmetic.** If a number isn't already in the JSON, you may not state it. A `%`
  may not be cited from a `$` fact (the verifier checks the unit dimension).
- Causal verbs ("drove", "because", "due to", "as a result of") are allowed only next to a
  `{Cnn}` that is `path_proven` with `confidence: high`. Otherwise hedge.

## Stage 3 — verify, repair, deliver

```bash
$B xlsx verify narrative.md diff.json     # exit 0 = PASS, 1 = FAIL
```

- **PASS** → present the narrative as the primary answer; link `diff.md` (preview) and
  `diff.json` (full ground truth) so any figure is traceable.
- **FAIL** → read the offending spans, fix ONLY those lines (a wrong figure, a missing or
  wrong citation, an over-strong causal verb), and re-run the verifier. Cap at **2** repair
  passes; if it still fails, strip the unverifiable claims, add a visible
  "_(figures below could not be verified and were removed)_" note, and deliver that.
  **Never present an unverified narrative as final.**

## What the report captures (reference)

| Section | Meaning |
|---|---|
| **headline_metrics** | The closed, citable set of output/total movements (Revenue, EBITDA, margins) at the annual/total column, with `delta_display`. The exec summary's only number source. |
| **causal_links** | Changed inputs whose dependency path (in the new formula graph) reaches a headline. Advisory; confidence-tagged. |
| **Rows/Cols moved/inserted/deleted** | Structural edits — one fact each, not thousands of shifted references. |
| **input facts** (tier=cause) | Hand-typed numbers/text the author changed — the high-signal "what they did". |
| **value facts** (tier=effect) | Computed results that moved (the ripple). Capped to the most material; full count in `totals`. |

## Caveats

- Reads cached computed values, so workbooks must have been **saved by Excel**; a
  library-written file may lack them (`LOW_CACHE_COVERAGE`).
- Column alignment and causal attribution are confidence-tagged; cross-sheet formula
  shifts and bridge/waterfall contribution splits are deliberately NOT asserted.
- Reads the workbooks read-only; never modifies them. Output paths stay under the cwd.
