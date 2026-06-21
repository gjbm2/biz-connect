"""Structural, human-readable diff of two .xlsx workbooks.

Financial models are full of formulas that reference other cells. Insert or
delete a single row and Excel rewrites thousands of references -- a naive
cell-by-cell diff then drowns in shift noise (``=A227`` -> ``=A228`` repeated
ten thousand times). This module diffs *structurally*:

  1. Align the rows of each sheet (so a pure insertion shows up as
     "1 row moved/inserted", not 15,000 reference shifts).
  2. On aligned rows, report the changes a human actually cares about:
       * INPUT changes   -- a hand-typed number/text the author changed
       * VALUE changes   -- a cell whose computed result moved (ripple)
       * FORMULA changes -- formula logic rewritten (shift-corrected, so a
                            row-move alone is *not* reported)

Row alignment uses each row's cached computed values plus its leftmost text
label as a shift-invariant signature, matched with difflib. Relocated rows are
recovered as "moves" by re-pairing an insert and a delete that share a
signature (or, failing that, a unique label). Formula comparison uses openpyxl's
Translator to de-shift the old formula to the new row before comparing.

Public API:
    compare(old_path, new_path, *, formulas=False) -> WorkbookDiff
    render_markdown(wd, *, values=False, formulas=False, max_rows=0) -> str
    diff_to_markdown(old, new, *, formulas=False, values=False, max_rows=0) -> str

The core (SheetView, diff_sheet) operates on plain {(row, col): value} maps so it
can be unit-tested without round-tripping real workbooks (openpyxl cannot
compute formula values, so synthetic SheetViews are the only way to test the
cached-value-dependent paths deterministically).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from math import floor, log10
from pathlib import Path

import openpyxl
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula


# --------------------------------------------------------------------------- #
# Formatting helpers                                                           #
# --------------------------------------------------------------------------- #

def fmt(value):
    """Render a cell value compactly for human reading."""
    if value is None:
        return "(empty)"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value == int(value):
            return f"{int(value):,}"
        return f"{value:,.10g}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, ArrayFormula):
        return "{" + str(value.text) + "}"
    return str(value).replace("\n", " ").strip()


def addr(r, c):
    return f"{get_column_letter(c)}{r}"


def is_formula(v):
    return isinstance(v, ArrayFormula) or (isinstance(v, str) and v.startswith("="))


def formula_text(v):
    """The formula string, unwrapping openpyxl's ArrayFormula container."""
    if isinstance(v, ArrayFormula):
        return v.text
    return v


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #

def _sheet_cells(ws):
    cells = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None:
                cells[(cell.row, cell.column)] = cell.value
    return cells


def load_workbook_maps(path):
    """Return (sheet order, content map, value map) for a workbook.

    content map: {sheet: {(r, c): formula-or-literal}}   (data_only=False)
    value map:   {sheet: {(r, c): cached computed value}} (data_only=True)
    """
    wb_f = openpyxl.load_workbook(path, data_only=False, read_only=True)
    wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
    order = list(wb_f.sheetnames)
    content = {name: _sheet_cells(wb_f[name]) for name in order}
    values = {name: _sheet_cells(wb_v[name]) for name in wb_v.sheetnames}
    wb_f.close()
    wb_v.close()
    return order, content, values


# --------------------------------------------------------------------------- #
# Per-sheet row model                                                          #
# --------------------------------------------------------------------------- #

def _round_sig(v, sig=6):
    if isinstance(v, float):
        if v == 0:
            return 0.0
        d = sig - 1 - floor(log10(abs(v)))
        return round(v, d)
    return v


class SheetView:
    """Row-indexed view of a sheet built from content + value maps.

    Constructible directly from dicts for testing:
        SheetView({(1, 1): "=A2", ...}, {(1, 1): 42, ...})
    """

    def __init__(self, content, values):
        self.content = dict(content)             # {(r,c): formula/literal}
        self.values = dict(values)               # {(r,c): cached value}
        rows = {r for (r, _c) in self.content} | {r for (r, _c) in self.values}
        self.min_row = min(rows) if rows else 1
        self.max_row = max(rows) if rows else 0
        cols = {c for (_r, c) in self.content} | {c for (_r, c) in self.values}
        self.max_col = max(cols) if cols else 0
        self._row_cells_content = self._index_by_row(self.content)
        self._row_cells_values = self._index_by_row(self.values)

    @staticmethod
    def _index_by_row(m):
        out = {}
        for (r, c), v in m.items():
            out.setdefault(r, {})[c] = v
        return out

    def label(self, r, max_col=6):
        """Leftmost text label in the row (the model's line name)."""
        rc = self._row_cells_values.get(r, {})
        cc = self._row_cells_content.get(r, {})
        for c in range(1, max_col + 1):
            v = rc.get(c)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for c in range(1, max_col + 1):
            v = cc.get(c)
            if isinstance(v, str) and v.strip() and not is_formula(v):
                return v.strip()
        return None

    def signature(self, r):
        """Shift-invariant identity for a row: label + rounded value tuple."""
        label = (self.label(r) or "").lower()
        vals = self._row_cells_values.get(r, {})
        vsig = tuple(sorted((c, _round_sig(v)) for c, v in vals.items()
                            if not isinstance(v, str) or v.strip()))
        return (label, vsig)

    def row_content(self, r):
        return self._row_cells_content.get(r, {})

    def row_values(self, r):
        return self._row_cells_values.get(r, {})

    def is_blank(self, r):
        return not self._row_cells_content.get(r) and not self._row_cells_values.get(r)


# --------------------------------------------------------------------------- #
# Diff primitives                                                              #
# --------------------------------------------------------------------------- #

def _values_differ(a, b):
    if a is None and b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) > 1e-6 * max(1.0, abs(a), abs(b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b))
    return a != b


def _deshift(formula, old_addr, new_addr):
    """Translate `formula` from old cell to new cell (correct relative refs)."""
    if old_addr == new_addr:
        return formula
    try:
        return Translator(formula, origin=old_addr).translate_formula(new_addr)
    except Exception:
        return formula


def diff_aligned_row(va, ra, vb, rb, want_formulas):
    """Diff one aligned row pair (old row ra <-> new row rb).

    Returns (inputs, valuechanges, formulas), each a list of per-column dicts.
    """
    inputs, valuechanges, formulas = [], [], []
    ca, cb = va.row_content(ra), vb.row_content(rb)
    xa, xb = va.row_values(ra), vb.row_values(rb)
    for c in sorted(set(ca) | set(cb)):
        a, b = ca.get(c), cb.get(c)
        af, bf = is_formula(a), is_formula(b)

        # INPUT: literal (non-formula) content typed by the author
        if not af and not bf:
            if a != b:
                inputs.append({"col": c, "old": a, "new": b})
            continue

        # FORMULA logic change (shift-corrected)
        if want_formulas:
            at = formula_text(a) if af else a
            bt = formula_text(b) if bf else b
            a_norm = _deshift(at, addr(ra, c), addr(rb, c)) if af else at
            if a_norm != bt:
                formulas.append({"col": c, "old": at, "new": bt, "old_norm": a_norm})

        # VALUE change on a formula/computed cell
        xva, xvb = xa.get(c), xb.get(c)
        if _values_differ(xva, xvb):
            valuechanges.append({"col": c, "old": xva, "new": xvb})

    return inputs, valuechanges, formulas


def diff_sheet(va: SheetView, vb: SheetView, want_formulas=False):
    """Diff one sheet. Returns (inserted, deleted, moved, changed).

    inserted/deleted: row indices (new/old respectively).
    moved:  list of (old_row, new_row).
    changed: list of (old_row, new_row, inputs, valuechanges, formulas).
    """
    rows_a = list(range(va.min_row, va.max_row + 1)) if va.max_row else []
    rows_b = list(range(vb.min_row, vb.max_row + 1)) if vb.max_row else []
    sig_a = [va.signature(r) for r in rows_a]
    sig_b = [vb.signature(r) for r in rows_b]

    sm = SequenceMatcher(a=sig_a, b=sig_b, autojunk=False)

    inserted, deleted, changed = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ra, rb = rows_a[i1 + k], rows_b[j1 + k]
                ins, val, fml = diff_aligned_row(va, ra, vb, rb, want_formulas)
                if ins or val or fml:
                    changed.append((ra, rb, ins, val, fml))
        elif tag == "replace":
            la, lb = i2 - i1, j2 - j1
            n = min(la, lb)
            for k in range(n):
                ra, rb = rows_a[i1 + k], rows_b[j1 + k]
                ins, val, fml = diff_aligned_row(va, ra, vb, rb, want_formulas)
                if ins or val or fml:
                    changed.append((ra, rb, ins, val, fml))
            for k in range(n, lb):
                rb = rows_b[j1 + k]
                if not vb.is_blank(rb):
                    inserted.append(rb)
            for k in range(n, la):
                ra = rows_a[i1 + k]
                if not va.is_blank(ra):
                    deleted.append(ra)
        elif tag == "insert":
            for rb in rows_b[j1:j2]:
                if not vb.is_blank(rb):
                    inserted.append(rb)
        elif tag == "delete":
            for ra in rows_a[i1:i2]:
                if not va.is_blank(ra):
                    deleted.append(ra)

    moved, inserted, deleted = _match_moves(va, vb, inserted, deleted)
    return inserted, deleted, moved, changed


def _match_moves(va, vb, inserted, deleted):
    """Collapse insert+delete pairs of the same row into moves.

    difflib reports a relocated row as a delete at its old position and an
    insert at its new one. Re-pair them so the report reads "row moved".

    Pass 1 matches on full signature (relocated, unchanged rows). Pass 2 matches
    a remaining insert+delete that share a *unique* label (rows that moved and
    also had their computed values rippled, so signatures no longer match).
    """
    from collections import Counter, defaultdict

    del_by_sig = defaultdict(list)
    for ra in deleted:
        del_by_sig[va.signature(ra)].append(ra)

    moved, rem_inserted, used = [], [], set()
    for rb in inserted:
        cand = del_by_sig.get(vb.signature(rb))
        ra = None
        while cand:
            x = cand.pop(0)
            if x not in used:
                ra = x
                break
        if ra is not None:
            used.add(ra)
            moved.append((ra, rb))
        else:
            rem_inserted.append(rb)
    rem_deleted = [ra for ra in deleted if ra not in used]

    ins_labels = Counter(vb.label(rb) for rb in rem_inserted)
    del_labels = Counter(va.label(ra) for ra in rem_deleted)
    del_by_label = {}
    for ra in rem_deleted:
        del_by_label.setdefault(va.label(ra), []).append(ra)

    final_inserted, used2 = [], set()
    for rb in rem_inserted:
        lab = vb.label(rb)
        if lab and ins_labels[lab] == 1 and del_labels.get(lab) == 1:
            ra = del_by_label[lab][0]
            moved.append((ra, rb))
            used2.add(ra)
        else:
            final_inserted.append(rb)
    final_deleted = [ra for ra in rem_deleted if ra not in used2]

    moved.sort(key=lambda t: t[1])
    return moved, final_inserted, final_deleted


# --------------------------------------------------------------------------- #
# Top-level diff objects                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class SheetDiff:
    inserted: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    moved: list = field(default_factory=list)
    changed: list = field(default_factory=list)   # (ra, rb, inputs, values, formulas)

    @property
    def n_inputs(self):
        return sum(len(c[2]) for c in self.changed)

    @property
    def n_values(self):
        return sum(len(c[3]) for c in self.changed)

    @property
    def n_formulas(self):
        return sum(len(c[4]) for c in self.changed)

    def is_empty(self):
        return not (self.inserted or self.deleted or self.moved or self.changed)


@dataclass
class WorkbookDiff:
    old_name: str
    new_name: str
    added_sheets: list
    removed_sheets: list
    common: list                       # ordered common sheet names
    sheets: dict                       # name -> (vb, va, SheetDiff)
    computed_formulas: bool = False


def compare(old_path, new_path, *, formulas=False) -> WorkbookDiff:
    """Diff two workbooks on disk and return a structured WorkbookDiff."""
    old_path, new_path = Path(old_path), Path(new_path)
    order_a, content_a, values_a = load_workbook_maps(old_path)
    order_b, content_b, values_b = load_workbook_maps(new_path)

    sheets_a, sheets_b = set(order_a), set(order_b)
    added = [s for s in order_b if s not in sheets_a]
    removed = [s for s in order_a if s not in sheets_b]
    common = [s for s in order_b if s in sheets_a]

    sheets = {}
    for name in common:
        va = SheetView(content_a.get(name, {}), values_a.get(name, {}))
        vb = SheetView(content_b.get(name, {}), values_b.get(name, {}))
        ins, dele, moved, changed = diff_sheet(va, vb, want_formulas=formulas)
        sheets[name] = (vb, va, SheetDiff(ins, dele, moved, changed))

    return WorkbookDiff(old_path.name, new_path.name, added, removed, common,
                        sheets, computed_formulas=formulas)


# --------------------------------------------------------------------------- #
# Markdown rendering                                                           #
# --------------------------------------------------------------------------- #

def _row_preview(view, r, max_cells=6):
    vals = view.row_values(r)
    parts = [fmt(vals[c]) for c in sorted(vals)[:max_cells]]
    return "[" + ", ".join(parts) + "]" if parts else ""


def _cap(seq, n):
    return seq[:n] if n else seq


def _more(L, seq, n, noun):
    if n and len(seq) > n:
        L.append(f"- ... and {len(seq) - n} more {noun}")


def render_markdown(wd: WorkbookDiff, *, values=False, formulas=False, max_rows=0) -> str:
    formulas = formulas and wd.computed_formulas
    L = []
    L.append("# Workbook diff (structural)")
    L.append("")
    L.append(f"- **OLD**: `{wd.old_name}`")
    L.append(f"- **NEW**: `{wd.new_name}`")
    if not values:
        L.append("- _Computed-value ripple omitted; pass `--values` to include it._")
    L.append("")

    L.append("## Summary")
    L.append("")
    if wd.added_sheets:
        L.append(f"- Sheets **added**: {', '.join(wd.added_sheets)}")
    if wd.removed_sheets:
        L.append(f"- Sheets **removed**: {', '.join(wd.removed_sheets)}")
    L.append("")
    header = "| Sheet | Inserted | Deleted | Moved | Inputs changed | Values moved |"
    rule = "|---|---|---|---|---|---|"
    if formulas:
        header += " Formulas changed |"
        rule += "---|"
    L.append(header)
    L.append(rule)
    for name in wd.common:
        _vb, _va, sd = wd.sheets[name]
        if sd.is_empty():
            continue
        row = (f"| {name} | {len(sd.inserted)} | {len(sd.deleted)} | {len(sd.moved)} "
               f"| {sd.n_inputs} | {sd.n_values} |")
        if formulas:
            row += f" {sd.n_formulas} |"
        L.append(row)
    L.append("")

    L.append("## Detail")
    L.append("")
    any_detail = False
    for name in wd.common:
        vb, va, sd = wd.sheets[name]
        show_values = values and any(c[3] for c in sd.changed)
        if sd.is_empty():
            continue
        if not (sd.inserted or sd.deleted or sd.moved or any(c[2] for c in sd.changed)
                or (formulas and any(c[4] for c in sd.changed)) or show_values):
            continue
        any_detail = True
        L.append(f"### {name}")
        L.append("")

        if sd.moved:
            L.append(f"**Rows moved ({len(sd.moved)})**")
            L.append("")
            for ra, rb in _cap(sd.moved, max_rows):
                lab = vb.label(rb) or va.label(ra) or "(no label)"
                delta = rb - ra
                dirn = f"down {delta}" if delta > 0 else f"up {-delta}"
                L.append(f"- **{lab}**: old row {ra} -> new row {rb} ({dirn})")
            _more(L, sd.moved, max_rows, "moved rows")
            L.append("")

        if sd.inserted:
            L.append(f"**Rows inserted ({len(sd.inserted)})**")
            L.append("")
            for rb in _cap(sd.inserted, max_rows):
                lab = vb.label(rb) or "(no label)"
                L.append(f"- new row {rb}: **{lab}** {_row_preview(vb, rb)}")
            _more(L, sd.inserted, max_rows, "inserted rows")
            L.append("")

        if sd.deleted:
            L.append(f"**Rows deleted ({len(sd.deleted)})**")
            L.append("")
            for ra in _cap(sd.deleted, max_rows):
                lab = va.label(ra) or "(no label)"
                L.append(f"- old row {ra}: **{lab}** {_row_preview(va, ra)}")
            _more(L, sd.deleted, max_rows, "deleted rows")
            L.append("")

        input_rows = [(ra, rb, ins) for (ra, rb, ins, _v, _f) in sd.changed if ins]
        if input_rows:
            L.append("**Inputs changed**")
            L.append("")
            for ra, rb, ins in _cap(input_rows, max_rows):
                lab = vb.label(rb) or va.label(ra) or "(no label)"
                for it in ins:
                    a = addr(rb, it["col"])
                    L.append(f"- {a} **{lab}**: `{fmt(it['old'])}` => `{fmt(it['new'])}`")
            _more(L, input_rows, max_rows, "rows with input changes")
            L.append("")

        value_rows = [(ra, rb, v) for (ra, rb, _i, v, _f) in sd.changed if v]
        if value_rows and values:
            L.append("**Values moved** (computed results)")
            L.append("")
            for ra, rb, v in _cap(value_rows, max_rows):
                lab = vb.label(rb) or va.label(ra) or "(no label)"
                cells = ", ".join(
                    f"{addr(rb, it['col'])} {fmt(it['old'])}=>{fmt(it['new'])}"
                    for it in v[:8])
                extra = f" (+{len(v) - 8} more)" if len(v) > 8 else ""
                L.append(f"- row {rb} **{lab}**: {cells}{extra}")
            _more(L, value_rows, max_rows, "rows with value changes")
            L.append("")

        if formulas:
            fml_rows = [(ra, rb, f) for (ra, rb, _i, _v, f) in sd.changed if f]
            if fml_rows:
                L.append("**Formulas changed** (shift-corrected)")
                L.append("")
                for ra, rb, f in _cap(fml_rows, max_rows):
                    lab = vb.label(rb) or va.label(ra) or "(no label)"
                    for it in f:
                        a = addr(rb, it["col"])
                        L.append(f"- {a} **{lab}**: `{fmt(it['old'])}` => `{fmt(it['new'])}`")
                _more(L, fml_rows, max_rows, "rows with formula changes")
                L.append("")

    if not any_detail and not wd.added_sheets and not wd.removed_sheets:
        L.append("_No structural or content differences found._")

    return "\n".join(L)


def diff_to_markdown(old_path, new_path, *, formulas=False, values=False, max_rows=0) -> str:
    wd = compare(old_path, new_path, formulas=formulas)
    return render_markdown(wd, values=values, formulas=formulas, max_rows=max_rows)
