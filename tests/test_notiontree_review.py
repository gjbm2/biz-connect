"""Regressions from the v0.13.0 pre-release review: the committed `synced` baseline, database
rows, `map`/`locate`/`diff` and the guard messages. Runs against the in-memory fake Notion via
test_notiontree's fixture."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from bizconnect.connectors import notion as N
from bizconnect.connectors import notiontree as T
from tests.test_notiontree import H, LOGMAP, MAP, P, _row, env, manifest, run, tree  # noqa: F401 (fixture)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=str(root),
                          capture_output=True, text=True)


def _commit(env, msg="sync"):
    if not (env["root"] / ".git").exists():
        _git(env["root"], "init", "-q")
    _git(env["root"], "add", "-A")
    _git(env["root"], "commit", "-qm", msg)


def _new_machine(env):
    st = env["root"] / ".bizconnect" / "state.json"
    if st.exists():
        st.unlink()


def _msgs(t, key):
    return [(v, m) for v, k, m in t.results if k == key]


def _rows(env):
    did = list(env["fake"].dbs)[0]
    return [pg for pg in env["fake"].pages.values() if pg["parent"].get("database_id") == did and not pg["archived"]]


def _key(pg):
    return "".join(r["plain_text"] for r in pg["properties"]["Key"]["rich_text"])


def _set_key(env, pid, key):
    env["fake"].api("PATCH", "/pages/%s" % pid, body={"properties": {"Key": {"rich_text": [
        {"type": "text", "text": {"content": key}}]}}})


# ------------------------------------------------------------------ the committed baseline
@needs_git
def test_a_refused_first_sync_stays_refused(env):
    """A no-baseline refusal used to write an id that turned the next run into 'remote-ahead',
    so a second pull overwrote the authored file."""
    manifest(env, MAP % env["sub"])
    josh = env["proj"] / "josh.md"
    josh.write_text("# J\n\nauthored locally\n", encoding="utf-8")
    _commit(env)
    for verb in ("pull", "pull", "push", "status", "pull"):
        res, _ = run(env, verb)
        assert res.get("josh.md") == "no-baseline", (verb, res)
    assert "authored locally" in josh.read_text(encoding="utf-8")


@needs_git
def test_committed_but_unpushed_edit_is_local_ahead_on_another_machine(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    _commit(env)
    nous = env["proj"] / "nous.md"
    nous.write_text(nous.read_text(encoding="utf-8") + "- Offline mode\n", encoding="utf-8")
    _commit(env, "edit, never pushed")
    _new_machine(env)
    assert run(env, "status")[0] == {"nous.md": "local-ahead"}
    assert run(env, "pull")[0] == {"nous.md": "local-ahead"}
    assert "Offline mode" in nous.read_text(encoding="utf-8")
    assert run(env, "push")[0] == {"nous.md": "pushed"}
    assert ("bulleted_list_item", "Offline mode") in env["fake"].texts(env["hub"])


def test_synced_fingerprints_are_written_and_status_writes_nothing(env):
    manifest(env, MAP % env["sub"] + LOGMAP.replace("map:\n", ""))
    _row(env, "a.md", "---\ntitle: A\n---\n\nAlpha.\n")
    run(env, "pull")
    run(env, "push")
    t = tree(env)
    for key in ("josh.md", "nous.md", "insights.md", "log/a.md"):
        assert t.baseline(t.items[key]), key
    before = (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    assert T._run("status", [str(env["proj"])]) == 0
    assert (env["proj"] / "notion.yaml").read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ database rows
@pytest.mark.parametrize("new_machine", [False, True])
def test_row_created_but_body_failed_is_not_emptied_by_pull(env, monkeypatch, new_machine):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n\nAlpha body written locally.\n")
    real = N.append_children

    def boom(*a, **k):
        raise SystemExit("append blocks failed [502]: bad gateway")
    monkeypatch.setattr(N, "append_children", boom)
    res, _ = run(env, "push")
    assert res["log/a.md"] == "error"
    monkeypatch.setattr(N, "append_children", real)
    if new_machine:
        _new_machine(env)
    assert run(env, "status")[0] == {"log/a.md": "local-ahead"}
    assert run(env, "pull")[0] == {"log/a.md": "local-ahead"}
    assert "Alpha body written locally." in (env["proj"] / "log" / "a.md").read_text(encoding="utf-8")
    assert run(env, "push")[0] == {"log/a.md": "pushed"}
    assert run(env, "status")[0] == {}


def test_row_file_names_with_punctuation_round_trip(env):
    manifest(env, LOGMAP)
    for name in ("Q&A.md", "notes (old).md", "O'Brien, J.md"):
        _row(env, name, "---\ntitle: %s\n---\n\nbody\n" % name[:-3])
    res, _ = run(env, "push")
    assert {k: v for k, v in res.items() if k != "log/"} == {
        "log/Q&A.md": "pushed", "log/notes (old).md": "pushed", "log/O'Brien, J.md": "pushed"}
    assert run(env, "status")[0] == {}
    assert run(env, "pull")[0] == {}
    assert sorted(p.name for p in (env["proj"] / "log").iterdir()) == ["O'Brien, J.md", "Q&A.md", "notes (old).md"]
    _row(env, ".hidden.md", "---\ntitle: H\n---\n")
    res, t = run(env, "push")
    assert res == {"log/.hidden.md": "error"}
    assert "rename .hidden.md" in _msgs(t, "log/.hidden.md")[0][1]
    assert len(_rows(env)) == 3


@needs_git
def test_unreadable_row_is_never_taken_for_deleted(env):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n\nOriginal body.\n")
    _row(env, "b.md", "---\ntitle: B\n---\n")
    run(env, "push")
    _commit(env)
    a = env["proj"] / "log" / "a.md"
    bad = "---\ntitle: Caf\xe9\n---\n\nnew local work\n".encode("cp1252")
    a.write_bytes(bad)
    res, t = run(env, "push", prune=True)
    assert res == {"log/a.md": "error"}
    assert len(_rows(env)) == 2                                      # nothing archived
    assert run(env, "status")[0] == {"log/a.md": "error"}
    _new_machine(env)
    assert run(env, "pull", force=True)[0] == {"log/a.md": "error"}
    assert a.read_bytes() == bad                                     # never overwritten
    a.write_text("---\ntitle: Meeting: Josh\n---\n", encoding="utf-8")   # broken front-matter
    assert run(env, "pull", force=True)[0] == {"log/a.md": "error"}
    assert run(env, "push", prune=True)[0] == {"log/a.md": "error"}
    assert len(_rows(env)) == 2


def test_key_differing_only_in_case_never_overwrites(env):
    manifest(env, LOGMAP)
    _row(env, "Entry.md", "---\ntitle: Mine\n---\n\nMy row body.\n")
    run(env, "push")
    did = list(env["fake"].dbs)[0]
    env["fake"].api("POST", "/pages", body={"parent": {"database_id": did}, "properties": {
        "Title": {"title": [{"type": "text", "text": {"content": "Theirs"}}]},
        "Key": {"rich_text": [{"type": "text", "text": {"content": "entry"}}]}}, "children": [P("Their body.")]})
    run(env, "pull")
    assert "My row body." in (env["proj"] / "log" / "Entry.md").read_text(encoding="utf-8")


def test_one_bad_row_does_not_stop_the_others(env, monkeypatch):
    manifest(env, LOGMAP)
    for n in "abcd":
        _row(env, n + ".md", "---\ntitle: %s\n---\n" % n.upper())
    _res, t = run(env, "push")
    bad = t.row_ids["log/b.md"]
    for n in "abcd":
        _row(env, n + ".md", "---\ntitle: %s\n---\n\nmore\n" % n.upper())
    real = env["fake"].api

    def api(method, path, *a, **k):
        if method == "PATCH" and path == "/pages/%s" % bad:
            return 400, {"message": "body failed validation: url is not a valid URL"}
        return real(method, path, *a, **k)
    monkeypatch.setattr(N, "api", api)
    res, _ = run(env, "push")
    assert res == {"log/a.md": "pushed", "log/b.md": "error", "log/c.md": "pushed", "log/d.md": "pushed"}


def test_row_pull_keeps_the_front_matter_style(env):
    manifest(env, LOGMAP)
    src = "---\nverification: accepted\nurl: https://example.com/decision/1/\n---\n# Adopt the two-way sync\n\nWhy.\n"
    _row(env, "adopt-sync.md", src)
    run(env, "push")
    row = _rows(env)[0]
    row["properties"]["Verification"] = {"type": "select", "select": {"name": "rejected"}}
    row["last_edited_time"] = env["fake"]._now()
    assert run(env, "pull")[0] == {"log/adopt-sync.md": "pulled"}
    assert (env["proj"] / "log" / "adopt-sync.md").read_text(encoding="utf-8") == src.replace("accepted", "rejected")
    assert run(env, "status")[0] == {}


def test_front_matter_scalars_stay_plain_and_parse_back():
    for v in ("https://example.com/a/b", "a:b", "Q3 plan", "12:30", "x:", "yes", "3.5", "#tag", "a: b"):
        fm = T.dump_front_matter([("k", v)])
        assert T._yaml_load(fm.strip("-\n"))["k"] == v, v
    assert "url: https://example.com/a/b" in T.dump_front_matter([("url", "https://example.com/a/b")])


def test_rekeyed_rows_are_recognised(env):
    manifest(env, LOGMAP)
    _row(env, "drop-mcp.md", "---\ntitle: Drop mcp\n---\n\nbody\n")
    run(env, "push")
    pid = _rows(env)[0]["id"]
    _set_key(env, pid, "../evil")                                     # an unsafe Key: set back
    res, t = run(env, "pull")
    assert res == {"log/drop-mcp.md": "re-keyed"}
    assert sorted(p.name for p in (env["proj"] / "log").iterdir()) == ["drop-mcp.md"]
    assert _key(env["fake"].pages[pid]) == "drop-mcp"
    _set_key(env, pid, "renamed")                                     # a plain rename: reported
    res, t = run(env, "push")
    assert res == {"log/drop-mcp.md": "remote-deleted", "log/renamed.md": "new-remote"}
    assert "its Key in Notion is now 'renamed'" in _msgs(t, "log/drop-mcp.md")[0][1]
    run(env, "push", force=True)
    assert len(_rows(env)) == 1                                       # --force made no duplicate
    res, t = run(env, "pull")
    assert res["log/renamed.md"] == "error" and "synced as" in _msgs(t, "log/renamed.md")[0][1]
    assert not (env["proj"] / "log" / "renamed.md").exists()


# ------------------------------------------------------------------ pages, sections, uploads
def test_pulled_page_titled_index_does_not_become_the_folder_index(env):
    manifest(env, "map:\n  - path: research\n    pages: new\n")
    (env["proj"] / "research").mkdir()
    (env["proj"] / "research" / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
    run(env, "push")
    folder = tree(env)._id(tree(env).items["research/"])
    env["fake"].new_page("Index", parent=folder, blocks=[P("a colleague's page")])
    assert run(env, "pull")[0] == {"research/index-page.md": "pulled"}
    res, _ = run(env, "push", prune=True)
    assert "archived" not in res.values()
    assert ("child_page", "Index") in env["fake"].texts(folder)


def test_failed_upload_changes_nothing(env, monkeypatch):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    before = env["fake"].texts(env["hub"])
    (env["proj"] / "chart.png").write_bytes(b"png")
    (env["proj"] / "nous.md").write_text("# What does Nous have?\n\n- Tent poles, revised\n- Connectors\n\n"
                                         "![chart](chart.png)\n", encoding="utf-8")

    def fail(path):
        raise SystemExit("upload chart.png failed [500]")
    monkeypatch.setattr(N, "upload_file", fail)
    res, _ = run(env, "push")
    assert res == {"nous.md": "error"}
    assert env["fake"].texts(env["hub"]) == before


def test_unshared_container_blocks_only_its_own_entry(env, monkeypatch):
    ghost = "0123456789abcdef0123456789abcdef"
    manifest(env, MAP % env["sub"] + "  - path: extra.md\n    page: new\n    in: https://www.notion.so/%s\n" % ghost)
    run(env, "pull")
    (env["proj"] / "extra.md").write_text("# Extra\n\nx\n", encoding="utf-8")
    (env["proj"] / "josh.md").write_text("# J\n\nnew words\n", encoding="utf-8")
    real = env["fake"].api

    def api(method, path, *a, **k):
        if ghost[:8] in path.replace("-", ""):
            return 404, {"message": "Could not find block — make sure it is shared with your integration"}
        return real(method, path, *a, **k)
    monkeypatch.setattr(N, "api", api)
    res, _ = run(env, "push")
    assert res == {"josh.md": "pushed", "extra.md": "error"}


def test_refused_new_note_leaves_no_empty_page(env):
    manifest(env, "map:\n  - path: research\n    pages: new\n")
    (env["proj"] / "research").mkdir()
    many = " ".join("[l%d](https://x.com/%d)" % (i, i) for i in range(120))
    (env["proj"] / "research" / "many-links.md").write_text("# Many links\n\n" + many + "\n", encoding="utf-8")
    (env["proj"] / "research" / "ok.md").write_text("# OK\n\nfine\n", encoding="utf-8")
    res, t = run(env, "push")
    assert res["research/many-links.md"] == "error" and res["research/ok.md"] == "pushed"
    folder = t._id(t.items["research/"])
    assert ("child_page", "Many links") not in env["fake"].texts(folder)


def test_create_true_before_the_first_push(env, capsys):
    manifest(env, "map:\n  - path: plan.md\n    section: Next steps\n    create: true\n")
    assert run(env, "status")[0] == {"plan.md": "skipped"}
    assert T._run("pull", [str(env["proj"])]) == 0
    (env["proj"] / "plan.md").write_text("# Next steps\n\n- ship\n", encoding="utf-8")
    res, t = run(env, "status")
    assert res == {"plan.md": "new-local"} and "push adds it" in _msgs(t, "plan.md")[0][1]
    assert "create: true" not in _msgs(t, "plan.md")[0][1]
    assert T._run("pull", [str(env["proj"])]) == 0
    capsys.readouterr()
    assert T._run("push", [str(env["proj"]), "--dry-run"]) == 0
    assert "would-push" in capsys.readouterr().out
    assert run(env, "push")[0] == {"plan.md": "pushed"}
    assert env["fake"].texts(env["hub"])[-2:] == [("heading_1", "Next steps"), ("bulleted_list_item", "ship")]


def _page_hub(env, blocks):
    pid = env["fake"].new_page("Plain hub", blocks=blocks)
    return pid


def test_created_heading_takes_the_pages_section_level(env):
    hub = _page_hub(env, [H(2, "Alpha"), P("a"), H(2, "Beta"), H(3, "Beta detail"), P("b")])
    (env["proj"] / "notion.yaml").write_text("hub: %s\nmap:\n  - path: c.md\n    section: Gamma\n    create: true\n"
                                             % hub, encoding="utf-8")
    (env["proj"] / "c.md").write_text("# Gamma\n\nc\n", encoding="utf-8")
    assert run(env, "push")[0] == {"c.md": "pushed"}
    assert ("heading_2", "Gamma") in env["fake"].texts(hub)


def test_nested_mapped_sections_are_blocked(env):
    hub = _page_hub(env, [H(1, "Top"), P("t"), H(2, "Sub"), P("s"), H(1, "Other"), P("o")])
    (env["proj"] / "notion.yaml").write_text(
        "hub: %s\nmap:\n  - path: top.md\n    section: Top\n  - path: sub.md\n    section: Sub\n"
        "  - path: other.md\n    section: Other\n" % hub, encoding="utf-8")
    t = tree(env)
    t.check_overlaps()
    t.pull()
    res = {k: v for v, k, m in t.results}
    assert res == {"top.md": "error", "sub.md": "error", "other.md": "pulled"}
    assert "lies inside" in _msgs(t, "sub.md")[0][1]


# ------------------------------------------------------------------ map / locate / diff / link
def test_map_writes_create_only_for_a_genuinely_new_heading(env, capsys):
    manifest(env, "map:\n  - path: insights.md\n    page: %s\n" % env["sub"])
    p = env["proj"]
    T.cmd_map([str(p / "typo.md"), "--section", "What does Nous hav"])
    assert "did you mean" in capsys.readouterr().out
    T.cmd_map([str(p / "new.md"), "--section", "Totally different topic"])
    T.cmd_map([str(p / "forced.md"), "--section", "What does Josh thnk", "--create"])
    T.cmd_map([str(p / "ideas.md"), "--section", "Brand new heading", "--in", "insights.md"])
    T.cmd_map([str(p / "wa.md"), "--section", "WhatsApp", "--in", "insights.md"])
    entries = {e["path"]: e for e in tree(env).man["map"]}
    assert "create" not in entries["typo.md"]
    assert entries["new.md"]["create"] is True
    assert entries["forced.md"]["create"] is True
    assert entries["ideas.md"]["create"] is True                    # `--in` an entry path is checked too
    assert "create" not in entries["wa.md"]


def test_an_unmapped_path_is_an_error(env, capsys):
    manifest(env, MAP % env["sub"])
    for verb in ("push", "pull", "status"):
        capsys.readouterr()
        assert T._run(verb, [str(env["proj"] / "findngs.md")]) == 1, verb
        assert "is not mapped" in capsys.readouterr().out


def test_locate_on_a_folder_pages_own_text(env, capsys):
    manifest(env, "map:\n  - path: research\n    pages: new\n")
    (env["proj"] / "research").mkdir()
    (env["proj"] / "research" / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
    run(env, "push")
    folder = tree(env)._id(tree(env).items["research/"])
    env["fake"]._insert(folder, [H(2, "Summary"), P("[to follow]")])
    bid = env["fake"].ids_of(folder)[-1]
    capsys.readouterr()
    T.cmd_locate(["https://www.notion.so/%s#%s" % (folder.replace("-", ""), bid.replace("-", ""))])
    out = capsys.readouterr().out
    assert "mapped   no" in out and "folder of pages" in out


def test_diff_refuses_folders_and_reports_unreadable_files(env):
    manifest(env, MAP % env["sub"] + LOGMAP.replace("map:\n", ""))
    run(env, "pull")
    with pytest.raises(SystemExit, match="not a folder"):
        T.cmd_diff([str(env["proj"] / "log")])
    (env["proj"] / "josh.md").write_bytes("# J\n\ncaf\xe9\n".encode("cp1252"))
    with pytest.raises(SystemExit, match="not UTF-8"):
        T.cmd_diff([str(env["proj"] / "josh.md")])


def test_guard_messages_never_suggest_forcing_over_notion_edits(env):
    manifest(env, MAP % env["sub"])
    t = tree(env)
    assert "--force" not in t.refusal("remote-ahead", "josh.md")
    assert "push --force proj/josh.md" in t.refusal("conflict", "josh.md")


def test_link_creates_the_folder_and_outline_explains_a_missing_manifest(env, capsys):
    assert T.cmd_link([str(env["root"] / "newproj"), env["hub"]]) == 0
    assert (env["root"] / "newproj" / "notion.yaml").is_file()
    (env["root"] / "other").mkdir()
    with pytest.raises(SystemExit, match="no notion.yaml in"):
        T.cmd_outline([str(env["root"] / "other")])


def test_safe_key_rules():
    for ok in ("Q&A", "notes (old)", "O'Brien, J", "日本語メモ", "_draft", "v1.2"):
        assert T.safe_key(ok), ok
    for bad in ("", "../x", "a/b", "a\\b", "C:x", ".hidden", "x.", "x ", " x", "con", "a..b", "q?", "x" * 121):
        assert not T.safe_key(bad), bad
    assert T.file_slug("Index") == "index-page" and T.file_slug("README") == "readme-page"
