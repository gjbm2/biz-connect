"""Tests of the low-level Notion client (bizconnect.connectors.notion): the retry policy of
api(), NotionError, norm_id's URL forms, and append_children's request limits against the
strict in-memory fake (tests/fake_notion.py). Nothing here touches the live API."""
from __future__ import annotations

import copy
import email.message
import email.utils
import http.client
import io
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bizconnect.connectors import notion as N
from tests.fake_notion import FakeNotion

HUB = "3e4e1111222233334444555566667777"
PAGE = "9a9a1111222233334444555566668888"


def dashed(h):
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


# ============================================================================ api() retries
class _Resp:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http(code, retry_after=None, message="boom"):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    body = json.dumps({"object": "error", "status": code, "message": message}).encode()
    return urllib.error.HTTPError(N.API + "/x", code, "err", hdrs, io.BytesIO(body))


@pytest.fixture
def wire(monkeypatch):
    """Script urlopen: wire.script is a list of outcomes (an exception to raise, or a _Resp);
    wire.sent records (method, url, Notion-Version) per attempt; wire.sleeps the waits."""
    class W:
        script, sent, sleeps = [], [], []

    def urlopen(req, timeout=None):
        W.sent.append((req.get_method(), req.full_url, req.get_header("Notion-version")))
        out = W.script.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(N.time, "sleep", W.sleeps.append)
    monkeypatch.setattr(N, "_token", lambda: "secret-test")
    monkeypatch.setattr(N.config, "secret", lambda key, default=None, required=False: default)
    monkeypatch.setattr(N, "PINNED_VERSION", None)
    return W


def _network_errors():
    return [socket.timeout("timed out"), TimeoutError("read timed out"), ConnectionResetError(10054, "reset"),
            urllib.error.URLError("unreachable"), http.client.RemoteDisconnected("closed"),
            http.client.IncompleteRead(b"par")]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_idempotent_calls_retry_5xx_529_and_network_errors(wire, method):
    wire.script = [_http(500), _http(502), _http(529), socket.timeout("timed out"),
                   http.client.RemoteDisconnected("closed"), _Resp(200, b'{"id": "x"}')]
    st, body = N.api(method, "/blocks/abc")
    assert (st, body) == (200, {"id": "x"})
    assert len(wire.sent) == 6 and not wire.script


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_idempotent_calls_retry_every_network_error_kind(wire, method):
    errs = _network_errors()
    for e in errs:
        wire.script = [e, _Resp()]
        wire.sent.clear()
        assert N.api(method, "/blocks/abc")[0] == 200, type(e)
        assert len(wire.sent) == 2


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_writes_retry_only_on_409_429_529(wire, method):
    wire.script = [_http(409), _http(429), _http(529), _Resp(200, b'{"results": []}')]
    st, body = N.api(method, "/blocks/abc/children", body={"children": []})
    assert (st, body) == (200, {"results": []})
    assert [m for m, _u, _v in wire.sent] == [method] * 4


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_writes_are_not_resent_on_other_5xx(wire, method, code):
    wire.script = [_http(code, message="bad gateway"), _Resp()]
    st, body = N.api(method, "/blocks/abc/children", body={"children": []})
    assert st == code and body["message"] == "bad gateway"
    assert len(wire.sent) == 1 and wire.sleeps == []


@pytest.mark.parametrize("method, path", [("POST", "/pages"), ("PATCH", "/blocks/abc/children")])
def test_writes_are_not_resent_after_a_network_error(wire, method, path):
    for e in _network_errors():
        wire.script = [e, _Resp()]
        wire.sent.clear()
        st, body = N.api(method, path, body={"parent": {"page_id": "p"}})
        assert st == 599, type(e)
        assert "not resent" in body["message"]
        assert len(wire.sent) == 1


@pytest.mark.parametrize("path", ["/pages/%s" % PAGE, "/blocks/%s" % PAGE, "/databases/%s" % HUB])
def test_a_patch_that_sets_values_is_retried(wire, path):
    """PATCH of a page/block/database sets values: resending it leaves the same state."""
    wire.script = [_http(502), _Resp(200, b'{"id": "x"}')]
    assert N.api("PATCH", path, body={"archived": False}) == (200, {"id": "x"})
    assert len(wire.sent) == 2
    for e in _network_errors():
        wire.script = [e, _Resp()]
        wire.sent.clear()
        assert N.api("PATCH", path, body={"archived": False})[0] == 200, type(e)
        assert len(wire.sent) == 2


@pytest.mark.parametrize("path", ["/blocks/abc/children", "/blocks/abc/children/", "/blocks/abc/children?x=1"])
def test_a_patch_append_is_never_resent(wire, path):
    wire.script = [_http(502), _Resp()]
    assert N.api("PATCH", path, body={"children": []})[0] == 502
    assert len(wire.sent) == 1
    wire.script = [socket.timeout("t"), _Resp()]
    wire.sent.clear()
    assert N.api("PATCH", path, body={"children": []})[0] == 599
    assert len(wire.sent) == 1


def test_a_write_error_stops_the_caller(wire):
    """append_children gets the 502 back and raises instead of resending the append."""
    wire.script = [_http(502, message="bad gateway"), _Resp()]
    with pytest.raises(N.NotionError, match=r"append blocks failed \[502\]: bad gateway"):
        N.append_children("abc", [{"type": "paragraph", "paragraph": {"rich_text": []}}])
    assert len(wire.sent) == 1


def test_read_only_post_queries_count_as_idempotent(wire):
    wire.script = [_http(502), socket.timeout("t"), _Resp(200, b'{"results": []}')]
    assert N.api("POST", "/databases/%s/query" % HUB, body={"page_size": 100})[0] == 200
    assert len(wire.sent) == 3
    wire.script = [_http(502), _Resp()]
    wire.sent.clear()
    assert N.api("POST", "/search", body={"query": "x"})[0] == 200
    assert len(wire.sent) == 2


def test_file_upload_send_is_not_resent_on_5xx(wire):
    wire.script = [_http(503), _Resp()]
    st, _ = N.api("POST", "", raw_url="https://api.notion.com/v1/file_uploads/x/send",
                  headers={"Authorization": "Bearer t"}, data_bytes=b"--b--")
    assert st == 503 and len(wire.sent) == 1


def test_retry_after_is_honoured(wire):
    wire.script = [_http(429, retry_after=7), _http(529, retry_after="2.5"), _Resp()]
    assert N.api("PATCH", "/pages/abc", body={})[0] == 200
    assert wire.sleeps == [7.0, 2.5]


def test_retry_after_http_date_and_junk(wire):
    later = email.utils.formatdate(time.time() + 30, usegmt=True)
    wire.script = [_http(429, retry_after=later), _http(429, retry_after="soon"), _Resp()]
    assert N.api("GET", "/users/me")[0] == 200
    assert 20 <= wire.sleeps[0] <= 31
    assert wire.sleeps[1] == 2.0                   # unparseable -> the backoff delay (1s, then 2s)


def test_retries_exhausted_returns_the_last_error(wire):
    wire.script = [_http(503) for _ in range(6)]
    st, body = N.api("GET", "/blocks/abc")
    assert st == 503 and len(wire.sent) == 6        # 1 try + 5 retries
    wire.script = [socket.timeout("t") for _ in range(3)]
    wire.sent.clear()
    st, body = N.api("GET", "/blocks/abc", retries=2)
    assert st == 599 and "network error" in body["message"] and len(wire.sent) == 3


def test_non_retryable_status_comes_straight_back(wire):
    wire.script = [_http(400, message="body failed validation")]
    assert N.api("GET", "/blocks/abc") == (400, {"object": "error", "status": 400,
                                                  "message": "body failed validation"})
    assert len(wire.sent) == 1


def test_pinned_version_overrides_notion_version(wire, monkeypatch):
    monkeypatch.setattr(N.config, "secret", lambda key, default=None, required=False:
                        "2025-09-03" if key == "NOTION_VERSION" else default)
    wire.script = [_Resp(), _Resp()]
    N.api("GET", "/users/me")
    monkeypatch.setattr(N, "PINNED_VERSION", "2022-06-28")
    N.api("GET", "/users/me")
    assert [v for _m, _u, v in wire.sent] == ["2025-09-03", "2022-06-28"]


# ============================================================================ NotionError
def test_die_raises_notion_error_with_the_same_message():
    N._die(200, {}, "fine")                           # no error below 300
    with pytest.raises(N.NotionError) as ei:
        N._die(404, {"message": "Could not find block"}, "list children")
    e = ei.value
    assert isinstance(e, SystemExit)
    assert str(e) == e.code == "list children failed [404]: Could not find block"


def test_uncaught_notion_error_exits_like_sys_exit():
    root = Path(__file__).resolve().parents[1]
    code = ("from bizconnect.connectors import notion as N\n"
            "N._die(404, {'message': 'gone'}, 'list children')\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr.strip() == "list children failed [404]: gone"     # message only, no traceback


# ============================================================================ norm_id
@pytest.mark.parametrize("s, want", [
    (HUB, HUB),
    (dashed(HUB), HUB),
    (HUB.upper(), HUB),
    ("https://www.notion.so/acme/Project-Hub-%s" % HUB, HUB),
    ("https://www.notion.so/acme/Project-Hub-%s?pvs=4" % HUB, HUB),
    ("https://www.notion.so/acme/Project-Hub-%s#%s" % (HUB, PAGE), HUB),      # block anchor: page id
    ("https://www.notion.so/acme/Deadbeef-Cafe-Plan-%s/" % HUB, HUB),        # stray hex in the slug
    ("https://www.notion.so/%s" % HUB, HUB),
    ("https://acme.notion.site/Hub-%s" % HUB, HUB),
    ("https://www.notion.so/acme/%s?v=%s" % (HUB, PAGE), HUB),               # database view: not p=
    # side-peek: the address bar while a sub-page is open in peek mode -> the PEEKED page
    ("https://www.notion.so/acme/Project-Hub-%s?p=%s&pm=s" % (HUB, PAGE), PAGE),
    ("https://www.notion.so/acme/Project-Hub-%s?pm=s&p=%s" % (HUB, PAGE), PAGE),
    ("https://www.notion.so/acme/Project-Hub-%s?p=%s" % (HUB, dashed(PAGE)), PAGE),
    ("https://www.notion.so/acme/Project-Hub-%s?p=%s&pm=s#frag" % (HUB, PAGE), PAGE),
    ("https://www.notion.so/acme/Project-Hub-%s?p=notanid&pm=s" % HUB, HUB),  # junk p= is ignored
])
def test_norm_id_forms(s, want):
    assert N.norm_id(s) == dashed(want)


def test_norm_id_dot_means_the_notes_page(monkeypatch):
    peek = "https://www.notion.so/acme/Hub-%s?p=%s&pm=s" % (HUB, PAGE)
    for page, want in ((HUB, HUB), (peek, PAGE)):
        monkeypatch.setattr(N.config, "load_connections", lambda page=page: ({"notion": {"notes_page": page}}, None))
        assert N.norm_id(".") == dashed(want)
    monkeypatch.setattr(N.config, "load_connections", lambda: ({}, None))
    with pytest.raises(N.NotionError, match="notes_page"):
        N.norm_id(".")


def test_norm_id_without_an_id_raises_notion_error():
    with pytest.raises(N.NotionError) as ei:
        N.norm_id("https://www.notion.so/my-workspace/Project-hub")
    assert isinstance(ei.value, SystemExit)
    assert str(ei.value) == "could not find a Notion id in: 'https://www.notion.so/my-workspace/Project-hub'"


# ============================================================================ append_children
def _rt(text):
    return [{"type": "text", "text": {"content": text}}] if text else []


def P(text, kids=()):
    b = {"type": "paragraph", "paragraph": {"rich_text": _rt(text)}}
    if kids:
        b["paragraph"]["children"] = list(kids)
    return b


def B(text, kids=()):
    b = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(text)}}
    if kids:
        b["bulleted_list_item"]["children"] = list(kids)
    return b


def TABLE(rows):
    return {"type": "table", "table": {"table_width": 2, "has_column_header": True, "has_row_header": False,
                                       "children": [{"type": "table_row", "table_row": {
                                           "cells": [_rt("r%d" % i), _rt("v%d" % i)]}} for i in range(rows)]}}


def _label(t, body, cells_key):
    if t == "table_row":
        return "|".join("".join(s[cells_key] if cells_key == "plain_text" else s["text"]["content"]
                                for s in c) for c in body["cells"])
    if cells_key == "plain_text":
        return "".join(r.get("plain_text", "") for r in body.get("rich_text", []))
    return "".join(s["text"]["content"] for s in body.get("rich_text", []))


def want(blocks):
    """The (type, text, children) tree a list of create-shape blocks should produce."""
    return [(b["type"], _label(b["type"], b[b["type"]], "content"), want(b[b["type"]].get("children", [])))
            for b in blocks]


def tree(fake, parent):
    """The (type, text, children) tree the fake actually holds under `parent`."""
    out = []
    for cid in fake.kids.get(parent, []):
        b = fake.blocks[cid]
        out.append((b["type"], _label(b["type"], b[b["type"]], "plain_text"), tree(fake, cid)))
    return out


def _audit(body):
    """Independent check of one request body against Notion's limits -> (blocks, max array, depth)."""
    stats = {"blocks": 0, "arr": 0, "depth": 0}

    def walk(blocks, depth):
        stats["arr"] = max(stats["arr"], len(blocks))
        stats["blocks"] += len(blocks)
        stats["depth"] = max(stats["depth"], depth)
        for b in blocks:
            t = b["type"]
            assert t != "table" or b[t].get("children"), "a table was sent without rows"
            for k in ("rich_text", "caption"):
                stats["arr"] = max(stats["arr"], len(b[t].get(k) or []))
            if b[t].get("children"):
                walk(b[t]["children"], depth + 1)

    walk(body["children"], 0)
    return stats


@pytest.fixture
def fake(monkeypatch):
    f = FakeNotion()
    f.sent = []                       # (parent id, body) of every append request
    real = f.api

    def api(method, path, body=None, **kw):
        if method == "PATCH" and path.endswith("/children"):
            f.sent.append((path.split("/")[2], copy.deepcopy(body)))
        return real(method, path, body=body, **kw)

    monkeypatch.setattr(N, "api", api)
    yield f
    assert not f.rejected, f.rejected
    for _parent, body in f.sent:
        s = _audit(body)
        assert s["arr"] <= 100 and s["blocks"] <= 1000 and s["depth"] <= N.MAX_NEST
        assert len(json.dumps(body)) <= N.MAX_BYTES


def test_table_with_150_rows(fake):
    pid = fake.new_page("Doc")
    blocks = [P("before"), TABLE(150), P("after")]
    ids = N.append_children(pid, blocks)
    assert ids == fake.ids_of(pid) and len(ids) == 3
    assert tree(fake, pid) == want(blocks)
    table = ids[1]
    assert [len(b["children"][1]["table"]["children"]) for p, b in fake.sent if p == pid] == [100]
    assert [len(b["children"]) for p, b in fake.sent if p == table] == [50]     # rows appended to the table


def test_one_bullet_with_150_children(fake):
    pid = fake.new_page("Doc")
    blocks = [B("parent", [B("kid %d" % i) for i in range(150)])]
    ids = N.append_children(pid, blocks)
    assert tree(fake, pid) == want(blocks)
    assert len(fake.kids[ids[0]]) == 150
    assert [len(b["children"]) for p, b in fake.sent if p == ids[0]] == [50]


def test_100_bullets_with_20_sub_bullets(fake):
    pid = fake.new_page("Doc")
    blocks = [B("b%d" % i, [B("b%d.%d" % (i, j)) for j in range(20)]) for i in range(100)]
    ids = N.append_children(pid, blocks)
    assert ids == fake.ids_of(pid) and len(ids) == 100
    assert tree(fake, pid) == want(blocks)
    to_page = [b for p, b in fake.sent if p == pid]
    assert len(to_page) >= 3                                   # 2100 blocks: > 2 requests of <= 1000
    assert all(_audit(b)["blocks"] <= 1000 for b in to_page)


def test_four_levels_of_nesting(fake):
    pid = fake.new_page("Doc")

    def chain(label, depth):
        return B(label, [chain("%s.%d" % (label, k), depth - 1) for k in range(2)] if depth else ())

    blocks = [P("intro"), chain("a", 4), chain("b", 4), P("outro")]      # 5 levels of blocks
    ids = N.append_children(pid, blocks)
    assert ids == fake.ids_of(pid)
    assert tree(fake, pid) == want(blocks)


def test_nested_table_is_never_sent_without_its_rows(fake):
    pid = fake.new_page("Doc")
    blocks = [B("l0", [B("l1", [P("l2 before"), TABLE(3), P("l2 after")])]),
              B("x", [TABLE(120)])]
    N.append_children(pid, blocks)
    assert tree(fake, pid) == want(blocks)                    # _audit (teardown) checks every table


def test_a_column_opening_with_a_table_is_refused_before_sending(fake):
    """column_list > column > table > rows is 3 levels below the top: no request can create
    that column with its first child, and Notion rejects a column sent without children."""
    pid = fake.new_page("Doc")
    col = lambda *kids: {"type": "column", "column": {"children": list(kids)}}
    blocks = [P("intro"), {"type": "column_list", "column_list": {"children": [col(TABLE(2)), col(P("b"))]}}]
    with pytest.raises(N.NotionError, match="cannot create a column whose first child needs deeper nesting"):
        N.append_children(pid, blocks)
    assert fake.sent == []
    ok = [{"type": "column_list", "column_list": {"children": [col(P("a"), TABLE(2)), col(P("b"))]}}]
    tb, deferred, _n, _b = N._trim(ok[0])                    # a table after the first child is deferred
    assert tb["column_list"]["children"][0]["column"]["children"] == [P("a")]
    assert deferred == [((0,), [TABLE(2)])]


def test_batches_split_by_payload_size(fake):
    pid = fake.new_page("Doc")
    seg = [{"type": "text", "text": {"content": "x" * 1900}}] * 60
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": list(seg)}} for _ in range(30)]
    ids = N.append_children(pid, blocks)
    assert ids == fake.ids_of(pid) and len(ids) == 30
    assert len([1 for p, _b in fake.sent if p == pid]) >= 8  # ~3.5MB of text / 450KB


def test_insert_after_and_at_start_keep_order_across_batches(fake):
    pid = fake.new_page("Doc", blocks=[P("A"), P("B")])
    a, b = fake.ids_of(pid)
    mid = [P("m%d" % i) for i in range(250)]
    ids = N.append_children(pid, mid, after=a)
    assert fake.ids_of(pid) == [a] + ids + [b]
    top = [P("t%d" % i) for i in range(120)]
    ids2 = N.append_children(pid, top, at_start=True)
    assert fake.ids_of(pid) == ids2 + [a] + ids + [b]
    assert [t for _ty, t, _k in tree(fake, pid)] == \
        ["t%d" % i for i in range(120)] + ["A"] + ["m%d" % i for i in range(250)] + ["B"]


def test_input_blocks_are_not_mutated(fake):
    pid = fake.new_page("Doc")
    blocks = [TABLE(130), B("p", [B("k%d" % i) for i in range(120)])]
    before = copy.deepcopy(blocks)
    N.append_children(pid, blocks)
    assert blocks == before


# ============================================================================ the strict fake
def test_fake_rejects_requests_over_the_limits():
    f = FakeNotion()
    pid = f.new_page("Doc")
    path = "/blocks/%s/children" % pid
    bad = {
        "children > 100": {"children": [P("x") for _ in range(101)]},
        "table rows > 100": {"children": [TABLE(101)]},
        "sub-items > 100": {"children": [B("x", [B("y") for _ in range(101)])]},
        "blocks > 1000": {"children": [B("b", [B("c") for _ in range(20)]) for _ in range(50)]},
        "nesting > 2": {"children": [B("a", [B("b", [B("c", [B("d")])])])]},
        "rich_text > 100": {"children": [{"type": "paragraph", "paragraph": {"rich_text": _rt("x") * 101}}]},
        "text > 2000": {"children": [P("x" * 2001)]},
        "payload > 500KB": {"children": [{"type": "paragraph", "paragraph": {"rich_text": _rt("x" * 2000) * 100}}
                                         for _ in range(3)]},
    }
    for name, body in bad.items():
        st, resp = f.api("PATCH", path, body=body)
        assert st == 400 and resp["code"] == "validation_error", name
        assert f.ids_of(pid) == [], name                     # nothing applied
    assert len(f.rejected) == len(bad)
    st, _ = f.api("POST", "/pages", body={"parent": {"page_id": pid},
                                          "properties": {"title": {"title": _rt("t" * 2001)}}})
    assert st == 400
    # at the limits is fine
    assert f.api("PATCH", path, body={"children": [B("b", [B("c") for _ in range(99)]) for _ in range(10)]})[0] == 200
    assert f.api("PATCH", path, body={"children": [B("a", [B("b", [B("c")])])]})[0] == 200


def test_a_rejected_append_surfaces_as_notion_error(fake):
    pid = fake.new_page("Doc")
    with pytest.raises(N.NotionError, match=r"append blocks failed \[400\].*rich_text\.length should be"):
        N.append_children(pid, [{"type": "paragraph", "paragraph": {"rich_text": _rt("x") * 101}}])
    fake.rejected.clear()                                    # expected: don't fail the teardown check
    fake.sent.clear()
