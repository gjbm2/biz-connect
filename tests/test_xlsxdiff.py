"""Tests for bizconnect.xlsxdiff (structural workbook diff).

Most tests drive the core on synthetic SheetViews built from {(row, col): value}
maps. This is deliberate: openpyxl cannot *compute* formula values, so a workbook
it writes has no cached values -- and the alignment / formula-shift logic depends
on cached values. Synthetic views are the only way to test those paths
deterministically. One integration test exercises the real-file path end to end.
"""
from __future__ import annotations

import openpyxl
import pytest

from bizconnect import xlsxdiff as xd
from bizconnect.xlsxdiff import SheetView, diff_sheet
from openpyxl.worksheet.formula import ArrayFormula


def _sv(cells):
    """SheetView where content == values (good enough for literal-only rows)."""
    return SheetView(dict(cells), dict(cells))


# --------------------------------------------------------------------------- #
# formatting / helper units                                                   #
# --------------------------------------------------------------------------- #

def test_fmt_variants():
    assert xd.fmt(None) == "(empty)"
    assert xd.fmt(1234.0) == "1,234"          # integral float -> int with commas
    assert xd.fmt(1000) == "1,000"
    assert xd.fmt(0.175) == "0.175"
    assert xd.fmt("  hi\nthere ") == "hi there"
    assert xd.fmt(ArrayFormula(ref="A1:A2", text="=SUM(B1:B2)")) == "{=SUM(B1:B2)}"


def test_is_formula_and_text():
    af = ArrayFormula(ref="A1", text="=SUM(B1:B2)")
    assert xd.is_formula("=A1") and xd.is_formula(af)
    assert not xd.is_formula("plain") and not xd.is_formula(42)
    assert xd.formula_text(af) == "=SUM(B1:B2)"
    assert xd.formula_text("=A1") == "=A1"


def test_values_differ_tolerance():
    assert not xd._values_differ(1.0, 1.0 + 1e-12)     # float noise ignored
    assert xd._values_differ(1.0, 1.1)
    assert not xd._values_differ(None, None)
    assert xd._values_differ(None, 0)
    assert xd._values_differ("a", "b")


def test_deshift_round_trips_a_row_move():
    # =A1 sitting in A2 references the cell above; moved to A3 it should be =A2
    assert xd._deshift("=A1", "A2", "A3") == "=A2"
    assert xd._deshift("=A1", "A2", "A2") == "=A1"     # no move -> unchanged
    assert xd._deshift("=$A$1", "A2", "A3") == "=$A$1"  # absolute ref unaffected


# --------------------------------------------------------------------------- #
# structural diff on synthetic sheets                                         #
# --------------------------------------------------------------------------- #

def test_input_change_detected():
    a = _sv({(1, 1): "Revenue", (1, 2): 100})
    b = _sv({(1, 1): "Revenue", (1, 2): 120})
    inserted, deleted, moved, changed = diff_sheet(a, b)
    assert inserted == [] and deleted == [] and moved == []
    assert len(changed) == 1
    _ra, _rb, inputs, _vals, _fml = changed[0]
    assert inputs == [{"col": 2, "old": 100, "new": 120}]


def test_inserted_row_detected():
    a = _sv({(1, 1): "A", (2, 1): "B"})
    b = _sv({(1, 1): "A", (2, 1): "NEW", (3, 1): "B"})
    inserted, deleted, moved, _changed = diff_sheet(a, b)
    assert inserted == [2] and deleted == [] and moved == []


def test_deleted_row_detected():
    a = _sv({(1, 1): "A", (2, 1): "GONE", (3, 1): "B"})
    b = _sv({(1, 1): "A", (2, 1): "B"})
    inserted, deleted, moved, _changed = diff_sheet(a, b)
    assert deleted == [2] and inserted == [] and moved == []


def test_moved_row_exact_signature():
    a = _sv({(1, 1): "X", (2, 1): "Mover", (2, 2): 5, (3, 1): "Y", (4, 1): "Z"})
    b = _sv({(1, 1): "X", (2, 1): "Y", (3, 1): "Z", (4, 1): "Mover", (4, 2): 5})
    inserted, deleted, moved, _changed = diff_sheet(a, b)
    assert moved == [(2, 4)] and inserted == [] and deleted == []


def test_moved_row_with_rippled_value_matched_by_label():
    # same labelled row relocates AND its value changed -> signatures differ,
    # so it must be recovered by the unique-label pass.
    a = _sv({(1, 1): "X", (2, 1): "Mover", (2, 2): 5, (3, 1): "Y", (4, 1): "Z"})
    b = _sv({(1, 1): "X", (2, 1): "Y", (3, 1): "Z", (4, 1): "Mover", (4, 2): 9})
    inserted, deleted, moved, _changed = diff_sheet(a, b)
    assert moved == [(2, 4)] and inserted == [] and deleted == []


def test_pure_row_shift_does_not_flag_formula():
    # A formula relocated by an inserted row above must NOT be reported as a
    # formula change once shift-corrected. This is the whole point of the tool.
    a = SheetView({(1, 1): "Top", (2, 1): "=A1"},
                  {(1, 1): "Top", (2, 1): 10})
    b = SheetView({(1, 1): "Top", (2, 1): "Inserted", (3, 1): "=A2"},
                  {(1, 1): "Top", (2, 1): "Inserted", (3, 1): 10})
    inserted, deleted, moved, changed = diff_sheet(a, b, want_formulas=True)
    assert inserted == [2]
    # the relocated formula row (old 2 -> new 3) carries no real change
    assert all(not fml for (_ra, _rb, _i, _v, fml) in changed)


def test_genuine_formula_change_is_flagged():
    a = SheetView({(1, 1): "Top", (2, 1): "=A1"},
                  {(1, 1): "Top", (2, 1): 10})
    b = SheetView({(1, 1): "Top", (2, 1): "Inserted", (3, 1): "=A2*2"},
                  {(1, 1): "Top", (2, 1): "Inserted", (3, 1): 10})
    _ins, _del, _moved, changed = diff_sheet(a, b, want_formulas=True)
    fmls = [fml for (_ra, _rb, _i, _v, fml) in changed if fml]
    assert len(fmls) == 1
    assert fmls[0][0]["new"] == "=A2*2"


def test_value_ripple_detected_on_aligned_row():
    # identical formula, but the cached computed result moved
    a = SheetView({(1, 1): "Out", (1, 2): "=B5"}, {(1, 1): "Out", (1, 2): 10})
    b = SheetView({(1, 1): "Out", (1, 2): "=B5"}, {(1, 1): "Out", (1, 2): 12})
    _ins, _del, _moved, changed = diff_sheet(a, b)
    assert len(changed) == 1
    _ra, _rb, _inputs, vals, _fml = changed[0]
    assert vals == [{"col": 2, "old": 10, "new": 12}]


# --------------------------------------------------------------------------- #
# end-to-end on real .xlsx files                                              #
# --------------------------------------------------------------------------- #

def test_compare_real_workbooks(tmp_path):
    old = tmp_path / "old.xlsx"
    new = tmp_path / "new.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"], ws["B1"] = "Revenue", 100
    ws["A2"], ws["B2"] = "Costs", 40
    wb.save(old)

    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Model"
    ws2["A1"], ws2["B1"] = "Revenue", 120          # input changed
    ws2["A2"], ws2["B2"] = "New line", 5           # row inserted
    ws2["A3"], ws2["B3"] = "Costs", 40
    wb2.save(new)

    wd = xd.compare(old, new)
    assert wd.common == ["Model"]
    _vb, _va, sd = wd.sheets["Model"]
    assert sd.inserted == [2]                       # "New line"
    assert sd.deleted == []
    inputs = [it for (_ra, _rb, ins, _v, _f) in sd.changed for it in ins]
    assert {"col": 2, "old": 100, "new": 120} in inputs

    md = xd.render_markdown(wd)
    assert "Workbook diff" in md and "New line" in md and "Inputs changed" in md


def test_identical_workbooks_have_no_diff(tmp_path):
    p1 = tmp_path / "a.xlsx"
    p2 = tmp_path / "b.xlsx"
    for p in (p1, p2):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"], ws["B1"] = "Revenue", 100
        wb.save(p)
    wd = xd.compare(p1, p2)
    _vb, _va, sd = wd.sheets["Sheet"]
    assert sd.is_empty()
    assert "No structural or content differences" in xd.render_markdown(wd)
