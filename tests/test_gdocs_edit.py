"""Tests of the Google Doc passage edits (`gdoc insert` / `gdoc replace`) and the content-based
drift guard. Nothing here calls Google: the Docs and Drive services are in-memory fakes, and
a small simulator applies the planned requests to a text model so the index arithmetic is
checked, not assumed."""
from __future__ import annotations

import hashlib

import pytest

from bizconnect.connectors import gdocs as G

INDENT = {"indentStart": {"magnitude": 30, "unit": "PT"},
          "indentFirstLine": {"magnitude": 12, "unit": "PT"}}


# ------------------------------------------------------------------ a synthetic document
def P(text, named="NORMAL_TEXT", bullet=False, runs=None, **style):
    """A paragraph spec: runs are (content, textStyle); the default is one plain run."""
    return {"text": text, "named": named, "bullet": bullet, "style": style,
            "runs": runs or [(text, {})]}


def make_doc(specs, table_after=None):
    """A documents.get response (includeTabsContent) for paragraph specs; table_after=i puts a
    one-cell table after paragraph i."""
    content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}]
    idx = 1
    for i, sp in enumerate(specs):
        els, start = [], idx
        runs = list(sp["runs"])
        runs[-1] = (runs[-1][0] + "\n", runs[-1][1])
        for c, st in runs:
            els.append({"startIndex": idx, "endIndex": idx + G._u16(c),
                        "textRun": {"content": c, "textStyle": st}})
            idx += G._u16(c)
        para = {"elements": els,
                "paragraphStyle": {"namedStyleType": sp["named"], **sp["style"]}}
        if sp["bullet"]:
            para["bullet"] = {"listId": "kix.list.1"}
            para["paragraphStyle"].update(INDENT)
        content.append({"startIndex": start, "endIndex": idx, "paragraph": para})
        if table_after == i:
            cell = {"content": [{"startIndex": idx + 3, "endIndex": idx + 9, "paragraph": {
                "elements": [{"textRun": {"content": "Cell A\n", "textStyle": {}}}]}}]}
            content.append({"startIndex": idx, "endIndex": idx + 10,
                            "table": {"rows": 1, "columns": 1,
                                      "tableRows": [{"tableCells": [cell]}]}})
            idx += 10
    return {"documentId": "DOC", "revisionId": "rev-1",
            "tabs": [{"tabProperties": {"tabId": "t.0"},
                      "documentTab": {"body": {"content": content}}}]}


def sample():
    gap = {"spaceBelow": {"magnitude": 11.25, "unit": "PT"}}
    return make_doc([
        P("Title", "HEADING_1"),
        P("Byline", runs=[("Byline", {"italic": True})], **gap),
        P("Summary", "HEADING_2", spaceAbove={"unit": "PT"}),
        P("Lead paragraph", runs=[("Lead paragraph", {"bold": True})]),
        P("Alpha: one", bullet=True, runs=[("Alpha:", {"bold": True}), (" one", {})]),
        P("Beta: two", bullet=True, runs=[("Beta:", {"bold": True}), (" two", {})]),
        P("Closing words", **gap),
        P("Details", "HEADING_2", spaceAbove={"unit": "PT"}),
        P("Choice", "HEADING_3"),
        P("Gamma: three", bullet=True, runs=[("Gamma:", {"bold": True}), (" three", {})]),
    ])


# ------------------------------------------------------------------ request simulator
def simulate(doc, requests):
    """Apply a planned batchUpdate to a text model. Returns the resulting paragraphs, each with
    its text, the request kinds that styled it, its named style and its bold spans."""
    tab = doc["tabs"][0]["documentTab"]
    text = ""
    for el in tab["body"]["content"][1:]:
        if "paragraph" in el:
            text += G._para_text(el["paragraph"])
        else:
            text += "\x00" * (el["endIndex"] - el["startIndex"])   # opaque table
    assert all(ord(c) < 0x10000 for c in text)                   # BMP: one unit per char here
    touched, named, bold = {}, {}, set()

    def para_of(i):                      # paragraph number containing body index i
        return text[: i - 1].count("\n")

    for r in requests:
        (kind, body), = r.items()
        if kind == "insertText":
            i = body["location"]["index"]
            assert body["location"].get("tabId") == "t.0"
            text = text[: i - 1] + body["text"] + text[i - 1:]
            continue
        rng = body.get("range") or {}
        a, z = rng["startIndex"], rng["endIndex"]
        assert 1 <= a < z <= len(text) + 1, (kind, a, z)
        if kind == "updateTextStyle" and body["textStyle"].get("bold"):
            bold.update(range(a, z))
        for n in range(para_of(a), para_of(z - 1) + 1):
            touched.setdefault(n, []).append(kind)
            if kind == "updateParagraphStyle":
                named[n] = body["paragraphStyle"]["namedStyleType"]
    out, pos = [], 1
    for n, line in enumerate(text.split("\n")[:-1]):
        b = "".join(ch if (pos + j) in bold else "_" for j, ch in enumerate(line))
        out.append({"text": line, "touched": touched.get(n, []), "named": named.get(n), "bold": b})
        pos += len(line) + 1
    return out


def texts(paras):
    return [p["text"] for p in paras]


# ------------------------------------------------------------------ Markdown parsing
def test_inline_bold_italic_link_and_escapes():
    plain, spans = G._md_inline(r"**Revenue:** \$5M, P\&L at \+86 *now*, see [the plan](https://x.y/z).")
    assert plain == "Revenue: $5M, P&L at +86 now, see the plan."
    got = {(plain[a:z], tuple(sorted(st))) for a, z, st in spans}
    assert got == {("Revenue:", ("bold",)), ("now", ("italic",)), ("the plan", ("link",))}


def test_inline_offsets_are_utf16():
    plain, spans = G._md_inline("\U0001F600 **x**")
    assert plain == "\U0001F600 x"
    assert spans == [(3, 4, {"bold": True})]        # the emoji is two UTF-16 units


def test_blocks_headings_bullets_quotes_and_continuations():
    md = ("## **The moment**\n\n**Lead.**\n\nFirst line\ncontinues here.\n\n"
          "> * **A:** one  \n> * **B:** two\nwraps\n")
    b = G._md_blocks(md)
    assert [(x["kind"], x["level"], x["text"]) for x in b] == [
        ("heading", 2, "The moment"), ("para", 0, "Lead."),
        ("para", 0, "First line continues here."),
        ("bullet", 0, "A: one"), ("bullet", 0, "B: two wraps")]


@pytest.mark.parametrize("md", ["| a | b |", "1. first", "* top\n  * nested", "```\ncode\n```",
                                "![img](x.png)", "---"])
def test_blocks_refuse_what_they_cannot_place(md):
    with pytest.raises(SystemExit):
        G._md_blocks(md)


# ------------------------------------------------------------------ insert planning
def plan(doc, md, anchor, after):
    tab = doc["tabs"][0]["documentTab"]
    reqs, _p = G._plan_insert(tab, "t.0", G._md_blocks(md), anchor, after)
    return reqs, simulate(doc, reqs)


def test_insert_section_before_a_heading():
    doc = sample()
    reqs, out = plan(doc, "## **The moment**\n\n**Early October.**\n\nAt the offsite.\n\nThe plan.",
                     "Summary", after=False)
    assert texts(out)[:8] == ["Title", "Byline", "The moment", "Early October.", "At the offsite.",
                              "The plan.", "Summary", "Lead paragraph"]
    new = out[2:6]
    assert [p["named"] for p in new] == ["HEADING_2", "NORMAL_TEXT", "NORMAL_TEXT", "NORMAL_TEXT"]
    assert all(not out[i]["touched"] for i in (0, 1, 6, 7))      # neighbours untouched
    assert new[0]["bold"] == "The moment" and new[1]["bold"] == "Early October."
    assert set(new[2]["bold"]) == {"_"}
    # no bullets to add or remove: the anchor is a heading, as are/aren't the new paragraphs
    assert not any(k in r for r in reqs for k in ("createParagraphBullets", "deleteParagraphBullets"))
    # spacing comes from same-kind paragraphs: a paragraph followed by a paragraph or heading gets
    # the gap; the heading copies the explicit zero spaceAbove of its neighbour
    styles = [r["updateParagraphStyle"]["paragraphStyle"] for r in reqs if "updateParagraphStyle" in r]
    assert styles[0]["spaceAbove"] == {"magnitude": 0, "unit": "PT"}
    assert styles[2]["spaceBelow"]["magnitude"] == 11.25


def test_insert_subsection_after_last_bullet_removes_and_keeps_bullets():
    doc = sample()
    md = "### **The paradox**\n\n**Hard to sell.**\n\n* **Why:** inertia\n* **How:** savings"
    reqs, out = plan(doc, md, "Beta: two", after=True)
    assert texts(out)[4:11] == ["Alpha: one", "Beta: two", "The paradox", "Hard to sell.",
                                "Why: inertia", "How: savings", "Closing words"]
    assert [p["named"] for p in out[6:10]] == ["HEADING_3", "NORMAL_TEXT", "NORMAL_TEXT", "NORMAL_TEXT"]
    dels = [r["deleteParagraphBullets"]["range"] for r in reqs if "deleteParagraphBullets" in r]
    assert len(dels) == 1 and not any("createParagraphBullets" in r for r in reqs)
    # the deleted range covers exactly the heading and the lead paragraph
    assert "deleteParagraphBullets" in out[6]["touched"] and "deleteParagraphBullets" in out[7]["touched"]
    assert "deleteParagraphBullets" not in out[8]["touched"]
    assert not out[4]["touched"] and not out[10]["touched"]
    assert out[8]["bold"].startswith("Why:_")
    # the new bullets take the Doc's own list indent from a neighbouring bullet
    bstyle = [r["updateParagraphStyle"]["paragraphStyle"] for r in reqs if "updateParagraphStyle" in r][2]
    assert bstyle["indentStart"]["magnitude"] == 30


def test_insert_bullets_before_a_paragraph_creates_a_list():
    doc = sample()
    reqs, out = plan(doc, "* one\n* two", "Closing words", after=False)
    assert texts(out)[6:9] == ["one", "two", "Closing words"]
    made = [r for r in reqs if "createParagraphBullets" in r]
    assert len(made) == 1 and "createParagraphBullets" in out[7]["touched"]
    assert not out[8]["touched"]


def test_insert_paragraph_between_bullets_and_template_by_context():
    doc = sample()
    reqs, out = plan(doc, "**Lens.**\n\n* new point", "Beta: two", after=False)
    assert texts(out)[4:8] == ["Alpha: one", "Lens.", "new point", "Beta: two"]
    para = [r["updateParagraphStyle"]["paragraphStyle"] for r in reqs if "updateParagraphStyle" in r][0]
    # followed by a list, so it copies a paragraph that sits flush against a list: no gap
    assert "spaceBelow" not in para and "indentStart" not in para


def test_insert_last_paragraph_and_first_paragraph():
    doc = sample()
    _r, out = plan(doc, "* tail", "Gamma: three", after=True)
    assert texts(out)[-2:] == ["Gamma: three", "tail"]
    _r, out = plan(doc, "**DEPRECATED**", "Title", after=False)
    assert texts(out)[:2] == ["DEPRECATED", "Title"] and out[0]["bold"] == "DEPRECATED"
    assert out[0]["named"] == "NORMAL_TEXT"


def test_insert_after_paragraph_before_table():
    doc = make_doc([P("Intro"), P("After")], table_after=0)
    reqs, out = plan(doc, "New one.", "Intro", after=True)
    assert texts(out)[:2] == ["Intro", "New one."]


def test_insert_refuses_ambiguous_missing_and_duplicate():
    doc = sample()
    tab = doc["tabs"][0]["documentTab"]
    with pytest.raises(SystemExit, match="3 paragraphs"):
        G._plan_insert(tab, None, G._md_blocks("x"), "a: ", after=True)    # Alpha:, Beta:, Gamma:
    with pytest.raises(SystemExit, match="0 paragraphs"):
        G._plan_insert(tab, None, G._md_blocks("x"), "nowhere", after=True)
    with pytest.raises(SystemExit, match="already in the Doc"):
        G._plan_insert(tab, None, G._md_blocks("* **Beta:** two"), "Alpha", after=True)


def test_anchor_matching_ignores_smart_punctuation_and_case():
    doc = make_doc([P("The “right speed” plan — now"), P("Other")])
    tab = doc["tabs"][0]["documentTab"]
    _reqs, p = G._plan_insert(tab, None, G._md_blocks("x"), 'the "right speed" plan - now', after=False)
    assert p["text"].startswith("The “right")


# ------------------------------------------------------------------ the verbs, against fakes
class FakeDocs:
    def __init__(self, doc, fail=None):
        self.doc, self.fail, self.sent = doc, fail, []

    def documents(self):
        return self

    def get(self, documentId, includeTabsContent=False):
        assert includeTabsContent
        return _Exec(lambda: self.doc)

    def batchUpdate(self, documentId, body):
        def run():
            if self.fail:
                raise self.fail
            self.sent.append(body)
            return {"replies": [{}], "writeControl": {"requiredRevisionId": "rev-2"}}
        return _Exec(run)


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


URL = "https://docs.google.com/document/d/DOC1234567890abcdefghijklmnop/edit"


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocs(make_doc([
        P("Intro"), P("Org", runs=[("Few, exceptional hires; progressive automation", {})]),
        P("Hire ahead of need"), P("Sep 2026: Muse launched; it’s big")]))
    monkeypatch.setattr(G, "_docs", lambda data=None: f)
    return f


def test_cmd_insert_sends_one_guarded_batch(fake, tmp_path, capsys):
    snippet = tmp_path / "s.md"
    snippet.write_text("## New\n\nBody.", encoding="utf-8")
    G.cmd_insert([URL, "--after", "Intro", "--from", str(snippet)])
    (body,) = fake.sent
    assert body["writeControl"] == {"requiredRevisionId": "rev-1"}
    assert body["requests"][0]["insertText"]["text"] == "\nNew\nBody."
    assert "inserted 2 paragraph(s) after 'Intro'" in capsys.readouterr().out


def test_cmd_insert_dry_run_sends_nothing(fake, capsys):
    G.cmd_insert([URL, "--before", "Intro", "--md", "Hello.", "--dry-run"])
    assert fake.sent == [] and "requiredRevisionId" in capsys.readouterr().out


def test_cmd_insert_needs_one_anchor_and_one_source(fake):
    for argv in ([URL, "--md", "x"], [URL, "--before", "a", "--after", "b", "--md", "x"],
                 [URL, "--before", "Intro"]):
        with pytest.raises(SystemExit):
            G.cmd_insert(argv)
    assert fake.sent == []


def test_cmd_replace_unique_text(fake):
    G.cmd_replace([URL, "Few, exceptional hires", "Senior ICs and \"stem cell\" hires"])
    (body,) = fake.sent
    r = body["requests"][0]["replaceAllText"]
    assert r["containsText"] == {"text": "Few, exceptional hires", "matchCase": True}
    assert r["tabsCriteria"] == {"tabIds": ["t.0"]}


def test_cmd_replace_refuses_reapply_and_ambiguity(fake, monkeypatch):
    with pytest.raises(SystemExit, match="already in the Doc"):
        G.cmd_replace([URL, "Hire ahead of need", "Hire ahead of need"])
    with pytest.raises(SystemExit, match="occurs 0 times"):
        G.cmd_replace([URL, "absent text", "whatever"])
    with pytest.raises(SystemExit, match="different characters"):
        G.cmd_replace([URL, "it's big", "it's huge"])            # the Doc has a curly apostrophe
    fake.doc = make_doc([P("dup here"), P("dup here too")])
    with pytest.raises(SystemExit, match="occurs 2 times"):
        G.cmd_replace([URL, "dup here", "x"])
    fake.doc = make_doc([P("Hire ahead of need; more managers")])
    with pytest.raises(SystemExit, match="already in the Doc"):
        G.cmd_replace([URL, "Hire ahead of need", "Hire ahead of need; more managers"])
    assert fake.sent == []


def test_revision_mismatch_writes_nothing(fake, monkeypatch):
    class HttpErr(Exception):
        pass
    fake.fail = HttpErr()
    monkeypatch.setattr(G._google, "http_error",
                        lambda e: (400, "The required revision ID does not match the latest"))
    with pytest.raises(SystemExit, match="REVISION MISMATCH"):
        G.cmd_replace([URL, "Intro", "Introduction"])


# ------------------------------------------------------------------ drift guard
class FakeDrive:
    def __init__(self, modified, content):
        self.modified, self.content, self.exports = modified, content, 0

    def files(self):
        return self

    def get(self, fileId, fields, supportsAllDrives=True):
        return _Exec(lambda: {"modifiedTime": self.modified})

    def export(self, fileId, mimeType):
        def run():
            self.exports += 1
            return self.content
        return _Exec(run)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_guard_same_time_needs_no_export():
    d = FakeDrive("T1", b"x")
    assert not G._doc_changed(d, "D", {"synced_modified": "T1", "synced_doc_sha256": sha(b"x")})
    assert d.exports == 0


def test_guard_time_moved_but_content_same_is_not_a_change():
    d = FakeDrive("T0-earlier", b"body")
    assert not G._doc_changed(d, "D", {"synced_modified": "T1", "synced_doc_sha256": sha(b"body")})


def test_guard_content_moved_is_a_change():
    d = FakeDrive("T2", b"edited")
    assert G._doc_changed(d, "D", {"synced_modified": "T1", "synced_doc_sha256": sha(b"body")})


def test_guard_legacy_state_uses_the_pulled_file_as_baseline():
    st = {"synced_modified": "T1", "last_pull_sha256": sha(b"body"), "last_pull_at": "2026-01-02"}
    assert not G._doc_changed(FakeDrive("T0", b"body"), "D", st)
    st_pushed_since = {**st, "last_push_at": "2026-01-03"}        # pull hash is stale after a push
    assert G._doc_changed(FakeDrive("T0", b"body"), "D", st_pushed_since)


def test_guard_without_a_baseline_time_does_not_block():
    assert not G._doc_changed(FakeDrive("T9", b"x"), "D", {})
