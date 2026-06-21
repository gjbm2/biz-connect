"""xlsx — local Excel workbook tooling (no external service / credentials).

  diff OLD.xlsx NEW.xlsx [options]
        Structural, human-readable diff of two workbooks. Aligns rows AND columns
        so an inserted row/column is one fact, not thousands of shifted-formula
        changes. Captures number formats (values read as the model shows them),
        classifies row roles, extracts headline metrics, and (advisory) traces
        changed inputs to the outputs they drive. Emits a deterministic JSON fact
        graph (the ground truth) and/or a capped Markdown preview.
        Options:
          -o,  --out PATH      write the Markdown report (else stdout)
          --json PATH          write the JSON fact graph (the source of truth)
          --format md|json|both   what to print when no file is given (default md)
          --formulas           also report formula-logic changes (advisory)
          --values             also report computed-value (ripple) changes
          --max N              cap rows shown per section per sheet (0 = no cap)
          --no-graph           skip causal attribution (faster)

  verify NARRATIVE.md DIFF.json
        Mechanically check a narrative against a diff.json: binds run ids, rejects
        fabricated/uncited figures and unproven causal phrasing. Exit 0=PASS, 1=FAIL.

Reads workbooks read-only; never modifies them. Output paths are confined under
the current directory (defence-in-depth when an agent supplies the path).
"""
from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path


def _confined(path_str):
    """Resolve an output path and ensure it stays under the current directory."""
    p = Path(path_str)
    base = Path.cwd().resolve()
    full = (base / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        full.relative_to(base)
    except ValueError:
        sys.exit(f"refusing to write outside the working directory: {path_str}")
    return full


def _write(path_str, text):
    out = _confined(path_str)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")


def cmd_diff(args):
    p = argparse.ArgumentParser(prog="bizconnect xlsx diff",
                                description="Structural diff of two .xlsx workbooks.")
    p.add_argument("old"); p.add_argument("new")
    p.add_argument("-o", "--out")
    p.add_argument("--json", dest="json_out")
    p.add_argument("--summary", dest="summary_out",
                   help="write a small, agent-readable JSON (no value ripple)")
    p.add_argument("--format", choices=("md", "json", "both"), default="md")
    p.add_argument("--formulas", action="store_true")
    p.add_argument("--values", action="store_true")
    p.add_argument("--max", type=int, default=0)
    p.add_argument("--no-graph", action="store_true")
    ns = p.parse_args(args)

    for pth in (ns.old, ns.new):
        if not Path(pth).exists():
            sys.exit(f"file not found: {pth}")

    from .. import xlsxdiff
    wd = xlsxdiff.compare(ns.old, ns.new, formulas=ns.formulas, want_graph=not ns.no_graph)
    j = xlsxdiff.emit_json(wd)
    md = xlsxdiff.render_markdown(wd, values=ns.values, formulas=ns.formulas,
                                  max_rows=ns.max, headlines=j.get("headline_metrics"),
                                  causal=j.get("causal_links"))
    blob = _json.dumps(j, ensure_ascii=True, indent=1)

    if ns.json_out:
        _write(ns.json_out, blob)
    if ns.summary_out:
        summ = xlsxdiff.emit_summary_json(wd)
        _write(ns.summary_out, _json.dumps(summ, ensure_ascii=True, indent=1))
    if ns.out:
        _write(ns.out, md)
    if not ns.json_out and not ns.out:
        if ns.format == "json":
            print(blob)
        elif ns.format == "both":
            print(md); print("\n---\n"); print(blob)
        else:
            print(md)
    if wd.status == "aborted":
        print("status: ABORTED — see diagnostics.errors", file=sys.stderr)
    return 0


def cmd_verify(args):
    p = argparse.ArgumentParser(prog="bizconnect xlsx verify")
    p.add_argument("narrative"); p.add_argument("json")
    ns = p.parse_args(args)
    from .. import xlsxverify
    result = xlsxverify.verify_files(ns.narrative, ns.json)
    print(xlsxverify.format_report(result))
    return 0 if result["status"] == "PASS" else 1


VERBS = {"diff": cmd_diff, "verify": cmd_verify}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown xlsx verb {verb!r}. One of: {', '.join(sorted(VERBS))}")
    return fn(rest) or 0
