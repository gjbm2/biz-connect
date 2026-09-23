"""An in-memory stand-in for the slice of the Notion REST API the tree sync uses.

It reproduces the behaviours the sync depends on (checked against the live API): pages are
also `child_page` blocks of their parent; `PATCH /blocks/{id}/children` honours `position`
(start / after_block) and answers with the NEW blocks first, then every later sibling;
uploaded files come back as `file` objects with a signed URL whose second-to-last path
segment is stable; select options are created on first use.
"""
from __future__ import annotations

import copy
import itertools
import uuid

ANN = ("bold", "italic", "strikethrough", "underline", "code")


def _retrieved_rich(rich):
    out = []
    for r in rich or []:
        if r.get("type", "text") == "text":
            t = r.get("text", {})
            link = (t.get("link") or {}).get("url")
            ann = {k: bool((r.get("annotations") or {}).get(k)) for k in ANN}
            ann["color"] = "default"
            out.append({"type": "text", "text": {"content": t.get("content", ""), "link": {"url": link} if link else None},
                        "annotations": ann, "plain_text": t.get("content", ""), "href": link})
        else:
            out.append(copy.deepcopy(r))
    return out


class FakeNotion:
    def __init__(self):
        self.blocks = {}          # id -> retrieved block (no children inline)
        self.kids = {}            # parent id -> [child ids]
        self.parent = {}          # block id -> parent id
        self.pages = {}           # id -> page object
        self.dbs = {}             # id -> database object
        self.uploads = {}         # upload id -> filename
        self.files = {}           # file url -> bytes (for downloads)
        self._clock = itertools.count(1)
        self.calls = []

    # ---------------------------------------------------------------- helpers
    def _now(self):
        n = next(self._clock)
        return "2026-09-23T%02d:%02d:00.000Z" % (10 + n // 60, n % 60)

    @staticmethod
    def _id():
        return str(uuid.uuid4())

    def _touch(self, block_id):
        cur = block_id
        while cur is not None:
            if cur in self.pages:
                self.pages[cur]["last_edited_time"] = self._now()
                return
            cur = self.parent.get(cur)

    def new_page(self, title, parent=None, blocks=()):
        pid = self._id()
        self.pages[pid] = {"object": "page", "id": pid, "url": "https://www.notion.so/%s" % pid.replace("-", ""),
                           "archived": False, "in_trash": False, "last_edited_time": self._now(),
                           "parent": {"page_id": parent} if parent else {"workspace": True},
                           "properties": {"title": {"id": "title", "type": "title",
                                                    "title": _retrieved_rich([{"type": "text", "text": {"content": title}}])}}}
        self.kids[pid] = []
        if parent:
            self._insert(parent, [{"type": "child_page", "child_page": {"title": title}}], ids=[pid])
        if blocks:
            self._insert(pid, list(blocks))
        return pid

    def _make_block(self, b, bid=None):
        t = b["type"]
        body = copy.deepcopy(b.get(t) or {})
        kids = body.pop("children", None) or []
        for k in ("rich_text", "caption"):
            if k in body:
                body[k] = _retrieved_rich(body[k])
        if t == "table_row":
            body["cells"] = [_retrieved_rich(c) for c in body.get("cells", [])]
        if t in ("image", "file", "pdf", "video", "audio") and body.get("type") == "file_upload":
            name = self.uploads[body["file_upload"]["id"]]
            fid = self._id()
            url = "https://files.fake/ws/%s/%s?sig=%d" % (fid, name, next(self._clock))
            self.files[url.split("?")[0]] = b"bytes-of-" + name.encode()
            body = {"type": "file", "file": {"url": url, "expiry_time": "x"}, "caption": body.get("caption", [])}
            if t != "image":
                body["name"] = name
        if t == "code":
            body.setdefault("language", "plain text")
        blk = {"object": "block", "id": bid or self._id(), "type": t, t: body,
               "has_children": False, "archived": False, "in_trash": False}
        return blk, kids

    def _insert(self, parent, blocks, position=None, ids=None):
        lst = self.kids.setdefault(parent, [])
        if position is None or position.get("type") == "end":
            at = len(lst)
        elif position["type"] == "start":
            at = 0
        else:
            at = lst.index(position["after_block"]["id"]) + 1
        new = []
        for k, b in enumerate(blocks):
            blk, kids = self._make_block(b, bid=(ids[k] if ids else None))
            self.blocks[blk["id"]] = blk
            self.parent[blk["id"]] = parent
            self.kids.setdefault(blk["id"], [])
            if kids:
                self._insert(blk["id"], kids)
            new.append(blk["id"])
        lst[at:at] = new
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = True
        self._touch(parent)
        return new, lst[at + len(new):]

    # ---------------------------------------------------------------- the API
    def api(self, method, path, body=None, raw_url=None, headers=None, data_bytes=None, retries=5):
        self.calls.append((method, path))
        if raw_url:                                    # file upload "send"
            return 200, {"status": "uploaded"}
        p = path.split("?")[0].strip("/").split("/")
        if p[0] == "pages" and len(p) == 1 and method == "POST":
            return self._create_page(body)
        if p[0] == "pages" and method == "GET":
            pg = self.pages.get(p[1])
            return (200, copy.deepcopy(pg)) if pg else (404, {"message": "not found"})
        if p[0] == "pages" and method == "PATCH":
            pg = self.pages.get(p[1])
            if not pg:
                return 404, {"message": "not found"}
            if "archived" in body:
                pg["archived"] = body["archived"]
                if body["archived"] and p[1] in self.parent:
                    self.kids[self.parent[p[1]]].remove(p[1])
            for name, val in (body.get("properties") or {}).items():
                pg["properties"][name] = self._prop(pg, name, val)
            pg["last_edited_time"] = self._now()
            return 200, copy.deepcopy(pg)
        if p[0] == "blocks" and len(p) == 3 and method == "GET":
            lst = self.kids.get(p[1], [])
            start = int(dict(x.split("=") for x in path.split("?")[1].split("&")).get("start_cursor", 0)) if "?" in path else 0
            page = lst[start:start + 100]
            more = start + 100 < len(lst)
            return 200, {"results": [self._out(b) for b in page], "has_more": more,
                         "next_cursor": str(start + 100) if more else None}
        if p[0] == "blocks" and len(p) == 3 and method == "PATCH":
            new, after = self._insert(p[1], body["children"], body.get("position"))
            return 200, {"results": [self._out(b) for b in new + after]}
        if p[0] == "blocks" and len(p) == 2 and method == "GET":
            b = self.blocks.get(p[1])
            return (200, self._out(p[1])) if b else (404, {"message": "not found"})
        if p[0] == "blocks" and method == "DELETE":
            b = self.blocks.get(p[1])
            if not b:
                return 404, {"message": "not found"}
            b["archived"] = True
            par = self.parent[p[1]]
            self.kids[par].remove(p[1])
            if par in self.blocks and not self.kids[par]:
                self.blocks[par]["has_children"] = False
            self._touch(par)
            return 200, self._out(p[1])
        if p[0] == "databases" and len(p) == 1 and method == "POST":
            return self._create_db(body)
        if p[0] == "databases" and len(p) == 2 and method == "GET":
            db = self.dbs.get(p[1])
            return (200, copy.deepcopy(db)) if db else (404, {"message": "not found"})
        if p[0] == "databases" and len(p) == 2 and method == "PATCH":
            db = self.dbs[p[1]]
            for name, spec in (body.get("properties") or {}).items():
                t = next(iter(spec))
                db["properties"][name] = {"id": name, "name": name, "type": t, t: {}}
            if "title" in body:
                db["title"] = _retrieved_rich(body["title"])
            if "description" in body:
                db["description"] = _retrieved_rich(body["description"])
            return 200, copy.deepcopy(db)
        if p[0] == "databases" and p[-1] == "query":
            rows = [copy.deepcopy(pg) for pg in self.pages.values()
                    if pg["parent"].get("database_id") == p[1] and not pg["archived"]]
            return 200, {"results": rows, "has_more": False}
        if p[0] == "file_uploads" and method == "POST":
            fid = self._id()
            self.uploads[fid] = body["filename"]
            return 200, {"id": fid, "upload_url": "https://upload.fake/%s" % fid}
        raise AssertionError("fake notion: unhandled %s %s" % (method, path))

    def _out(self, bid):
        b = copy.deepcopy(self.blocks[bid])
        b["has_children"] = bool(self.kids.get(bid))
        par = self.parent.get(bid)
        if par:
            b["parent"] = ({"type": "page_id", "page_id": par} if par in self.pages
                           else {"type": "block_id", "block_id": par})
        if b["type"] in ("child_page",) and bid in self.pages:
            b["child_page"]["title"] = "".join(r["plain_text"] for r in self.pages[bid]["properties"]["title"]["title"])
        return b

    def _prop(self, pg, name, val):
        t = next(iter(val))
        v = val[t]
        if t in ("title", "rich_text"):
            v = _retrieved_rich(v)
        return {"id": name, "type": t, t: v}

    def _create_page(self, body):
        par = body["parent"]
        if "database_id" in par:
            pid = self._id()
            db = self.dbs[par["database_id"]]
            props = {}
            for name, spec in db["properties"].items():
                t = spec["type"]
                props[name] = {"id": name, "type": t, t: [] if t in ("title", "rich_text", "multi_select") else (False if t == "checkbox" else None)}
            pg = {"object": "page", "id": pid, "url": "https://www.notion.so/%s" % pid.replace("-", ""),
                  "archived": False, "in_trash": False, "last_edited_time": self._now(),
                  "parent": {"database_id": par["database_id"]}, "properties": props}
            self.pages[pid] = pg
            self.kids[pid] = []
            for name, val in (body.get("properties") or {}).items():
                assert name in db["properties"], "property %r not in database" % name
                pg["properties"][name] = self._prop(pg, name, val)
            if body.get("children"):
                self._insert(pid, body["children"])
            return 200, copy.deepcopy(pg)
        title = "".join(r["text"]["content"] for r in body["properties"]["title"]["title"])
        pid = self.new_page(title, parent=par["page_id"], blocks=body.get("children") or ())
        return 200, copy.deepcopy(self.pages[pid])

    def _create_db(self, body):
        did = self._id()
        props = {n: {"id": n, "name": n, "type": next(iter(s)), next(iter(s)): {}} for n, s in body["properties"].items()}
        title = _retrieved_rich(body.get("title"))
        self.dbs[did] = {"object": "database", "id": did, "url": "https://www.notion.so/%s" % did.replace("-", ""),
                         "archived": False, "in_trash": False, "title": title,
                         "description": _retrieved_rich(body.get("description")), "properties": props,
                         "is_inline": body.get("is_inline", False)}
        self._insert(body["parent"]["page_id"], [{"type": "child_database",
                                                 "child_database": {"title": "".join(r["plain_text"] for r in title)}}],
                     ids=[did])
        return 200, copy.deepcopy(self.dbs[did])

    # ---------------------------------------------------------------- test helpers
    def texts(self, parent):
        """(type, plain text) of each child block — for assertions."""
        out = []
        for bid in self.kids.get(parent, []):
            b = self.blocks[bid]
            t = b["type"]
            body = b.get(t, {})
            txt = "".join(r.get("plain_text", "") for r in body.get("rich_text", [])) if isinstance(body, dict) else ""
            if t == "child_page":
                txt = self._out(bid)["child_page"]["title"]
            if t == "child_database":
                txt = body.get("title", "")
            out.append((t, txt))
        return out

    def ids_of(self, parent):
        return list(self.kids.get(parent, []))

    def edit_text(self, block_id, text):
        b = self.blocks[block_id]
        t = b["type"]
        b[t]["rich_text"] = _retrieved_rich([{"type": "text", "text": {"content": text}}])
        self._touch(block_id)
