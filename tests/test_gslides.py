"""Tests of the Google Slides connector. Nothing here calls Google: the Slides service is an
in-memory fake (FakeSlides) that serves a synthetic presentation and records batchUpdates."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from bizconnect import cli
from bizconnect.connectors import gslides as G

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "slides-edit" / "SKILL.md"
EMU = G.EMU_IN


# ------------------------------------------------------------------ a synthetic presentation
def _text(*runs, align="START"):
    """A Slides text object from (content, style) runs."""
    els, pos = [], 0
    full = "".join(c for c, _s in runs)
    els.append({"endIndex": len(full), "paragraphMarker": {"style": {"alignment": align}}})
    for c, st in runs:
        els.append({"startIndex": pos, "endIndex": pos + len(c), "textRun": {"content": c, "style": st}})
        pos += len(c)
    return {"textElements": els}


def _style(font="Arial", size=12, bold=False, italic=False, rgb=None):
    st = {"fontFamily": font, "fontSize": {"magnitude": size, "unit": "PT"}, "bold": bold, "italic": italic}
    st["foregroundColor"] = {"opaqueColor": {"rgbColor": rgb if rgb is not None else {}}}
    return st


def _box(oid, x, y, w, h, text=None, shape="TEXT_BOX", placeholder=None, **props):
    el = {"objectId": oid, "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
                                    "height": {"magnitude": 3000000, "unit": "EMU"}},
          "transform": {"scaleX": w * EMU / 3000000, "scaleY": h * EMU / 3000000,
                        "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"},
          "shape": {"shapeType": shape, "shapeProperties": props}}
    if text is not None:
        el["shape"]["text"] = text
    if placeholder:
        el["shape"]["placeholder"] = placeholder
    return el


def _slide(oid, layout, master, elements, skipped=False, notes=""):
    notes_el = {"objectId": oid + "_notes", "shape": {"shapeType": "TEXT_BOX"}}
    if notes:
        notes_el["shape"]["text"] = _text((notes + "\n", {}))
    return {"objectId": oid, "pageElements": elements,
            "slideProperties": {"layoutObjectId": layout, "masterObjectId": master, "isSkipped": skipped,
                                "notesPage": {"notesProperties": {"speakerNotesObjectId": oid + "_notes"},
                                              "pageElements": [notes_el]}}}


def make_pres():
    title = _box("t1", 0.69, 0.94, 11.95, 0.47,
                 _text(("Rules versus principles", _style(size=28, bold=True, rgb={"red": 0.109804, "green": 0.098039,
                                                                                  "blue": 0.090196})), ("\n", {})))
    big = _box("big", 0.7, 3.0, 1.0, 1.0, _text(("01", _style(size=50, bold=True)), ("\n", {})))
    table = {"objectId": "tbl1", "size": {"width": {"magnitude": 3000000}, "height": {"magnitude": 3000000}},
             "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0.69 * EMU, "translateY": 2.24 * EMU,
                           "unit": "EMU"},
             "table": {"rows": 2, "columns": 2,
                       "tableColumns": [{"columnWidth": {"magnitude": 2 * EMU}}, {"columnWidth": {"magnitude": 3 * EMU}}],
                       "tableRows": [
                           {"rowHeight": {"magnitude": 0.5 * EMU}, "tableCells": [
                               {"text": _text(("Head A", _style(size=13)), ("\n", {}), align="CENTER"),
                                "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {
                                    "color": {"rgbColor": {"red": 0.992157, "green": 0.901961, "blue": 0.737255}}}}}},
                               {"text": _text(("Head B", _style(size=13)), ("\n", {}), align="CENTER"),
                                "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {
                                    "color": {"rgbColor": {"red": 0.992157, "green": 0.901961, "blue": 0.737255}}}}}}]},
                           {"rowHeight": {"magnitude": 0.8 * EMU}, "tableCells": [
                               {"text": _text(("Body", _style(size=11)), ("\n", {}))},
                               {"text": _text(("Body two", _style(size=11, bold=True, italic=True)), ("\n", {}))}]}],
                       "horizontalBorderRows": [{"tableBorderCells": [{"tableBorderProperties": {
                           "tableBorderFill": {"solidFill": {"color": {"rgbColor": {"red": 0.619608, "green": 0.619608,
                                                                                    "blue": 0.619608}}}},
                           "weight": {"magnitude": 19050, "unit": "EMU"}, "dashStyle": "SOLID"}}]}]}}
    line = {"objectId": "rule1", "size": {"width": {"magnitude": 3000000}, "height": {"magnitude": 3000000}},
            "transform": {"scaleX": 2.7432, "translateX": 0.5 * EMU, "translateY": 4.7 * EMU, "unit": "EMU"},
            "line": {"lineCategory": "STRAIGHT", "lineProperties": {
                "lineFill": {"solidFill": {"color": {"themeColor": "ACCENT1"}}},
                "weight": {"magnitude": 1, "unit": "PT"}}}}
    group = {"objectId": "grp1", "transform": {"scaleX": 1, "scaleY": 1, "translateX": 1 * EMU, "translateY": 1 * EMU},
             "elementGroup": {"children": [_box("g_a", 0, 0, 1, 1, _text(("A", _style()), ("\n", {}))),
                                           _box("g_b", 2, 1, 1, 1, _text(("B", _style()), ("\n", {})))]}}
    ph_title = _box("ph1", 0.5, 0.5, 9, 1, _text(("Placeholder title", {}), ("\n", {})),
                    placeholder={"type": "TITLE", "parentObjectId": "lay_title_ph"})
    own = _box("bc_abc123_old", 1, 5, 2, 0.5, _text(("from an earlier build", _style()), ("\n", {})))
    own_ph = _box("bc_abc123_ph", 1, 6, 2, 0.5, placeholder={"type": "BODY"})
    return {
        "presentationId": "DECK123", "title": "Test deck", "revisionId": "rev1",
        "pageSize": {"width": {"magnitude": 12192000, "unit": "EMU"}, "height": {"magnitude": 6858000, "unit": "EMU"}},
        "masters": [
            {"objectId": "m1", "masterProperties": {"displayName": "Theme A"},
             "pageProperties": {"colorScheme": {"colors": [{"type": "ACCENT1", "color": {"red": 1, "green": 0.5}}]},
                                "pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1, "green": 1,
                                                                                            "blue": 1}}}}}},
            {"objectId": "m2", "masterProperties": {"displayName": "Theme B"}}],
        "layouts": [
            {"objectId": "L_blank1", "layoutProperties": {"masterObjectId": "m1", "name": "BLANK", "displayName": "Blank"},
             "pageElements": []},
            {"objectId": "L_title1", "layoutProperties": {"masterObjectId": "m1", "name": "TITLE_ONLY",
                                                          "displayName": "Title only"},
             "pageElements": [_box("lay_title_ph", 0.5, 0.5, 9, 1, _text(("x", _style(font="Georgia", size=30)),
                                                                          ("\n", {})),
                                   placeholder={"type": "TITLE"})]},
            {"objectId": "L_blank2", "layoutProperties": {"masterObjectId": "m2", "name": "TITLE_AND_BODY",
                                                          "displayName": "Blank"}, "pageElements": []}],
        "slides": [
            _slide("s1", "L_title1", "m1", [ph_title]),
            _slide("s2", "L_blank1", "m1", [title, table, big, line, group], skipped=True, notes="Say this"),
            _slide("s3", "L_blank2", "m2", [own, own_ph]),
        ]}


class FakeRequest:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeSlides:
    """presentations().get / batchUpdate / pages().getThumbnail; records every batchUpdate."""

    def __init__(self, pres=None, error=None):
        self.pres = pres or make_pres()
        self.batches = []
        self.error = error

    def presentations(self):
        return self

    def pages(self):
        return self

    def get(self, presentationId):
        return FakeRequest(lambda: copy.deepcopy(self.pres))

    def batchUpdate(self, presentationId, body):
        def run():
            if self.error:
                raise self.error
            self.batches.append(copy.deepcopy(body))
            for r in body["requests"]:                  # enough state for follow-up reads
                if "createSlide" in r:
                    c = r["createSlide"]
                    self.pres["slides"].insert(c["insertionIndex"], _slide(
                        c["objectId"], c["slideLayoutReference"]["layoutId"], "m1", []))
            replies = [{"replaceAllText": {"occurrencesChanged": 2}} if "replaceAllText" in r else {}
                       for r in body["requests"]]
            return {"writeControl": {"requiredRevisionId": "rev2"}, "replies": replies}
        return FakeRequest(run)

    def getThumbnail(self, **kw):
        self.thumb_kw = kw
        return FakeRequest(lambda: {"contentUrl": "https://example.invalid/thumb", "width": 1600, "height": 900})


class FakeHttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"message": message}}).encode()


@pytest.fixture
def fake(monkeypatch):
    f = FakeSlides()
    monkeypatch.setattr(G, "_slides_api", lambda: f)
    return f


def run_cli(*args):
    return cli.main(["slides", *args])


# ------------------------------------------------------------------ ids, refs, positions
@pytest.mark.parametrize("s", ["https://docs.google.com/presentation/d/AbC_12-x/edit#slide=id.p1",
                               "https://docs.google.com/presentation/u/0/d/AbC_12-x/edit", "AbC_12-x"])
def test_deck_id(s):
    assert G.deck_id(s) == "AbC_12-x"


def test_deck_id_rejects_other_urls():
    with pytest.raises(G.SpecError):
        G.deck_id("https://example.com/nothing/here")


def test_slide_numbers_and_refs():
    p = make_pres()
    assert G.slide_numbers(p, "2") == [2]
    assert G.slide_numbers(p, "1-3") == [1, 2, 3]
    assert G.slide_numbers(p, "3,1,3") == [3, 1]
    assert G.slide_numbers(p, "all") == [1, 2, 3]
    assert G.slide_numbers(p, "s2") == [2]
    for bad in ("4", "0-2", "nope"):
        with pytest.raises(G.SpecError):
            G.slide_numbers(p, bad)


def test_move_insertion_index_counts_before_the_move():
    # slides A B C D; move B (index 1) to become slide 4 (index 3): insertionIndex 4
    assert G.move_insertion_index(1, 3) == 4
    # move D (index 3) to become slide 1: insertionIndex 0
    assert G.move_insertion_index(3, 0) == 0


def test_position_bounds():
    p = make_pres()
    assert G.position(p, 4) == 3                     # the end
    with pytest.raises(G.SpecError):
        G.position(p, 4, allow_end=False)
    with pytest.raises(G.SpecError):
        G.position(p, 0)


def test_resolve_layout():
    p = make_pres()
    assert G.resolve_layout(p, "title ONLY") == "L_title1"
    assert G.resolve_layout(p, "L_blank2") == "L_blank2"
    assert G.resolve_layout(p, "Blank", master="m2") == "L_blank2"
    assert G.resolve_layout(p, "Blank", near=2) == "L_blank2"      # neighbour slide 3 uses master m2
    assert G.resolve_layout(p, None, near=0) == "L_blank1"         # default layout: Blank
    assert G.resolve_layout(p, "TITLE_AND_BODY") == "L_blank2"     # internal name when no display name matches
    with pytest.raises(G.SpecError, match="ambiguous"):
        G.resolve_layout({"layouts": p["layouts"], "slides": []}, "Blank")
    with pytest.raises(G.SpecError, match="no layout named"):
        G.resolve_layout(p, "Nope")


# ------------------------------------------------------------------ outline
def test_outline_summarises_geometry_text_styles_and_notes():
    d = G.outline(make_pres(), [2])
    assert d["revision"] == "rev1" and d["size_in"] == [13.33, 7.5] and d["slide_count"] == 3
    s = d["slides"][0]
    assert (s["number"], s["index"], s["id"], s["skipped"], s["layout"], s["layout_name"], s["master"]) == \
        (2, 1, "s2", True, "L_blank1", "Blank", "m1")
    assert s["title"] == "Rules versus principles"                 # top of slide beats the bigger "01" lower down
    assert s["notes"] == "Say this"
    els = {e["id"]: e for e in s["elements"]}
    assert els["t1"]["geom"] == "x0.69 y0.94 w11.95 h0.47"
    assert els["t1"]["style"] == ["Arial 28pt bold #1C1917"]
    t = els["tbl1"]
    assert t["type"] == "TABLE 2x2" and t["col_widths"] == [2.0, 3.0] and t["row_heights"] == [0.5, 0.8]
    assert t["geom"] == "x0.69 y2.24 w5 h1.3"
    assert t["rows"][0]["fills"] == "#FDE6BC" and t["rows"][0]["align"] == "CENTER"
    assert "Arial 11pt bold italic #000000" in t["rows"][1]["style"]
    assert t["borders"] == ["#9E9E9E 1.5pt SOLID x1"]
    assert els["rule1"]["geom"] == "x0.5 y4.7 w9 h0"                 # omitted scaleY is 0, not 1
    assert els["rule1"]["outline"] == "theme:ACCENT1(#FF8000) 1pt"
    assert els["grp1"]["type"] == "GROUP (2)" and els["grp1"]["geom"] == "x1 y1 w3 h2"
    text = G.render_outline(d)
    assert "slide 2 [index 1]  id=s2" in text and "HIDDEN" in text and "revision: rev1" in text


def test_outline_placeholder_title_inherits_layout_style():
    d = G.outline(make_pres(), [1])
    s = d["slides"][0]
    assert s["title"] == "Placeholder title" and "title_from" not in s
    assert s["elements"][0]["style"] == ["Georgia 30pt #000000"]
    assert s["elements"][0]["placeholder"] == "TITLE"


def test_outline_full_lists_group_children():
    d = G.outline(make_pres(), [2], full=True)
    grp = next(e for e in d["slides"][0]["elements"] if e["id"] == "grp1")
    assert [c["geom"] for c in grp["children"]] == ["x1 y1 w1 h1", "x3 y2 w1 h1"]


# ------------------------------------------------------------------ markup + text requests
@pytest.mark.parametrize("src,runs", [
    ("plain", [{"text": "plain"}]),
    ("a **b** *c* ***d***", [{"text": "a "}, {"text": "b", "bold": True}, {"text": " "},
                             {"text": "c", "italic": True}, {"text": " "},
                             {"text": "d", "bold": True, "italic": True}]),
    ("Revenue* only", [{"text": "Revenue* only"}]),
    ("5 * 3 = 15", [{"text": "5 * 3 = 15"}]),
    ("2*3*4", [{"text": "2"}, {"text": "3", "italic": True}, {"text": "4"}]),
    ("a literal \\*star", [{"text": "a literal *star"}]),
    ("a ** b ** c", [{"text": "a ** b ** c"}]),
])
def test_parse_markup(src, runs):
    assert G.parse_markup(src) == runs


def test_parse_markup_off():
    assert G.parse_markup("**x**", markup=False) == [{"text": "**x**"}]


def _ranges(reqs, kind):
    return [(r[kind]["textRange"]["startIndex"], r[kind]["textRange"]["endIndex"]) for r in reqs if kind in r]


def test_text_requests_indices_styles_and_bullets():
    paras = G.paragraphs(["Head **bold**", {"text": "one", "bullet": True}, {"text": "two", "bullet": True, "level": 1},
                          "tail \U0001F600 end"], True, "t")
    reqs, _spans = G.text_requests("obj", paras, {"font": "Arial", "size": 12, "color": "#000000"}, {"align": "center"})
    ins = reqs[0]["insertText"]
    assert ins["text"] == "Head bold\none\n\ttwo\ntail \U0001F600 end"
    total = len(ins["text"].encode("utf-16-le")) // 2                 # the emoji is 2 UTF-16 units
    styled = _ranges(reqs, "updateTextStyle")
    assert styled[0][0] == 0 and styled[-1][1] == total                # every character is styled
    assert all(a[1] == b[0] for a, b in zip(styled, styled[1:]))       # contiguous, no gaps or overlaps
    bold = [r for r in reqs if "updateTextStyle" in r and r["updateTextStyle"]["style"].get("bold")]
    assert _ranges(bold, "updateTextStyle") == [(5, 10)]              # "bold" + its paragraph's newline
    assert _ranges(reqs, "updateParagraphStyle") == [(0, total)]        # one group: same alignment
    bullets = [r for r in reqs if "createParagraphBullets" in r]
    assert len(bullets) == 1 and _ranges(bullets, "createParagraphBullets") == [(10, 18)]
    assert reqs.index(bullets[0]) == len(reqs) - 1                      # bullets come last


def test_text_requests_bullet_groups_run_back_to_front():
    paras = G.paragraphs([{"text": "a", "bullet": True}, "gap", {"text": "b", "bullet": "NUMBERED_DIGIT_ALPHA_ROMAN"}],
                         True, "t")
    reqs, _ = G.text_requests("obj", paras, {"size": 10}, {})
    b = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    assert [x["textRange"]["startIndex"] for x in b] == [6, 0]
    assert b[0]["bulletPreset"] == "NUMBERED_DIGIT_ALPHA_ROMAN"


def test_empty_text_makes_no_requests():
    assert G.text_requests("obj", G.paragraphs("", True, "t"), {}, {})[0] == []


def test_paragraph_height_estimate():
    one = G.paragraph_height_pt([("short", 12, False, "Arial")], 500)
    assert one == pytest.approx(12 * 1.2)
    many = G.paragraph_height_pt([("word " * 60, 12, False, "Arial")], 200)
    assert many > 5 * one


# ------------------------------------------------------------------ build
SPEC = {
    "new": {"at": 3, "layout": "Blank", "id": "bc_new_slide"},
    "defaults": {"font": "Arial", "size": 11, "color": "#1C1917"},
    "elements": [
        {"id": "title", "x": 0.69, "y": 0.9, "w": 11.95, "h": 0.5, "text": "A title", "size": 28, "bold": True},
        {"id": "card", "type": "round_rect", "x": 0.7, "y": 1.6, "w": 4, "h": 1, "fill": "#FDE6BC",
         "outline": {"color": "#9E9E9E", "weight": 1.5}, "valign": "middle", "text": "In a *card*"},
        {"id": "grid", "type": "table", "x": 0.69, "y": 3, "col_widths": [4, 4], "row_heights": [0.4, 0.8],
         "header": {"fill": "#FCB747", "bold": True, "align": "center"}, "borders": {"color": "#9E9E9E", "weight": 0.75},
         "merge": [[1, 0, 1, 2]],
         "rows": [["Left", "Right"], [{"text": "Merged cell", "fill": "#EAE4DC"}, ""]]},
        {"id": "rule", "type": "line", "x": 0.69, "y": 6.9, "w": 11.95, "h": 0, "color": "#9E9E9E"},
    ]}


def _kinds(reqs):
    return [next(iter(r)) for r in reqs]


def test_build_new_slide_is_one_ordered_batch():
    reqs, warnings, info = G.build_requests(make_pres(), copy.deepcopy(SPEC), "x.yaml")
    assert warnings == []
    assert reqs[0] == {"createSlide": {"insertionIndex": 2, "slideLayoutReference": {"layoutId": "L_blank1"},
                                       "objectId": "bc_new_slide"}}  # "Blank" from slide 2's master (m1)
    prefix = G._default_prefix("bc_new_slide")
    assert info["created"] == [f"bc_{prefix}_{n}" for n in ("title", "card", "grid", "rule")]
    k = _kinds(reqs)
    assert k.count("createShape") == 2 and k.count("createTable") == 1 and k.count("createLine") == 1
    card = next(r for r in reqs if "updateShapeProperties" in r and r["updateShapeProperties"]["objectId"].endswith("card"))
    props = card["updateShapeProperties"]["shapeProperties"]
    assert props["shapeBackgroundFill"]["solidFill"]["color"]["rgbColor"]["red"] == pytest.approx(0xFD / 255, abs=1e-5)
    assert props["outline"]["weight"] == {"magnitude": 1.5, "unit": "PT"} and props["contentAlignment"] == "MIDDLE"
    merge = next(r for r in reqs if "mergeTableCells" in r)["mergeTableCells"]["tableRange"]
    assert merge == {"location": {"rowIndex": 1, "columnIndex": 0}, "rowSpan": 1, "columnSpan": 2}
    cell_text = [r["insertText"] for r in reqs if "insertText" in r and "cellLocation" in r["insertText"]]
    assert [(t["cellLocation"]["rowIndex"], t["cellLocation"]["columnIndex"]) for t in cell_text] == [(0, 0), (0, 1), (1, 0)]
    widths = [r["updateTableColumnProperties"] for r in reqs if "updateTableColumnProperties" in r]
    assert widths == [{"objectId": f"bc_{prefix}_grid", "columnIndices": [0, 1],
                       "tableColumnProperties": {"columnWidth": {"magnitude": 4 * EMU, "unit": "EMU"}},
                       "fields": "columnWidth"}]
    # every element lands on the new slide
    pages = {r[k]["elementProperties"]["pageObjectId"] for r in reqs for k in r if "elementProperties" in r[k]}
    assert pages == {"bc_new_slide"}


def test_build_new_slide_layout_follows_the_neighbouring_master():
    spec = copy.deepcopy(SPEC)
    spec["new"]["at"] = 4                                  # after slide 3, which uses master m2
    reqs, _w, _i = G.build_requests(make_pres(), spec)
    assert reqs[0]["createSlide"]["slideLayoutReference"]["layoutId"] == "L_blank2"
    spec["new"]["master"] = "m1"
    reqs, _w, _i = G.build_requests(make_pres(), spec)
    assert reqs[0]["createSlide"]["slideLayoutReference"]["layoutId"] == "L_blank1"


def test_rebuild_replaces_only_its_own_elements():
    spec = {"slide": 3, "prefix": "abc123", "elements": [{"id": "new", "x": 1, "y": 1, "w": 2, "h": 1, "text": "hi"}]}
    reqs, _w, info = G.build_requests(make_pres(), spec)
    assert reqs[0] == {"deleteObject": {"objectId": "bc_abc123_old"}}   # the placeholder with our prefix is kept
    assert _kinds(reqs).count("deleteObject") == 1 and info["removed"] == 1
    spec["replace"] = False
    reqs, _w, _i = G.build_requests(make_pres(), spec)
    assert "deleteObject" not in _kinds(reqs)


def test_rebuild_of_a_created_slide_targets_it_in_place():
    p = make_pres()
    p["slides"].append(_slide("bc_new_slide", "L_blank1", "m1", []))
    reqs, _w, info = G.build_requests(p, copy.deepcopy(SPEC))
    assert "createSlide" not in _kinds(reqs) and "rebuilt in place" in info["slide"]


def test_build_notes_on_existing_slide():
    spec = {"slide": "s2", "notes": "New notes", "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1}]}
    reqs, _w, _i = G.build_requests(make_pres(), spec)
    assert reqs[-2:] == [{"deleteText": {"objectId": "s2_notes", "textRange": {"type": "ALL"}}},
                         {"insertText": {"objectId": "s2_notes", "insertionIndex": 0, "text": "New notes"}}]


@pytest.mark.parametrize("spec,msg", [
    ({"elements": []}, "exactly one of"),
    ({"slide": 1, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1, "colour": "#fff"}]}, "unknown key"),
    ({"slide": 1, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1, "color": "red"}]}, "hex colour"),
    ({"slide": 1, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1}]}, "needs h"),
    ({"slide": 1, "elements": [{"x": 1, "y": 1, "w": 1, "h": 1}]}, "needs an id"),
    ({"slide": 1, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1}, {"id": "a", "x": 1, "y": 1, "w": 1, "h": 1}]},
     "duplicate id"),
    ({"slide": 1, "elements": [{"id": "a", "type": "star", "x": 1, "y": 1, "w": 1, "h": 1}]}, "unknown type"),
    ({"slide": 1, "elements": [{"id": "t", "type": "table", "x": 1, "y": 1, "w": 4, "rows": [["a"]],
                                "merge": [[0, 0, 2, 1]]}]}, "outside"),
    ({"new": {"id": "s2"}, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1}]}, None),
])
def test_build_rejects_bad_specs(spec, msg):
    if msg is None:                        # new.id naming an existing slide rebuilds it: not an error
        G.build_requests(make_pres(), spec)
        return
    with pytest.raises(G.SpecError, match=msg):
        G.build_requests(make_pres(), spec)


def test_build_refuses_an_id_used_on_another_slide():
    p = make_pres()
    p["slides"][0]["pageElements"].append(_box("bc_p1_a", 0, 0, 1, 1))
    with pytest.raises(G.SpecError, match="already used"):
        G.build_requests(p, {"slide": 2, "prefix": "p1", "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1}]})


def test_build_warns_on_overflow_and_off_slide():
    spec = {"slide": 1, "elements": [
        {"id": "tight", "x": 1, "y": 1, "w": 2, "h": 0.3, "text": "word " * 40, "size": 14},
        {"id": "wide", "x": 12, "y": 1, "w": 3, "h": 1, "text": "x"},
        {"id": "tbl", "type": "table", "x": 1, "y": 2, "col_widths": [1, 1], "row_heights": [0.3],
         "rows": [["a very long cell text that must wrap onto many lines in a narrow column", "b"]]}]}
    _r, warnings, _i = G.build_requests(make_pres(), spec)
    text = "\n".join(warnings)
    assert "tight: text needs" in text and "wide: extends beyond" in text and "tbl: rows grow" in text


def test_build_ok_when_it_fits():
    spec = {"slide": 1, "elements": [{"id": "ok", "x": 1, "y": 1, "w": 6, "h": 1, "text": "Short line", "size": 14}]}
    assert G.build_requests(make_pres(), spec)[1] == []


# ------------------------------------------------------------------ guarded writes via the CLI
def test_hide_sends_one_guarded_batch(fake, capsys):
    assert run_cli("hide", "DECK123", "1,3") == 0
    body = fake.batches[-1]
    assert body["writeControl"] == {"requiredRevisionId": "rev1"}
    assert body["requests"] == [G.req_skip("s1", True), G.req_skip("s3", True)]
    assert "rev1 -> rev2" in capsys.readouterr().out


def test_unhide(fake):
    run_cli("unhide", "DECK123", "2")
    assert fake.batches[-1]["requests"] == [{"updateSlideProperties": {
        "objectId": "s2", "slideProperties": {"isSkipped": False}, "fields": "isSkipped"}}]


def test_stale_revision_is_refused_before_any_write(fake, capsys):
    with pytest.raises(SystemExit) as e:
        run_cli("hide", "DECK123", "1", "--revision", "old-rev")
    assert e.value.code == 3 and fake.batches == []
    assert "REVISION MISMATCH" in capsys.readouterr().out


def test_api_revision_mismatch_fails_loudly_without_retry(monkeypatch, capsys):
    f = FakeSlides(error=FakeHttpError(400, "The required revision ID 'rev1' does not match the latest revision."))
    monkeypatch.setattr(G, "_slides_api", lambda: f)
    with pytest.raises(SystemExit) as e:
        run_cli("move", "DECK123", "1", "--to", "3")
    assert e.value.code == 3
    out = capsys.readouterr().out
    assert "REVISION MISMATCH [400]" in out and "Nothing was written" in out


def test_transient_write_error_says_the_outcome_is_unknown(monkeypatch, capsys):
    f = FakeSlides(error=FakeHttpError(503, "Service unavailable"))
    monkeypatch.setattr(G, "_slides_api", lambda: f)
    with pytest.raises(SystemExit) as e:
        run_cli("hide", "DECK123", "1")
    assert e.value.code == 1
    assert "may or may not have been applied" in capsys.readouterr().out


def test_access_error_names_who_to_share_with(monkeypatch, capsys):
    f = FakeSlides(error=FakeHttpError(403, "The caller does not have permission"))
    monkeypatch.setattr(G, "_slides_api", lambda: f)
    monkeypatch.setattr(G._google, "impersonation_subject", lambda data=None: "someone@example.com")
    with pytest.raises(SystemExit) as e:
        run_cli("hide", "DECK123", "1")
    assert e.value.code == 1
    assert "share the presentation with someone@example.com" in capsys.readouterr().out


def test_dry_run_sends_nothing(fake, capsys):
    run_cli("insert", "DECK123", "--at", "2", "--layout", "title only", "--id", "new_slide_1", "--dry-run")
    assert fake.batches == []
    body = json.loads(capsys.readouterr().out)
    assert body["requests"] == [{"createSlide": {"insertionIndex": 1, "slideLayoutReference": {"layoutId": "L_title1"},
                                                 "objectId": "new_slide_1"}}]


def test_move_and_duplicate(fake):
    run_cli("move", "DECK123", "s1", "--to", "3")
    assert fake.batches[-1]["requests"] == [G.req_move(["s1"], 3)]
    run_cli("duplicate", "DECK123", "3", "--at", "1", "--id", "copy_of_3")
    assert fake.batches[-1]["requests"] == [
        {"duplicateObject": {"objectId": "s3", "objectIds": {"s3": "copy_of_3"}}}, G.req_move(["copy_of_3"], 0)]
    run_cli("duplicate", "DECK123", "1", "--id", "copy_of_1")          # no --at: stays after the original
    assert len(fake.batches[-1]["requests"]) == 1


def test_notes_replace_and_clear(fake):
    run_cli("notes", "DECK123", "2", "--text", "Line one\nLine two")
    assert fake.batches[-1]["requests"] == [
        {"deleteText": {"objectId": "s2_notes", "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": "s2_notes", "insertionIndex": 0, "text": "Line one\nLine two"}}]
    run_cli("notes", "DECK123", "1", "--text", "Fresh")                  # empty notes: nothing to delete
    assert fake.batches[-1]["requests"] == [
        {"insertText": {"objectId": "s1_notes", "insertionIndex": 0, "text": "Fresh"}}]
    run_cli("notes", "DECK123", "2", "--clear")
    assert fake.batches[-1]["requests"] == [{"deleteText": {"objectId": "s2_notes", "textRange": {"type": "ALL"}}}]


def test_apply_reads_a_request_file(fake, tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"requests": [G.req_skip("s1", True)],
                             "writeControl": {"requiredRevisionId": "ignored"}}), encoding="utf-8")
    assert run_cli("apply", "DECK123", str(f)) == 0
    assert fake.batches[-1] == {"requests": [G.req_skip("s1", True)], "writeControl": {"requiredRevisionId": "rev1"}}


def test_apply_rejects_malformed_requests(fake, tmp_path, capsys):
    f = tmp_path / "r.json"
    f.write_text(json.dumps([{"a": 1, "b": 2}]), encoding="utf-8")
    assert run_cli("apply", "DECK123", str(f)) == 2 and fake.batches == []


def test_replace_reports_occurrences(fake, capsys):
    run_cli("replace", "DECK123", "--find", "old", "--with", "new", "--slides", "2")
    r = fake.batches[-1]["requests"][0]["replaceAllText"]
    assert r == {"containsText": {"text": "old", "matchCase": False}, "replaceText": "new", "pageObjectIds": ["s2"]}
    assert "replaced 2 occurrence(s)" in capsys.readouterr().out


def test_build_via_cli_writes_one_batch(fake, tmp_path, capsys):
    spec = tmp_path / "slide.yaml"
    spec.write_text("slide: 2\nelements:\n  - {id: box, type: rect, x: 1, y: 1, w: 2, h: 1, fill: '#FDE6BC', "
                    "text: 'Hello **there**'}\n", encoding="utf-8")
    assert run_cli("build", "DECK123", str(spec)) == 0
    assert len(fake.batches) == 1 and fake.batches[0]["writeControl"]["requiredRevisionId"] == "rev1"
    out = capsys.readouterr().out
    assert "built 1 element(s)" in out


def test_build_new_slide_sets_notes_in_a_second_guarded_write(fake, tmp_path, capsys):
    spec = tmp_path / "slide.json"
    spec.write_text(json.dumps({"new": {"id": "bc_fresh_slide"}, "notes": "Talk track",
                                "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1, "text": "x"}]}),
                    encoding="utf-8")
    assert run_cli("build", "DECK123", str(spec)) == 0
    assert len(fake.batches) == 2
    assert fake.batches[0]["requests"][0]["createSlide"]["objectId"] == "bc_fresh_slide"
    assert fake.batches[1]["requests"] == [
        {"insertText": {"objectId": "bc_fresh_slide_notes", "insertionIndex": 0, "text": "Talk track"}}]
    assert all("requiredRevisionId" in b["writeControl"] for b in fake.batches)


def test_build_out_writes_requests_without_sending(fake, tmp_path):
    spec = tmp_path / "slide.json"
    spec.write_text(json.dumps({"slide": 2, "elements": [{"id": "a", "x": 1, "y": 1, "w": 1, "h": 1, "text": "x"}]}),
                    encoding="utf-8")
    out = tmp_path / "req.json"
    run_cli("build", "DECK123", str(spec), "--out", str(out))
    assert fake.batches == [] and isinstance(json.loads(out.read_text(encoding="utf-8")), list)


def test_outline_json(fake, capsys):
    run_cli("outline", "DECK123", "--slides", "2", "--json")
    d = json.loads(capsys.readouterr().out)
    assert d["slides"][0]["id"] == "s2" and d["revision"] == "rev1"


def test_thumb_writes_png(fake, tmp_path, monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"\x89PNG fake"
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=60: Resp())
    out = tmp_path / "s.png"
    assert run_cli("thumb", "DECK123", "2", "--out", str(out)) == 0
    assert out.read_bytes() == b"\x89PNG fake"
    assert fake.thumb_kw["pageObjectId"] == "s2" and fake.thumb_kw["thumbnailProperties_thumbnailSize"] == "LARGE"


def test_cli_knows_the_service():
    assert cli.SERVICES["slides"] == "gslides" and cli.SERVICES["gslides"] == "gslides"


# ------------------------------------------------------------------ the skill's example spec must build
def _skill_specs():
    text = SKILL.read_text(encoding="utf-8")
    return [b for b in re.findall(r"```ya?ml\n(.*?)```", text, re.S) if re.search(r"^elements:", b, re.M)]


def test_skill_has_an_example_spec():
    assert _skill_specs()


@pytest.mark.parametrize("n", range(len(_skill_specs()) if SKILL.exists() else 0))
def test_skill_example_spec_builds_cleanly(n):
    from ruamel.yaml import YAML
    spec = YAML(typ="safe").load(_skill_specs()[n])
    pres = make_pres()
    pres["slides"] += [_slide(f"pad{i:02d}", "L_blank1", "m1", []) for i in range(30)]
    reqs, warnings, _info = G.build_requests(pres, spec, "example.yaml")
    assert reqs and warnings == [], warnings
