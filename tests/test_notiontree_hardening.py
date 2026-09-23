"""v0.13 hardening of the mapped Notion sync: fresh clones, unsafe input, partial failures.
Runs against the in-memory fake Notion (tests/fake_notion.py) via test_notiontree's fixture."""
from __future__ import annotations

import random
import shutil
import subprocess

import pytest

from bizconnect.connectors import notion as N
from bizconnect.connectors import notiontree as T
from tests.test_notiontree import LOGMAP, MAP, P, _row, env, manifest, run, tree  # noqa: F401 (fixture)


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=str(root),
                          capture_output=True, text=True)


def test_unsafe_row_keys_never_escape_the_folder(env):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n")
    run(env, "push")
    fake = env["fake"]
    did = list(fake.dbs)[0]
    for evil in ("..\\..\\..\\escaped", "../../escaped2", "x/entry", "C:\\abs", "con"):
        fake.api("POST", "/pages", body={"parent": {"database_id": did}, "properties": {
            "Title": {"title": [{"type": "text", "text": {"content": "Evil " + evil}}]},
            "Key": {"rich_text": [{"type": "text", "text": {"content": evil}}]}}})
    res, t = run(env, "pull")
    written = sorted(p.name for p in (env["proj"] / "log").glob("*.md"))
    assert len(written) == 6
    assert not any(p.name.startswith("escaped") for p in env["root"].rglob("*.md") if "log" not in p.parts)
    assert sum("isn't a safe file name" in m for v, k, m in t.results) == 5
    assert run(env, "status")[0] == {}


def test_prune_keeps_uploads_this_machine_never_synced(env):
    manifest(env, "map:\n  - path: context/*.pdf\n    section: Background\n")
    (env["proj"] / "context").mkdir()
    (env["proj"] / "context" / "a.pdf").write_bytes(b"%PDF")
    run(env, "push")
    (env["root"] / ".bizconnect" / "state.json").unlink()           # a teammate's clone ...
    (env["proj"] / "context" / "a.pdf").unlink()                     # ... without the (untracked) PDF
    res, _ = run(env, "push", prune=True)
    assert res == {"context/a.pdf": "local-deleted"}
    assert ("pdf", "") in env["fake"].texts(env["hub"])


def test_missing_heading_is_reported_not_created_unless_opted_in(env):
    manifest(env, "map:\n  - path: goal.md\n    section: What does Nous hav\n")
    (env["proj"] / "goal.md").write_text("# G\n\nx\n", encoding="utf-8")
    t = tree(env)
    t.push()
    v = [(a, m) for a, k, m in t.results if k == "goal.md"]
    assert v[0][0] == "missing" and "What does Nous have?" in v[0][1]
    assert [x for x in env["fake"].texts(env["hub"]) if x[1] == "What does Nous hav"] == []
    manifest(env, "map:\n  - path: goal.md\n    section: Brand new\n    create: true\n")
    res, _ = run(env, "push")
    assert res.get("goal.md") == "pushed" and ("heading_1", "Brand new") in env["fake"].texts(env["hub"])


def test_in_can_name_a_folder_of_pages(env):
    manifest(env, "map:\n  - path: research\n    pages: new\n"
                  "  - path: extra/overview.md\n    page: new\n    in: research\n")
    (env["proj"] / "research").mkdir()
    (env["proj"] / "extra").mkdir()
    (env["proj"] / "extra" / "overview.md").write_text("# Overview\n\nhi\n", encoding="utf-8")
    res, t = run(env, "push")
    assert res["extra/overview.md"] == "pushed"
    assert ("child_page", "Overview") in env["fake"].texts(t._id(t.items["research/"]))


def test_lowercase_readme_is_the_folder_page_on_every_os(env):
    manifest(env, "map:\n  - path: research\n    pages: new\n")
    d = env["proj"] / "research"
    d.mkdir()
    (d / "readme.md").write_text("# Research notes\n\nAbout this folder.\n", encoding="utf-8")
    (d / "idea.md").write_text("# Idea\n\nx\n", encoding="utf-8")
    res, t = run(env, "push")
    assert set(t.items) == {"research/", "research/idea.md"}
    assert ("child_page", "Research notes") in env["fake"].texts(env["hub"])


def test_bom_file_and_non_utf8_file(env):
    manifest(env, "map:\n  - path: notes\n    pages: new\n")
    d = env["proj"] / "notes"
    d.mkdir()
    (d / "bom.md").write_bytes("\ufeff# Proper Title\n\nBody\n".encode("utf-8"))
    (d / "latin.md").write_bytes("# Caf\xe9\n\nna\xefve\n".encode("cp1252"))
    res, t = run(env, "push")
    assert res["notes/bom.md"] == "pushed"
    assert res["notes/latin.md"] == "error"
    assert any("not UTF-8" in m for v, k, m in t.results if k == "notes/latin.md")
    assert ("child_page", "Proper Title") in env["fake"].texts(t._id(t.items["notes/"]))


def test_overlong_rich_text_is_refused_not_truncated(env):
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    many = " ".join("[l%d](https://x.com/%d)" % (i, i) for i in range(120))
    (env["proj"] / "josh.md").write_text("# J\n\n" + many + "\n", encoding="utf-8")
    res, t = run(env, "push")
    assert res["josh.md"] == "error"
    assert any("Notion allows 100" in m for v, k, m in t.results if k == "josh.md")
    assert env["fake"].texts(env["hub"])[1] == ("paragraph", "[populate from agentic research]")


def test_status_makes_no_writes_and_sees_new_pages(env):
    manifest(env, MAP % env["sub"] + "  - path: research\n    pages: new\n")
    (env["proj"] / "research").mkdir()
    (env["proj"] / "research" / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
    run(env, "pull")
    run(env, "push")
    before = (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    folder = tree(env)._id(tree(env).items["research/"])
    env["fake"].new_page("Added in Notion", parent=folder, blocks=[P("hey")])
    assert T._run("status", [str(env["proj"])]) == 0
    assert (env["proj"] / "notion.yaml").read_text(encoding="utf-8") == before
    t = tree(env)
    t.status()
    assert ("new-remote", "research/added-in-notion.md") in [(v, k) for v, k, m in t.results]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_fresh_clone_first_sync(env, capsys):
    """Another machine (no sync state): the committed `synced` fingerprint says which side moved.
    A file unchanged since the last sync takes Notion's newer version; edits on both sides are a
    conflict, resolved by hand; with no fingerprint at all, differing sides are 'no-baseline'."""
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    _git(env["root"], "init", "-q")
    _git(env["root"], "add", "-A")
    _git(env["root"], "commit", "-qm", "sync")
    (env["root"] / ".bizconnect" / "state.json").unlink()            # a different machine
    fake, hub = env["fake"], env["hub"]
    fake.edit_text(fake.ids_of(hub)[3], "Tent poles (energy)")
    assert run(env, "status")[0] == {"nous.md": "remote-ahead"}
    assert run(env, "pull")[0] == {"nous.md": "pulled"}
    assert "Tent poles (energy)" in (env["proj"] / "nous.md").read_text(encoding="utf-8")
    (env["root"] / ".bizconnect" / "state.json").unlink()
    (env["proj"] / "josh.md").write_text("# J\n\nlocal words\n", encoding="utf-8")
    fake.edit_text(fake.ids_of(hub)[1], "notion words")
    assert run(env, "status")[0] == {"josh.md": "conflict"}
    assert run(env, "push")[0] == {"josh.md": "conflict"}
    assert run(env, "pull")[0] == {"josh.md": "conflict"}
    capsys.readouterr()
    assert T.cmd_diff([str(env["proj"] / "josh.md")]) == 0
    out = capsys.readouterr().out
    assert "-notion words" in out and "+local words" in out
    m = env["proj"] / "notion.yaml"                                  # an entry synced before `synced` existed
    m.write_text("\n".join(x for x in m.read_text(encoding="utf-8").splitlines() if "synced:" not in x) + "\n",
                 encoding="utf-8")
    assert run(env, "status")[0] == {"josh.md": "no-baseline"}
    res, t = run(env, "pull", force=True)
    assert res == {"josh.md": "pulled"}
    kept = [m for v, k, m in t.results if k == "josh.md"][0]          # the uncommitted edit is kept
    assert "uncommitted local copy kept" in kept
    assert "local words" in (env["root"] / kept.split("kept: ")[1].rstrip(")")).read_text(encoding="utf-8")


def test_malformed_manifests_are_reported_and_others_still_run(env, capsys):
    manifest(env, MAP % env["sub"])
    bad = env["root"] / "bad"
    bad.mkdir()
    for text, want in (("hub: %s\nmap:\n  - just-a-string\n" % env["hub"], "must be a mapping"),
                       ("hub: %s\nmap:\n  path: x.md\n" % env["hub"], "must be a list"),
                       ("hub: https://www.notion.so/team/Project-hub\nmap: []\n", "has no Notion page id"),
                       ("hub: [unclosed\n", "not valid YAML"),
                       ("hub: %s\nmap:\n  - path: ../x.md\n    page: new\n" % env["hub"], "inside this folder"),
                       ("hub: %s\nmap:\n  - path: x.md\n    widget: new\n" % env["hub"], "newer biz-connect")):
        (bad / "notion.yaml").write_text(text, encoding="utf-8")
        with pytest.raises(T.SyncError, match=want):
            T.Tree(env["root"], bad / "notion.yaml", out=lambda *a: None)
    capsys.readouterr()
    assert T._run("pull", []) == 1                                    # the bad one fails ...
    out = capsys.readouterr().out
    assert "newer biz-connect" in out and "pulled" in out            # ... the good one still pulls


def test_ids_are_saved_even_when_a_later_step_blows_up(env, monkeypatch):
    manifest(env, "map:\n  - path: a.md\n    page: new\n  - path: b.md\n    page: new\n")
    (env["proj"] / "a.md").write_text("# A\n\nx\n", encoding="utf-8")
    (env["proj"] / "b.md").write_text("# B\n\ny\n", encoding="utf-8")
    real = N.append_children
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise SystemExit("append blocks failed [502]: bad gateway")
        return real(*a, **k)
    monkeypatch.setattr(N, "append_children", flaky)
    assert T._run("push", [str(env["proj"])]) == 1
    txt = (env["proj"] / "notion.yaml").read_text(encoding="utf-8")
    assert txt.count("id: ") == 2                  # both pages' ids kept: a re-run won't duplicate them


def test_database_400_is_an_error_not_gone(env, monkeypatch):
    manifest(env, LOGMAP)
    _row(env, "a.md", "---\ntitle: A\n---\n")
    run(env, "push")
    real = env["fake"].api

    def api(method, path, *a, **k):
        if method == "GET" and path.startswith("/databases/"):
            return 400, {"message": "Databases with multiple data sources are not supported in this API version."}
        return real(method, path, *a, **k)
    monkeypatch.setattr(N, "api", api)
    res, t = run(env, "push")
    assert res == {"log/": "error"}
    assert any("second data source" in m for v, k, m in t.results)


def test_lcs_matches_the_dp_on_random_sequences():
    rnd = random.Random(7)
    for _ in range(300):
        a = [rnd.choice("abcde") for _ in range(rnd.randint(0, 30))]
        b = [rnd.choice("abcde") for _ in range(rnd.randint(0, 30))]
        pairs = T.lcs_pairs(a, b)
        assert len(pairs) == len(T._lcs_dp(a, b))
        assert all(a[i] == b[j] for i, j in pairs)
        assert all(p1[0] < p2[0] and p1[1] < p2[1] for p1, p2 in zip(pairs, pairs[1:]))


def test_pull_then_push_of_an_unrelated_line_keeps_styled_blocks(env):
    """Regression for the v0.12 re-created block: a styled run with an edge space."""
    fake, hub = env["fake"], env["hub"]
    blk = fake.ids_of(hub)[1]

    def seg(t, **a):
        return {"type": "text", "text": {"content": t}, "plain_text": t, "annotations": a, "href": None}
    fake.blocks[blk]["paragraph"]["rich_text"] = [
        seg("headed in the right direction "), seg("for a true agent ", italic=True), seg("\u2014 this is diff.")]
    fake._insert(hub, [P("second line")], position={"type": "after_block", "after_block": {"id": blk}})
    manifest(env, MAP % env["sub"])
    run(env, "pull")
    p = env["proj"] / "josh.md"
    p.write_text(p.read_text(encoding="utf-8").replace("second line", "second line, edited"), encoding="utf-8")
    res, t = run(env, "push")
    assert res == {"josh.md": "pushed"}
    assert blk in fake.ids_of(hub)                                  # the styled paragraph was kept
    assert any("1 block(s) added, 1 removed" in m for v, k, m in t.results)
