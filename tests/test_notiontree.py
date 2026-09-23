"""End-to-end tests of the mapped Notion sync (bizconnect.connectors.notiontree) against an
in-memory fake of the Notion API (tests/fake_notion.py)."""
from __future__ import annotations

import textwrap

import pytest

from bizconnect.connectors import notion as N
from bizconnect.connectors import notiontree as T
from tests.fake_notion import FakeNotion


def H(level, text):
    return {"type": "heading_%d" % level, "heading_%d" % level: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def P(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def B(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(N, "api", fake.api)
    monkeypatch.setattr(N, "upload_file", lambda path: fake.api("POST", "/file_uploads",
                                                               body={"filename": path.name})[1]["id"])
    monkeypatch.setattr(N, "_fetch_bytes", lambda url, timeout=90: fake.files[url.split("?")[0]])
    (tmp_path / "connections.yaml").write_text("notion: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # a hub shaped like a hand-authored project page
    hub = fake.new_page("Project X", blocks=[
        H(1, "What does Josh think?"), P("[populate from agentic research]"),
        H(1, "What does Nous have?"), B("Tent poles"), B("Connectors"),
        H(1, "What do we know?"),
    ])
    sub = fake.new_page("Behavioural insights", parent=hub, blocks=[H(1, "WhatsApp"), B("66% connect")])
    fake._insert(hub, [H(1, "Background")])
    up = fake.api("POST", "/file_uploads", body={"filename": "brief.docx"})[1]["id"]
    fake._insert(hub, [{"type": "file", "file": {"type": "file_upload", "file_upload": {"id": up}}}])
    proj = tmp_path / "proj"
    proj.mkdir()
    return {"fake": fake, "hub": hub, "sub": sub, "root": tmp_path, "proj": proj}


def manifest(env, body):
    (env["proj"] / "notion.yaml").write_text("hub: %s\n%s" % (env["hub"], textwrap.dedent(body)), encoding="utf-8")


def tree(env, **kw):
    return T.Tree(env["root"], env["proj"] / "notion.yaml", out=lambda *a: None, **kw)


def run(env, verb, scope=None, **kw):
    t = tree(env, **kw)
    getattr(t, verb)(scope)
    t.save()
    return {k: v for v, k, _m in t.results if v != "in-sync"}, t


MAP = """\
map:
  - path: josh.md
    section: What does Josh think?
  - path: nous.md
    section: What does Nous have?
  - path: insights.md
    page: %s
"""


def test_pull_then_status_clean(env):
    manifest(env, MAP % env["sub"])
    res, _ = run(env, "pull")
    assert res == {"josh.md": "pulled", "nous.md": "pulled", "insights.md": "pulled"}
    assert (env["proj"] / "josh.md").read_text(encoding="utf-8") == \
        "# What does Josh think?\n\n[populate from agentic research]\n"
    assert (env["proj"] / "nous.md").read_text(encoding="utf-8") == \
        "# What does Nous have?\n\n- Tent poles\n- Connectors\n"
    ins = (env["proj"] / "insights.md").read_text(encoding="utf-8")
    assert ins.startswith("# Behavioural insights\n\n# WhatsApp\n\n- 66% connect")
    res, _ = run(env, "status")
    assert res == {}
    # ids pinned in the mapping file
    txt = (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    assert txt.count("id: ") == 2


def test_push_replaces_only_its_section(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    fake, hub = env["fake"], env["hub"]
    before = fake.ids_of(hub)
    (env["proj"] / "josh.md").write_text(
        "# What does Josh think?\n\nHe wants **network effects**.\n\n## Memory\n\n- judgment over recall\n",
        encoding="utf-8")
    res, _ = run(env, "status")
    assert res == {"josh.md": "local-ahead"}
    res, _ = run(env, "push")
    assert res == {"josh.md": "pushed"}
    texts = fake.texts(hub)
    assert texts == [
        ("heading_1", "What does Josh think?"), ("paragraph", "He wants network effects."),
        ("heading_2", "Memory"), ("bulleted_list_item", "judgment over recall"),
        ("heading_1", "What does Nous have?"), ("bulleted_list_item", "Tent poles"),
        ("bulleted_list_item", "Connectors"), ("heading_1", "What do we know?"),
        ("child_page", "Behavioural insights"), ("heading_1", "Background"), ("file", "")]
    after = fake.ids_of(hub)
    # every block outside the section kept its identity (comments survive)
    assert set(before) - {before[1]} <= set(after)
    res, _ = run(env, "status")
    assert res == {}


def test_minimal_diff_keeps_unchanged_blocks(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    fake, hub = env["fake"], env["hub"]
    nous_ids = fake.ids_of(hub)[3:5]
    (env["proj"] / "nous.md").write_text("# What does Nous have?\n\n- Tent poles\n- Regulatory approvals\n- Connectors\n",
                                         encoding="utf-8")
    run(env, "push")
    ids = fake.ids_of(hub)
    assert nous_ids[0] in ids and nous_ids[1] in ids          # untouched bullets kept
    assert [t for t in fake.texts(hub)][3:6] == [("bulleted_list_item", "Tent poles"),
                                                 ("bulleted_list_item", "Regulatory approvals"),
                                                 ("bulleted_list_item", "Connectors")]


def test_notion_edit_guards_push_and_pull_takes_it(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    fake, hub = env["fake"], env["hub"]
    fake.edit_text(fake.ids_of(hub)[3], "Tent poles (energy, broadband)")
    res, _ = run(env, "status")
    assert res == {"nous.md": "remote-ahead"}
    res, _ = run(env, "push")
    assert res == {"nous.md": "remote-ahead"}                 # refused
    assert fake.texts(hub)[3] == ("bulleted_list_item", "Tent poles (energy, broadband)")
    res, _ = run(env, "pull")
    assert res == {"nous.md": "pulled"}
    assert "- Tent poles (energy, broadband)" in (env["proj"] / "nous.md").read_text(encoding="utf-8")


def test_conflict_needs_force(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    fake, hub = env["fake"], env["hub"]
    fake.edit_text(fake.ids_of(hub)[3], "Remote edit")
    (env["proj"] / "nous.md").write_text("# What does Nous have?\n\n- Local edit\n", encoding="utf-8")
    assert run(env, "status")[0] == {"nous.md": "conflict"}
    assert run(env, "push")[0] == {"nous.md": "conflict"}
    assert run(env, "pull")[0] == {"nous.md": "conflict"}
    assert (env["proj"] / "nous.md").read_text(encoding="utf-8").endswith("- Local edit\n")
    assert run(env, "push", force=True)[0] == {"nous.md": "pushed"}
    assert fake.texts(hub)[3] == ("bulleted_list_item", "Local edit")


def test_first_push_onto_hand_written_section_is_guarded(env):
    manifest(env, MAP % env["sub"])
    (env["proj"] / "nous.md").write_text("# What does Nous have?\n\n- Something else\n", encoding="utf-8")
    res, _ = run(env, "push", scope="nous.md")
    assert res == {"nous.md": "conflict"}
    assert env["fake"].texts(env["hub"])[3] == ("bulleted_list_item", "Tent poles")


def test_section_follows_renamed_heading(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    fake, hub = env["fake"], env["hub"]
    fake.edit_text(fake.ids_of(hub)[0], "What does Josh care about?")
    (env["proj"] / "josh.md").write_text("# x\n\nNew view.\n", encoding="utf-8")
    assert run(env, "push")[0] == {"josh.md": "pushed"}
    assert fake.texts(hub)[:2] == [("heading_1", "What does Josh care about?"), ("paragraph", "New view.")]


def test_links_between_mapped_files(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    (env["proj"] / "josh.md").write_text("# J\n\nSee [the insights](insights.md) and [Nous](nous.md).\n",
                                         encoding="utf-8")
    run(env, "push")
    fake, hub = env["fake"], env["hub"]
    rich = fake.blocks[fake.ids_of(hub)[1]]["paragraph"]["rich_text"]
    hrefs = [r["href"] for r in rich if r.get("href")]
    assert hrefs[0] == "https://www.notion.so/" + env["sub"].replace("-", "")
    assert hrefs[1].startswith("https://www.notion.so/" + hub.replace("-", "") + "#")
    assert run(env, "status")[0] == {}
    fake.edit_text(fake.ids_of(hub)[3], "Tent poles!")          # force a pull of josh? no: of nous
    run(env, "pull")
    # re-pull josh after a remote change keeps relative links
    blk = fake.blocks[fake.ids_of(hub)[1]]
    blk["paragraph"]["rich_text"].append({"type": "text", "text": {"content": " More."}, "plain_text": " More.",
                                          "annotations": {}, "href": None})
    fake._touch(blk["id"])
    run(env, "pull")
    assert "[the insights](insights.md)" in (env["proj"] / "josh.md").read_text(encoding="utf-8")


LOGMAP = """\
map:
  - path: log
    database: new
    title: Public log
    schema:
      date: date
      verification: select
      themes: multi_select
      url: {type: url, name: URL}
"""


def _row(env, name, text):
    d = env["proj"] / "log"
    d.mkdir(exist_ok=True)
    (d / name).write_text(textwrap.dedent(text), encoding="utf-8")


def test_database_rows_roundtrip(env):
    manifest(env, LOGMAP)
    _row(env, "E-20260907-moats.md", """\
        ---
        title: On moats
        date: 2026-09-07
        verification: verified-primary
        themes: [moats, network effects]
        url: https://x.com/joshelman/status/1
        ---
        > "Most consumer AI is single-player."
        """)
    res, t = run(env, "push")
    assert res == {"log/": "created", "log/E-20260907-moats.md": "pushed"}
    fake = env["fake"]
    did = [d for d in fake.dbs][0]
    db = fake.dbs[did]
    assert set(db["properties"]) >= {"Title", "Key", "Date", "Verification", "Themes", "URL"}
    row = [p for p in fake.pages.values() if p["parent"].get("database_id") == did][0]
    assert row["properties"]["Key"]["rich_text"][0]["plain_text"] == "E-20260907-moats"
    assert row["properties"]["Themes"]["multi_select"] == [{"name": "moats"}, {"name": "network effects"}]
    assert fake.texts(row["id"]) == [("quote", '"Most consumer AI is single-player."')]
    assert run(env, "status")[0] == {}
    # the database landed on the hub page; nothing else there changed
    assert ("child_database", "Public log") in fake.texts(env["hub"])
    # a Notion-side property edit is pulled into the front-matter
    row["properties"]["Verification"] = {"type": "select", "select": {"name": "secondary"}}
    row["last_edited_time"] = fake._now()
    assert run(env, "status")[0] == {"log/E-20260907-moats.md": "remote-ahead"}
    run(env, "pull")
    txt = (env["proj"] / "log" / "E-20260907-moats.md").read_text(encoding="utf-8")
    assert "verification: secondary" in txt and "date: 2026-09-07" in txt
    assert run(env, "status")[0] == {}


def test_row_added_in_notion_is_pulled_with_a_key(env):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n")
    run(env, "push")
    fake = env["fake"]
    did = [d for d in fake.dbs][0]
    fake.api("POST", "/pages", body={"parent": {"database_id": did}, "properties": {
        "Title": {"title": [{"type": "text", "text": {"content": "New from Notion"}}]}},
        "children": [P("typed in Notion")]})
    res, _ = run(env, "pull")
    assert res == {"log/new-from-notion.md": "pulled"}
    txt = (env["proj"] / "log" / "new-from-notion.md").read_text(encoding="utf-8")
    assert txt == "---\ntitle: New from Notion\n---\n\ntyped in Notion\n"
    assert run(env, "status")[0] == {}


def test_deleted_row_needs_prune(env):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n")
    _row(env, "b.md", "---\ntitle: B\n---\n")
    run(env, "push")
    (env["proj"] / "log" / "b.md").unlink()
    assert run(env, "push")[0] == {"log/b.md": "local-deleted"}
    assert run(env, "push", prune=True)[0] == {"log/b.md": "archived"}
    fake = env["fake"]
    live = [p for p in fake.pages.values() if p["parent"].get("database_id") and not p["archived"]]
    assert len(live) == 1


def test_new_page_created_and_dry_run_writes_nothing(env):
    manifest(env, """\
        map:
          - path: notes/muse.md
            page: new
    """)
    (env["proj"] / "notes").mkdir()
    (env["proj"] / "notes" / "muse.md").write_text("# Muse\n\nEvidence.\n", encoding="utf-8")
    before = (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    res, _ = run(env, "push", dry=True)
    assert res == {"notes/muse.md": "would-push"}
    assert (env["proj"] / "notion.yaml").read_text(encoding="utf-8") == before
    assert ("child_page", "Muse") not in env["fake"].texts(env["hub"])
    res, _ = run(env, "push")
    assert res == {"notes/muse.md": "pushed"}
    assert ("child_page", "Muse") in env["fake"].texts(env["hub"])
    assert "id: " in (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    # a second machine (no local state) sees it as in sync, not as a conflict
    (env["root"] / ".bizconnect" / "state.json").unlink()
    assert run(env, "status")[0] == {}


def test_images_pulled_and_repushed_without_churn(env):
    fake = env["fake"]
    up = fake.api("POST", "/file_uploads", body={"filename": "image.png"})[1]["id"]
    up2 = fake.api("POST", "/file_uploads", body={"filename": "image.png"})[1]["id"]
    fake._insert(env["sub"], [{"type": "image", "image": {"type": "file_upload", "file_upload": {"id": up}}},
                              {"type": "image", "image": {"type": "file_upload", "file_upload": {"id": up2}}}])
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    txt = (env["proj"] / "insights.md").read_text(encoding="utf-8")
    assert "![](media/insights/image.png)" in txt and "![](media/insights/image-2.png)" in txt
    assert (env["proj"] / "media" / "insights" / "image-2.png").exists()
    assert run(env, "status")[0] == {}
    ids = fake.ids_of(env["sub"])
    (env["proj"] / "insights.md").write_text(txt + "\nA new closing line.\n", encoding="utf-8")
    assert run(env, "push")[0] == {"insights.md": "pushed"}
    assert set(ids) <= set(fake.ids_of(env["sub"]))           # images not re-uploaded
    # a brand-new local image is uploaded, then recognised on the next status
    (env["proj"] / "chart.png").write_bytes(b"png")
    (env["proj"] / "insights.md").write_text(txt + "\n![Chart](chart.png)\n", encoding="utf-8")
    assert run(env, "push")[0] == {"insights.md": "pushed"}
    assert run(env, "status")[0] == {}


def test_files_entry_uploads_into_section(env):
    manifest(env, """\
        map:
          - path: context/*.pdf
            section: Background
    """)
    (env["proj"] / "context").mkdir()
    (env["proj"] / "context" / "a.pdf").write_bytes(b"%PDF")
    assert run(env, "push")[0] == {"context/a.pdf": "pushed"}
    t = env["fake"].texts(env["hub"])
    assert t[-3:] == [("heading_1", "Background"), ("file", ""), ("pdf", "")]   # appended at the section end
    assert run(env, "push")[0] == {}
    assert run(env, "status")[0] == {}


def test_cli_status_exit_codes(env, capsys):
    manifest(env, MAP % env["sub"])
    assert T._run("pull", [str(env["proj"])]) == 0
    assert T._run("status", []) == 0
    env["fake"].edit_text(env["fake"].ids_of(env["hub"])[3], "changed")
    (env["proj"] / "nous.md").write_text("# What does Nous have?\n\n- local\n", encoding="utf-8")
    assert T._run("push", [str(env["proj"] / "nous.md")]) == 1
    out = capsys.readouterr().out
    assert "conflict" in out and "nous.md" in out


def test_whole_page_and_its_sections_cannot_both_be_mapped(env, capsys):
    manifest(env, """\
        map:
          - path: hub.md
            page: %s
          - path: josh.md
            section: What does Josh think?
    """ % env["hub"])
    assert T._run("status", [str(env["proj"])]) == 1
    assert "map the page whole OR by sections" in capsys.readouterr().out


def test_link_base_covers_links_out_of_the_folder(env):
    (env["proj"] / "notion.yaml").write_text(
        "hub: %s\nlink_base: https://github.com/o/r/blob/main/proj/\nmap:\n  - path: josh.md\n    section: What does Josh think?\n"
        % env["hub"], encoding="utf-8")
    t = tree(env)
    assert t.resolve("../CLAUDE.md", "josh.md") == "https://github.com/o/r/blob/main/CLAUDE.md"
    assert t.resolve("context/a b.pdf", "josh.md") == "https://github.com/o/r/blob/main/proj/context/a%20b.pdf"
    assert t.unresolve("https://github.com/o/r/blob/main/proj/context/a%20b.pdf", "josh.md") == "context/a b.pdf"
    assert t.resolve("#anchor", "josh.md") is None


def test_folder_of_pages(env):
    manifest(env, """\
        map:
          - path: research
            pages: new
            title: Competitor research
    """)
    d = env["proj"] / "research"
    d.mkdir()
    (d / "muse.md").write_text("# Muse: evidence\n\n*Private, a label:* \"verbatim … quote\"\n", encoding="utf-8")
    (d / "instinct.md").write_text("# Instinct\n\nNotes. See [Muse](muse.md).\n", encoding="utf-8")
    res, _ = run(env, "push")
    assert res == {"research/": "created", "research/muse.md": "pushed", "research/instinct.md": "pushed"}
    fake = env["fake"]
    cont = [b for b in fake.ids_of(env["hub"]) if fake.blocks[b]["type"] == "child_page"
            and fake.texts(env["hub"])[fake.ids_of(env["hub"]).index(b)][1] == "Competitor research"][0]
    assert [t for t in fake.texts(cont)] == [("child_page", "Instinct"), ("child_page", "Muse: evidence")]
    assert run(env, "status")[0] == {}
    # an agent drops a new note in the folder: the next push publishes it, no mapping step
    (d / "chatgpt.md").write_text("# ChatGPT\n\nCompared.\n", encoding="utf-8")
    assert run(env, "push")[0] == {"research/chatgpt.md": "pushed"}
    # a page added in Notion under the folder page arrives as a file
    fake.new_page("Typed in Notion", parent=cont, blocks=[P("hello")])
    assert run(env, "pull")[0] == {"research/typed-in-notion.md": "pulled"}
    assert (d / "typed-in-notion.md").read_text(encoding="utf-8") == "# Typed in Notion\n\nhello\n"
    # the italic label and the verbatim quote round-trip unchanged
    muse_id = [x for x in fake.ids_of(cont) if fake.texts(cont)[fake.ids_of(cont).index(x)][1] == "Muse: evidence"][0]
    fake.blocks[fake.ids_of(muse_id)[0]]["paragraph"]["rich_text"].append(
        {"type": "text", "text": {"content": " (checked)"}, "plain_text": " (checked)", "annotations": {}, "href": None})
    fake._touch(fake.ids_of(muse_id)[0])
    run(env, "pull")
    assert (d / "muse.md").read_text(encoding="utf-8") == \
        "# Muse: evidence\n\n*Private, a label:* \"verbatim … quote\" (checked)\n"
    # a note deleted locally stays in Notion unless pruned
    (d / "instinct.md").unlink()
    assert run(env, "push")[0] == {"research/instinct.md": "local-deleted"}
    assert run(env, "push", prune=True)[0] == {"research/instinct.md": "archived"}
    assert ("child_page", "Instinct") not in fake.texts(cont)
    assert "instinct.md" not in (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    assert run(env, "status")[0] == {}


def test_scoped_push_of_one_note_creates_its_folder_page(env):
    manifest(env, "map:\n  - path: research\n    pages: new\n")
    (env["proj"] / "research").mkdir()
    (env["proj"] / "research" / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
    (env["proj"] / "research" / "b.md").write_text("# B\n\ny\n", encoding="utf-8")
    res, _ = run(env, "push", scope="research/a.md")
    assert res == {"research/": "created", "research/a.md": "pushed"}


def test_save_merges_a_concurrent_manifest_edit(env):
    """Another session's `map` lands between our load and save: its entry survives, our ids land."""
    manifest(env, MAP % env["sub"])
    t = tree(env)
    m = env["proj"] / "notion.yaml"
    m.write_text(m.read_text(encoding="utf-8") + "  - path: who.md\n    section: What do we know?\n", encoding="utf-8")
    t.pull()
    t.save()
    txt = m.read_text(encoding="utf-8")
    assert "path: who.md" in txt                      # theirs kept
    assert txt.count("id: ") == 2                     # ours (josh.md, nous.md heading ids) written
    res, _ = run(env, "status")
    assert res == {"who.md": "new-remote"}


def test_save_merges_concurrent_state(env):
    manifest(env, MAP % env["sub"])
    a, b = tree(env), tree(env)                       # two sessions load the same state
    a.pull("josh.md")
    b.pull("nous.md")
    a.save()
    b.save()                                          # must not drop a's record of josh.md
    res, _ = run(env, "status")
    assert res == {"insights.md": "new-remote"}


def test_push_reports_what_it_changed(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    (env["proj"] / "josh.md").write_text("# What does Josh think?\n\n**Lead line.**\n\n- one\n- two\n",
                                         encoding="utf-8")
    t = tree(env)
    t.push()
    msg = next(m for v, k, m in t.results if k == "josh.md")
    assert "3 block(s) added, 1 removed, 0 unchanged" in msg and "under 'What does Josh think?'" in msg


def test_push_warns_on_long_paragraphs(env):
    manifest(env, "style: {max_para_words: 10}\n" + MAP % env["sub"])
    run(env, "pull")
    (env["proj"] / "josh.md").write_text("# What does Josh think?\n\n" + "word " * 30 + "\n\n- short bullet\n",
                                         encoding="utf-8")
    said = []
    t = T.Tree(env["root"], env["proj"] / "notion.yaml", out=said.append, dry=True)
    t.push()
    assert any("paragraph 1 is 30 words (house style max 10)" in s for s in said)


def test_locate_maps_and_pulls_an_unmapped_section(env, capsys):
    manifest(env, "map: []\n")
    fake, hub = env["fake"], env["hub"]
    bid = fake.ids_of(hub)[1]                                          # "[populate from agentic research]"
    link = "https://www.notion.so/Project-X-%s#%s" % (hub.replace("-", ""), bid.replace("-", ""))
    assert T.cmd_locate([link]) == 0
    out = capsys.readouterr().out
    assert "placeholder" in out and "# What does Josh think?" in out and "(only this one)" in out
    assert "mapped   no" in out
    assert T.cmd_locate([link, "--map", str(env["proj"] / "josh.md")]) == 0
    assert (env["proj"] / "josh.md").read_text(encoding="utf-8") == \
        "# What does Josh think?\n\n[populate from agentic research]\n"
    capsys.readouterr()
    assert T.cmd_locate([link]) == 0
    assert "mapped   proj/josh.md" in capsys.readouterr().out
    # the placeholder was pulled, so replacing it is an ordinary push — no --force
    (env["proj"] / "josh.md").write_text("# What does Josh think?\n\nFilled.\n", encoding="utf-8")
    res, _ = run(env, "push")
    assert res == {"josh.md": "pushed"}
    assert fake.texts(hub)[:3] == [("heading_1", "What does Josh think?"), ("paragraph", "Filled."),
                                   ("heading_1", "What does Nous have?")]
