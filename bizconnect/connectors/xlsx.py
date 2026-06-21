"""xlsx — local Excel workbook tooling (no external service / credentials).

  diff OLD.xlsx NEW.xlsx [-o OUT.md] [--formulas] [--values] [--max N]
        Structural, human-readable diff of two workbooks. Aligns rows first, so
        an inserted/deleted row reads as a move/insert rather than thousands of
        shifted-formula changes. Reports moves, inserts/deletes, hand-edited
        inputs, and -- opt-in -- formula-logic changes (--formulas) and the
        computed-value ripple (--values). Writes Markdown to -o, else stdout.

Reads the workbooks read-only; never modifies them. Cached computed values are
used for row alignment, so the inputs must have been saved by Excel (a workbook
written purely by a library may lack them, degrading alignment quality).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_diff(args):
    p = argparse.ArgumentParser(
        prog="bizconnect xlsx diff",
        description="Structural, human-readable diff of two .xlsx workbooks.")
    p.add_argument("old", help="baseline workbook")
    p.add_argument("new", help="changed workbook")
    p.add_argument("-o", "--out", help="write Markdown report here (else stdout)")
    p.add_argument("--formulas", action="store_true",
                   help="also report shift-corrected formula-logic changes")
    p.add_argument("--values", action="store_true",
                   help="also report computed-value (ripple) changes")
    p.add_argument("--max", type=int, default=0,
                   help="cap rows shown per section per sheet (0 = no cap)")
    ns = p.parse_args(args)

    old, new = Path(ns.old), Path(ns.new)
    for pth in (old, new):
        if not pth.exists():
            sys.exit(f"file not found: {pth}")

    from .. import xlsxdiff       # lazy: only needs openpyxl when actually diffing
    md = xlsxdiff.diff_to_markdown(old, new, formulas=ns.formulas,
                                   values=ns.values, max_rows=ns.max)
    if ns.out:
        out = Path(ns.out)
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(md)
    return 0


VERBS = {"diff": cmd_diff}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown xlsx verb {verb!r}. One of: {', '.join(sorted(VERBS))}")
    return fn(rest) or 0
