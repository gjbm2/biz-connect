"""notion tree sync — keep local project files and a Notion hub in step, via a mapping file.

The mapping file
----------------
A folder is bound to Notion by a `notion.yaml` in it (committed). Each `map` entry ties a
local path (relative to that folder) to ONE Notion target:

    hub: https://www.notion.so/...Project-Elman-3e4e...     # default container for targets
    link_base: https://github.com/you/repo/blob/main/proj/   # optional: links out of the folder
    map:
      - path: thesis.md
        section: What does Josh think & care about?  # the blocks under that heading on the hub,
                                                     # up to the next heading of the same/higher level
      - path: insights.md
        page: https://www.notion.so/...Insights-3e4e...   # the whole page (title = file's `# H1`)
      - path: notes/muse.md
        page: new                                    # a new child page, created on first push
        in: thesis.md                                # container: another entry, a URL, or the hub
      - path: research                               # a FOLDER of notes: each .md becomes its
        pages: new                                   # own child page of a folder page (new or
        title: Research notes                        # a URL); new files publish on next push
      - path: log                                    # a FOLDER of .md files ...
        database: new                                # ... = a database: one row per file,
        title: Public log                            #     front-matter -> properties, body -> page
        schema: {date: date, verification: select, themes: multi_select, url: {type: url, name: URL}}
      - path: context/*.pdf                          # files (glob) uploaded into a section/page
        section: Background

The tool writes each target's `id` (page, heading block or database id) back into its entry, so
a mapping keeps working when the page or heading is renamed or moved in Notion. Database rows
are matched by a `Key` property (= the file's stem); image files pulled from Notion are recorded
under the entry's `media` (Notion file id -> local path). Nothing outside the mapped targets is
ever touched: other sections, child pages, databases and file blocks on the page stay as they are.

Sync semantics
--------------
Markdown is the working copy agents edit and git versions; Notion is where people read and edit.
Both directions are GUARDED: each item remembers its local file hash and remote content hash
from the last sync (`.bizconnect/state.json`, git-ignored), so

    push    sends local changes, refusing items changed in Notion since the last sync;
    pull    takes Notion changes, refusing items changed locally since the last sync;
    status  shows each item's verdict: in-sync | local-ahead | remote-ahead | CONFLICT |
            new-local | new-remote | local-deleted | remote-deleted | missing.

`--force` overrides the guard; `--prune` lets push archive database rows whose file was deleted.
Pushes are minimal: blocks are diffed by signature (a block's signature is its Markdown), so
unchanged Notion blocks — with their comments — are untouched, and blocks Markdown can't express
(child pages, databases, embeds, files ...) are never removed.

Verbs (under `bizconnect notion`)
-----
  link    <dir> <hub-url>                    create <dir>/notion.yaml bound to a hub page
  map     <path> --section H | --page URL|new | --pages [URL|new] | --database [URL|new]
                                             [--in X] [--title T]
                                             add a mapping entry (path relative to cwd)
  outline <page|url|dir>                     a page's headings / child pages / databases, with ids
  status  [path] [--deep]                    per-item verdicts
  push    [path] [--dry-run] [--force] [--prune]
  pull    [path] [--dry-run] [--force]
With no path, status/push/pull cover every notion.yaml in the repo.
"""
from __future__ import annotations

import glob as _glob
import hashlib
import json
import os
import posixpath
import re
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

from .. import config
from .. import notion_md as M
from . import notion as N

MANIFEST = "notion.yaml"
INDEX_NAMES = ("README.md", "index.md")
KEY_PROP = "Key"
WRITABLE = {"title", "rich_text", "select", "status", "multi_select", "date", "url", "email",
            "phone_number", "number", "checkbox"}
HEADINGS = ("heading_1", "heading_2", "heading_3")
STATE_DIR, STATE_FILE, STATE_KEY = ".bizconnect", "state.json", "notion_trees"
NOTION_URL = "https://www.notion.so/"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".bizconnect"}


class SyncError(Exception):
    pass


# ============================================================ small helpers
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def _sha_raw(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jsha(obj) -> str:
    return _sha(json.dumps(obj, sort_keys=True, ensure_ascii=False))


ACRONYMS = {"url": "URL", "id": "ID", "api": "API", "ai": "AI"}


def prettify(name):
    s = re.sub(r"[-_]+", " ", name or "").strip()
    s = " ".join(ACRONYMS.get(w.lower(), w) for w in s.split(" "))
    return (s[:1].upper() + s[1:]) if s else "Untitled"


def slug(s):
    return re.sub(r"[^0-9a-z]+", "_", (s or "").strip().lower()).strip("_") or "field"


def file_slug(s):
    return re.sub(r"[^0-9A-Za-z._-]+", "-", (s or "").strip()).strip("-.").lower()[:80] or "untitled"


def _nid(s):
    """Last Notion id in any form (dashed, bare, URL, '/<hex>') -> dashed id, or None."""
    m = re.findall(r"[0-9a-fA-F]{32}", (s or "").replace("-", ""))
    if not m:
        return None
    h = m[-1].lower()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def _url_ids(u):
    """(page id, block id or None) from a Notion URL like .../<page>#<block>."""
    base, _, frag = (u or "").partition("#")
    return _nid(base), (_nid(frag) if frag else None)


def _is_notion_url(u):
    return bool(re.match(r"^(https?://([\w-]+\.)?(notion\.so|notion\.site|notion\.com)/|/[0-9a-fA-F]{32})", u or ""))


def _hex(nid):
    return (nid or "").replace("-", "")


def _rich(s, limit=1900):
    s = s or ""
    return [{"type": "text", "text": {"content": s[i:i + limit]}} for i in range(0, len(s), limit)]


def _die(status, body, what):
    if status >= 300:
        raise SyncError("%s failed [%s]: %s" % (what, status, body.get("message") or body))


def _norm_heading(s):
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def heading_level(b):
    t = b.get("type", "")
    return int(t[-1]) if t in HEADINGS else 0


# ============================================================ YAML
def _yaml_rt():
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("ruamel.yaml missing — run via the launcher (scripts/bizconnect.py).")
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _yaml_load(text):
    from ruamel.yaml import YAML
    data = YAML(typ="safe").load(text or "") or {}
    if not isinstance(data, dict):
        raise SyncError("front-matter is not a mapping")
    return data


_PLAIN_OK = re.compile(r"^[A-Za-z0-9À-￿][^:#\[\]{},&*!|>'\"%@`\n\r\t]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", "y", "n"}


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if (not _PLAIN_OK.match(s) or s != s.strip() or s.lower() in _RESERVED
            or re.fullmatch(r"[-+]?[\d_.]+([eE][-+]?\d+)?", s) or ": " in s or " #" in s):
        return json.dumps(s, ensure_ascii=False)
    return s


def dump_front_matter(pairs):
    lines = []
    for k, v in pairs:
        if isinstance(v, list):
            lines.append("%s: [%s]" % (k, ", ".join(_yaml_scalar(x) for x in v)))
        else:
            lines.append("%s: %s" % (k, _yaml_scalar(v)))
    return "---\n" + "\n".join(lines) + "\n---\n"


# ============================================================ property values
def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)


def norm_value(v, ptype):
    """Local front-matter value -> canonical value for `ptype` (None = empty)."""
    if v is None:
        return None
    if ptype == "multi_select":
        items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
        items = [_iso(x).strip() for x in items if _iso(x).strip()]
        return items or None
    if isinstance(v, list):
        v = ", ".join(_iso(x) for x in v)
    if ptype == "checkbox":
        return True if (v is True or str(v).lower() in ("true", "yes", "1", "x")) else None
    if ptype == "number":
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return v
        try:
            f = float(str(v).replace(",", ""))
            return int(f) if f.is_integer() else f
        except ValueError:
            return None
    s = _iso(v).strip()
    return s or None


def remote_value(prop):
    """A Notion page property -> canonical value (None = empty or unsupported)."""
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return M.plain(v).strip() or None
    if t in ("select", "status"):
        return (v or {}).get("name") or None
    if t == "multi_select":
        return [o.get("name", "") for o in v or []] or None
    if t == "date":
        if not v or not v.get("start"):
            return None
        return v["start"] + ("/" + v["end"] if v.get("end") else "")
    if t == "number":
        return v
    if t == "checkbox":
        return True if v else None
    if t in ("url", "email", "phone_number"):
        return v or None
    return None


def prop_payload(ptype, v):
    """Canonical value -> Notion property payload (None clears)."""
    if ptype == "title":
        return {"title": _rich(v or "")}
    if ptype == "rich_text":
        return {"rich_text": _rich(v or "")}
    if ptype in ("select", "status"):
        return {ptype: {"name": str(v).replace(",", " ")[:100]} if v else None}
    if ptype == "multi_select":
        return {"multi_select": [{"name": str(x).replace(",", " ")[:100]} for x in (v or [])]}
    if ptype == "date":
        if not v:
            return {"date": None}
        start, _, end = str(v).partition("/")
        return {"date": {"start": start, **({"end": end} if end else {})}}
    if ptype == "number":
        return {"number": v}
    if ptype == "checkbox":
        return {"checkbox": bool(v)}
    if ptype in ("url", "email", "phone_number"):
        return {ptype: v or None}
    return None


def infer_type(values):
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            return "checkbox"
        if isinstance(v, (int, float)):
            return "number"
        if isinstance(v, (date, datetime)):
            return "date"
        if isinstance(v, list):
            return "multi_select"
        if re.match(r"^https?://\S+$", str(v)):
            return "url"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
            return "date"
        return "rich_text"
    return "rich_text"


# ============================================================ state
def _state_path(root):
    return Path(root) / STATE_DIR / STATE_FILE


def load_state(root):
    p = _state_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(root, full):
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(full, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    gi = p.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")


# ============================================================ remote reads
def fetch_blocks(block_id):
    """Children of `block_id`, with `_children` loaded where a managed block needs them."""
    out = N.get_children(block_id)
    for b in out:
        if b.get("has_children") and (M.is_managed(b) or b.get("type") == "paragraph"):
            b["_children"] = fetch_blocks(b["id"])
    return out


def get_page(pid):
    st, pg = N.api("GET", "/pages/%s" % pid)
    if st in (400, 404):
        return None
    _die(st, pg, "read page %s" % pid)
    return pg


def page_title(pg):
    for p in (pg or {}).get("properties", {}).values():
        if p.get("type") == "title":
            return M.plain(p.get("title")).strip()
    return ""


def _alive(obj):
    return bool(obj) and not obj.get("archived") and not obj.get("in_trash")


def _file_uuid(block):
    """Stable identity of a Notion-hosted file: the id segment of its (signed) URL."""
    t = block.get("type")
    body = block.get(t, {}) if isinstance(block.get(t), dict) else {}
    if body.get("type") != "file":
        return None
    parts = urllib.parse.urlparse(body.get("file", {}).get("url", "")).path.strip("/").split("/")
    return parts[-2] if len(parts) >= 2 else None


def shift_headings(blocks, level):
    """Local file headings -> levels nested under a section heading of `level` (so they can't
    end the section early). Level-3 sections can't hold headings: they become bold paragraphs."""
    out = []
    for b in blocks:
        t = b.get("type")
        if t in HEADINGS:
            L = int(t[-1])
            if level >= 3:
                rich = [dict(r, annotations={**(r.get("annotations") or {}), "bold": True}) for r in b[t]["rich_text"]]
                out.append({"type": "paragraph", "paragraph": {"rich_text": rich}})
                continue
            nl = min(3, max(level + 1, L + level - 1))
            out.append({"type": "heading_%d" % nl, "heading_%d" % nl: b[t]})
        else:
            out.append(b)
    return out


def unshift_headings(blocks, level):
    out = []
    for b in blocks:
        t = b.get("type")
        if t in HEADINGS and level < 3:
            L = max(2, int(t[-1]) - level + 1)
            nb = dict(b)
            nb.pop(t)
            nb["type"] = "heading_%d" % L
            nb["heading_%d" % L] = b[t]
            out.append(nb)
        else:
            out.append(b)
    return out


def lcs_pairs(a, b):
    """Index pairs (i, j) of a longest common subsequence of sequences a and b."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai, row, nxt = a[i], dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _image_srcs(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "image" and "_src" in b.get("image", {}):
            out.append(b["image"]["_src"])
        out.extend(_image_srcs(M.children_of(b)))
    return out


def _images(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "image":
            out.append(b)
        out.extend(_images(M.children_of(b)))
    return out


def verdict(st, local_exists, remote_exists, local_sha, remote_sha, same, remote_empty=False):
    """The sync decision for one item. `same` = local and remote canonical forms are equal."""
    if not remote_exists:
        if not local_exists:
            return None
        return "remote-deleted" if st and st.get("remote_sha") else "new-local"
    if not local_exists:
        return "local-deleted" if st and st.get("local_sha") else "new-remote"
    if same:
        return "in-sync"
    if not st:
        return "local-ahead" if remote_empty else "conflict"
    lc = local_sha != st.get("local_sha")
    rc = remote_sha != st.get("remote_sha")
    if lc and rc:
        return "conflict"
    if lc:
        return "local-ahead"
    if rc:
        return "remote-ahead"
    return "in-sync"          # neither side moved; any difference is a known normalisation


# ============================================================ the tree
class Tree:
    def __init__(self, root, manifest, dry=False, force=False, prune=False, deep=False, out=print):
        self.root = Path(root).resolve()
        self.mpath = Path(manifest).resolve()
        self.dir = self.mpath.parent
        rel = self.dir.relative_to(self.root).as_posix()
        self.key = "" if rel == "." else rel
        self.man = _yaml_rt().load(self.mpath.read_text(encoding="utf-8")) or {}
        if not self.man.get("hub"):
            raise SyncError("%s has no `hub:`" % self.mpath)
        self.hub = N.norm_id(str(self.man["hub"]))
        self.link_base = self.man.get("link_base") or None
        self.dry, self.force, self.prune, self.deep, self.out = dry, force, prune, deep, out
        self.full_state = load_state(self.root)
        self.state = self.full_state.setdefault(STATE_KEY, {}).setdefault(self.key or ".", {})
        self.items = {}
        self.row_ids = {}                 # row key -> page id
        self.created = set()
        self.results = []
        self._blocks = {}                 # page id -> fetched top-level blocks (cache)
        self._build()

    # ------------------------------------------------------------- build items
    def _build(self):
        entries = self.man.get("map") or []
        for e in entries:
            path = str(e.get("path") or "").strip().strip("/")
            if not path:
                raise SyncError("a map entry has no `path`")
            if "database" in e:
                folder = self.dir / path
                self._add(path + "/", "db", folder if folder.is_dir() else None, e)
                if folder.is_dir():
                    for f in sorted(folder.glob("*.md"), key=lambda x: x.name.lower()):
                        if f.name not in INDEX_NAMES:
                            self._add(path + "/" + f.name, "row", f, e)
            elif "pages" in e:                              # a folder of notes: one page each
                folder = self.dir / path
                idx = next((folder / n for n in INDEX_NAMES if (folder / n).is_file()), None)
                self._add(path + "/", "page", idx, e)
                self.items[path + "/"].update(container=True, fr=path + "/" + (idx.name if idx else "README.md"))
                if folder.is_dir():
                    for f in sorted(folder.glob("*.md"), key=lambda x: x.name.lower()):
                        if f.name not in INDEX_NAMES:
                            self._add(path + "/" + f.name, "page", f, e)
                            self.items[path + "/" + f.name]["note_of"] = path + "/"
                for rel in (e.get("ids") or {}):
                    if str(rel) not in self.items:
                        self._add(str(rel), "page", None, e)
                        self.items[str(rel)]["note_of"] = path + "/"
            elif re.search(r"[*?\[]", path):
                for f in sorted(_glob.glob(str(self.dir / path), recursive=True)):
                    fp = Path(f)
                    if fp.is_file():
                        self._add(fp.relative_to(self.dir).as_posix(), "file", fp, e)
                for rel in (e.get("ids") or {}):
                    if str(rel) not in self.items:
                        self._add(str(rel), "file", None, e)
            elif "section" in e:
                self._add(path, "section", self.dir / path, e)
            elif "page" in e:
                self._add(path, "page", self.dir / path, e)
            else:
                raise SyncError("map entry %r needs one of page / section / database" % path)

    def check_overlaps(self):
        """A page mapped whole can't also have mapped sections: its diff would eat them."""
        whole = {self._id(it): k for k, it in self.items.items() if it["kind"] == "page" and self._id(it)}
        for k, it in self.items.items():
            if it["kind"] in ("section", "file") and "section" in it["entry"]:
                try:
                    cid = self.container_id(it)
                except SyncError:
                    continue
                if cid in whole:
                    raise SyncError("%s is a section of the page mapped whole by %s — map the page "
                                    "whole OR by sections, not both" % (k, whole[cid]))

    def _add(self, key, kind, path, entry):
        if key in self.items:
            raise SyncError("%s is mapped twice" % key)
        self.items[key] = {"key": key, "kind": kind, "path": path, "entry": entry}

    def in_scope(self, key, scope):
        if not scope:
            return True
        s = scope.rstrip("/")
        return key == scope or key.rstrip("/") == s or key.startswith(s + "/")

    # ------------------------------------------------------------- ids
    def item_id(self, it):
        e = it["entry"]
        if it["kind"] == "row":
            return self.row_ids.get(it["key"])
        if it["kind"] == "file" or it.get("note_of"):
            v = (e.get("ids") or {}).get(it["key"])
            return N.norm_id(str(v)) if v else None
        if e.get("id"):
            return N.norm_id(str(e["id"]))
        sel = (e.get("pages") if it.get("container") else e.get("page")) if it["kind"] == "page" \
            else e.get("database") if it["kind"] == "db" else None
        if sel and str(sel).strip().lower() != "new":
            return N.norm_id(str(sel))
        return None

    def set_id(self, it, nid):
        if not nid.startswith("dry:"):
            if it.get("note_of"):
                ids = it["entry"].get("ids")
                if ids is None:
                    from ruamel.yaml.comments import CommentedMap
                    ids = CommentedMap()
                    it["entry"]["ids"] = ids
                ids[it["key"]] = nid
            else:
                it["entry"]["id"] = nid
        it["_id"] = nid

    def _id(self, it):
        return it.get("_id") or self.item_id(it)

    def container_id(self, it):
        if it.get("note_of"):
            return self._id(self.items[it["note_of"]])
        ref = it["entry"].get("in")
        if not ref:
            return self.hub
        ref = str(ref).strip().strip("/")
        tgt = self.items.get(ref)
        if tgt is not None:
            if tgt["kind"] != "page":
                raise SyncError("%s: `in: %s` must name a page entry" % (it["key"], ref))
            return self._id(tgt)
        return N.norm_id(ref)

    # ------------------------------------------------------------- remote pages
    def page_blocks(self, pid):
        if pid not in self._blocks:
            self._blocks[pid] = fetch_blocks(pid)
        return self._blocks[pid]

    def invalidate(self, pid):
        self._blocks.pop(pid, None)

    def find_section(self, it, create=False):
        """(page id, heading block, region blocks) for a section item, or (pid, None, None)."""
        pid = self.container_id(it)
        if not pid or pid.startswith("dry:"):
            return pid, None, None
        blocks = self.page_blocks(pid)
        e = it["entry"]
        hid = it.get("_id") or (N.norm_id(str(e["id"])) if e.get("id") else None)
        idx = None
        if hid:
            idx = next((i for i, b in enumerate(blocks) if b["id"] == hid), None)
        if idx is None:                     # not yet anchored, or the heading block was replaced
            want = _norm_heading(str(e.get("section", "")))
            hits = [i for i, b in enumerate(blocks) if b.get("type") in HEADINGS
                    and _norm_heading(M.plain(b[b["type"]].get("rich_text"))) == want]
            if len(hits) > 1:
                self.out("  ! %s: %d headings read %r — using the first" % (it["key"], len(hits), e.get("section")))
            idx = hits[0] if hits else None
            if idx is None and create and not self.dry:
                lvl = int(e.get("level") or 1)
                h = {"type": "heading_%d" % lvl, "heading_%d" % lvl: {"rich_text": _rich(str(e.get("section")))}}
                N.append_children(pid, [h])
                self.invalidate(pid)
                self.note("created", it["key"], "heading %r at the end of the page" % e.get("section"))
                return self.find_section(it)
            if idx is not None:
                if hid:
                    self.note("re-anchored", it["key"], "heading block replaced; matched %r by text" % e.get("section"))
                self.set_id(it, blocks[idx]["id"])
        if idx is None:
            return pid, None, None
        head = blocks[idx]
        lvl = heading_level(head)
        region = []
        for b in blocks[idx + 1:]:
            if b.get("type") in HEADINGS and heading_level(b) <= lvl:
                break
            region.append(b)
        return pid, head, region

    # ------------------------------------------------------------- links & media
    def _target_url(self, key):
        it = self.items.get(key)
        if not it:
            return None
        if it["kind"] == "section":
            pid = self.container_id(it)
            hid = self._id(it)
            return NOTION_URL + _hex(pid) + ("#" + _hex(hid) if hid else "") if pid and not pid.startswith("dry:") else None
        nid = self._id(it)
        return NOTION_URL + _hex(nid) if nid and not nid.startswith("dry:") else None

    def resolve(self, raw, from_rel):
        """Local link target -> URL for Notion (None = drop the link, keep its text)."""
        raw = (raw or "").strip()
        if not raw or raw.startswith("#"):
            return None
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw):
            return raw if re.match(r"^(https?|mailto):", raw) else None
        path = urllib.parse.unquote(raw.split("#")[0].split("?")[0])
        if not path or path.startswith("/"):
            return None
        t = posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), path))
        if not t.startswith(".."):
            for cand in (t, t + "/", t.rstrip("/") + "/"):
                u = self._target_url(cand)
                if u:
                    return u
        if self.link_base:                   # e.g. a GitHub blob URL of this folder; `..` climbs it
            return urllib.parse.urljoin(self.link_base.rstrip("/") + "/", urllib.parse.quote(t))
        return None

    def _key_by_url(self):
        m = {}
        for k, it in self.items.items():
            if it["kind"] == "section":
                hid = self._id(it)
                if hid:
                    m[("s", hid)] = k
            else:
                nid = self._id(it)
                if nid:
                    m[("p", nid)] = k
        for k, nid in self.row_ids.items():
            m[("p", nid)] = k
        return m

    def unresolve(self, url, from_rel):
        """Notion-side URL -> local form (a relative path when it points at a mapped item)."""
        if _is_notion_url(url):
            pid, bid = _url_ids(url)
            m = self._key_by_url()
            key = m.get(("s", bid)) if bid else None
            key = key or (m.get(("p", pid)) if pid else None)
            if key is not None:
                return self._rel_from(key.rstrip("/") + ("/" if key.endswith("/") else ""), from_rel)
            return url
        if self.link_base and url.startswith(self.link_base.rstrip("/") + "/"):
            t = urllib.parse.unquote(url[len(self.link_base.rstrip("/")) + 1:])
            return self._rel_from(t, from_rel)
        return url

    @staticmethod
    def _rel_from(target, from_rel):
        base = posixpath.dirname(from_rel) or "."
        trail = "/" if target.endswith("/") else ""
        r = posixpath.relpath(target.rstrip("/") or ".", base)
        return (r + trail) if r != "." else ("./" if trail else ".")

    def href_local(self, from_rel):
        def f(raw):
            u = self.resolve(raw, from_rel)
            return self.unresolve(u, from_rel) if u else None
        return f

    def href_remote(self, from_rel):
        return lambda u: self.unresolve(u, from_rel)

    def _media_map(self, it):
        return dict(it["entry"].get("media") or {})

    def media_local(self, from_rel):
        def f(src, _block):
            if re.match(r"^https?://", src or ""):
                return src
            t = posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), urllib.parse.unquote(src)))
            return self._rel_from(t, from_rel)
        return f

    def media_remote(self, it, from_rel):
        mm = self._media_map(it)

        def f(src, block):
            body = block.get("image", {})
            if body.get("type") == "external":
                return src
            uid = _file_uuid(block)
            if uid and uid in mm:
                return self._rel_from(str(mm[uid]), from_rel)
            return "notion-file:%s" % (uid or "?")
        return f

    def _download_images(self, it, blocks, from_rel):
        """Pull: fetch Notion-hosted images not yet on disk into `media/<file-stem>/`."""
        mm = it["entry"].get("media")
        for b in _images(blocks):
            uid = _file_uuid(b)
            if not uid or (mm and uid in mm):
                continue
            url = b["image"]["file"]["url"]
            name = file_slug(urllib.parse.unquote(url.split("?")[0].rstrip("/").split("/")[-1])) or "image.png"
            stem = Path(from_rel).stem
            folder = posixpath.join(posixpath.dirname(from_rel), "media", stem)
            rel = posixpath.join(folder, name)
            n = 1
            while (self.dir / rel).exists():
                n += 1
                base, dot, ext = name.rpartition(".")
                rel = posixpath.join(folder, "%s-%d.%s" % (base or name, n, ext) if dot else "%s-%d" % (name, n))
            if self.dry:
                continue
            (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / rel).write_bytes(N._fetch_bytes(url))
            if mm is None:
                from ruamel.yaml.comments import CommentedMap
                mm = CommentedMap()
                it["entry"]["media"] = mm
            mm[uid] = rel

    def _learn_media(self, it, remote_blocks, local_blocks, from_rel):
        """Push: pair freshly uploaded Notion images with the local files they came from."""
        mm = it["entry"].get("media")
        known = set(mm or {})
        rimgs = [b for b in _images(remote_blocks) if _file_uuid(b)]
        limgs = [b for b in _images(local_blocks) if "_src" in b.get("image", {})]
        for rb, lb in zip(rimgs, limgs):
            uid = _file_uuid(rb)
            if uid in known:
                continue
            t = posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), urllib.parse.unquote(lb["image"]["_src"])))
            if mm is None:
                from ruamel.yaml.comments import CommentedMap
                mm = CommentedMap()
                it["entry"]["media"] = mm
            mm[uid] = t

    # ------------------------------------------------------------- local docs
    def local_doc(self, it):
        p = it.get("path")
        if not p or not p.is_file():
            return {"exists": False, "title": None, "blocks": [], "fm": None, "sha": "", "has_h1": False}
        raw = p.read_bytes()
        fm, rest = M.split_front_matter(raw.decode("utf-8", errors="replace"))
        title, body = M.split_title(rest)
        return {"exists": True, "title": title, "blocks": M.parse_blocks(body), "fm": fm,
                "sha": _sha(raw), "has_h1": title is not None}

    def render_local(self, blocks, fr):
        return M.render_blocks(blocks, self.href_local(fr), self.media_local(fr))

    def render_remote(self, it, blocks, fr, markers=False, exclude=()):
        keep = [b for b in blocks if b["id"] not in exclude] if exclude else blocks
        if not markers:
            keep = [b for b in keep if M.is_managed(b)]
        return M.render_blocks(keep, self.href_remote(fr), self.media_remote(it, fr), markers=markers)

    def _attached_ids(self, pid, heading_id=None):
        """Blocks placed by `files` entries on this page/section — never diffed as Markdown."""
        out = set()
        for it in self.items.values():
            if it["kind"] == "file":
                bid = self.item_id(it)
                if bid:
                    out.add(N.norm_id(str(bid)))
        return out

    # ------------------------------------------------------------- page/section views
    @staticmethod
    def fr(it):
        """The file an item's links are relative to (a folder page's is its README)."""
        return it.get("fr") or it["key"]

    @staticmethod
    def body_less(it):
        """A folder page with no README: it exists in Notion, but has no body to sync."""
        return bool(it.get("container")) and not (it.get("path") and it["path"].is_file())

    def doc_view(self, it):
        """Everything push/pull/status need to judge one page or section item."""
        fr = self.fr(it)
        ld = self.local_doc(it)
        v = {"it": it, "local": ld, "remote_exists": False, "remote_canon": None, "blocks": None,
             "pid": None, "heading": None, "level": 0}
        if it["kind"] == "section":
            pid, head, region = self.find_section(it)
            v["pid"] = pid
            if head is None:
                v["missing"] = True
                return v
            lvl = heading_level(head)
            v.update(heading=head, level=lvl, blocks=region, remote_exists=True)
            local_blocks = shift_headings(ld["blocks"], lvl)
            v["local_blocks"] = local_blocks
            v["local_canon"] = self.render_local(local_blocks, fr)
            v["remote_canon"] = self.render_remote(it, region, fr, exclude=self._attached_ids(pid))
            v["remote_empty"] = not v["remote_canon"].strip()
            return v
        nid = self._id(it)
        v["pid"] = nid
        v["local_blocks"] = ld["blocks"]
        manage_title = ld["title"] is not None
        lc = self.render_local(ld["blocks"], fr)
        v["local_canon"] = (("# %s\n\n" % ld["title"]) if manage_title else "") + lc
        if not nid or nid.startswith("dry:"):
            return v
        pg = get_page(nid)
        if not _alive(pg):
            return v
        blocks = self.page_blocks(nid)
        rtitle = page_title(pg)
        rc = self.render_remote(it, blocks, fr, exclude=self._attached_ids(nid))
        v.update(remote_exists=True, blocks=blocks, page=pg, remote_title=rtitle,
                 remote_canon=(("# %s\n\n" % rtitle) if manage_title else "") + rc,
                 remote_empty=not rc.strip())
        return v

    def judge(self, v):
        it = v["it"]
        if v.get("missing"):
            return "missing"
        ld = v["local"]
        if it["key"] in self.created:
            return "local-ahead" if ld["exists"] else "in-sync"
        same = v["remote_canon"] is not None and v["remote_canon"] == v["local_canon"]
        return verdict(self.state.get(it["key"]), ld["exists"], v["remote_exists"], ld["sha"],
                       _sha(v["remote_canon"]) if v["remote_canon"] is not None else None, same,
                       remote_empty=v.get("remote_empty", False))

    # ------------------------------------------------------------- recording
    def note(self, verdict_, key, msg=""):
        self.results.append((verdict_, key, msg))

    def record(self, key, **kw):
        if self.dry:
            return
        cur = dict(self.state.get(key) or {})
        cur.update(kw)
        cur["synced_at"] = _now()
        self.state[key] = cur

    # ================================================================ PUSH
    def push(self, scope=None):
        hub = get_page(self.hub)
        if not _alive(hub):
            raise SyncError("hub page %s is missing or archived — is it shared with the integration?" % self.hub)
        self._ensure_targets(scope)
        for it in list(self.items.values()):
            if it["kind"] == "db" and self._db_in_scope(it, scope):
                self._push_collection(it, scope)
        for it in list(self.items.values()):
            if it["kind"] in ("page", "section") and self.in_scope(it["key"], scope) and not self.body_less(it):
                self._push_doc(it)
        for it in list(self.items.values()):
            if it["kind"] == "file" and self.in_scope(it["key"], scope):
                self._push_file(it)
        return self.results

    def _db_in_scope(self, it, scope):
        return self.in_scope(it["key"], scope) or bool(scope and scope.startswith(it["key"]))

    def _ensure_targets(self, scope):
        """Create `page: new` pages and `database: new` databases (containers first)."""
        pending = [it for it in self.items.values() if it["kind"] in ("page", "db")
                   and not self._id(it) and (self.in_scope(it["key"], scope) or self._db_in_scope(it, scope)
                                             or self._is_container_of_scope(it, scope))]
        for _round in range(len(pending) + 1):
            progress = False
            for it in list(pending):
                try:
                    parent = self.container_id(it)
                except SyncError:
                    continue
                if not parent:
                    continue
                pending.remove(it)
                progress = True
                if it["kind"] == "page" and not it.get("container") and \
                        not (it.get("path") and it["path"].is_file()):
                    self.note("skipped", it["key"], "no local file yet — nothing to create")
                    continue
                title = self._title_for(it)
                if parent.startswith("dry:"):
                    self.set_id(it, "dry:" + it["key"])
                    self.created.add(it["key"])
                    continue
                adopted = self._adopt(parent, it["kind"], title)
                if adopted:
                    self.set_id(it, adopted)
                    self.note("adopted", it["key"], "existing %s %r" % ("database" if it["kind"] == "db" else "page", title))
                    continue
                if self.dry:
                    self.set_id(it, "dry:" + it["key"])
                    self.created.add(it["key"])
                    continue
                if it["kind"] == "page":
                    st, pg = N.api("POST", "/pages", body={"parent": {"page_id": parent},
                                                           "properties": {"title": {"title": _rich(title)}}})
                    _die(st, pg, "create page %s" % it["key"])
                else:
                    st, pg = N.api("POST", "/databases", body=self._db_create_body(it, parent, title))
                    _die(st, pg, "create database %s" % it["key"])
                self.set_id(it, pg["id"])
                self.created.add(it["key"])
                self.invalidate(parent)
                self.note("created", it["key"], "%s %r" % ("database" if it["kind"] == "db" else "page", title))
            if not progress:
                break
        for it in pending:
            self.note("error", it["key"], "container `in: %s` could not be resolved" % it["entry"].get("in"))

    def _is_container_of_scope(self, it, scope):
        if not scope:
            return False
        for other in self.items.values():
            if self.in_scope(other["key"], scope) and (
                    other.get("note_of") == it["key"]
                    or str(other["entry"].get("in") or "").strip("/") == it["key"].strip("/")):
                return True
        return False

    def _title_for(self, it):
        e = it["entry"]
        if e.get("title") and not it.get("note_of"):
            return str(e["title"])
        if it["kind"] == "page":
            ld = self.local_doc(it)
            if ld["title"]:
                return ld["title"]
            return prettify(Path(it["key"].rstrip("/")).stem)
        idx = next((it["path"] / n for n in INDEX_NAMES if it.get("path") and (it["path"] / n).is_file()), None)
        if idx:
            t, _ = M.split_title(M.split_front_matter(idx.read_text(encoding="utf-8"))[1])
            if t:
                return t
        return prettify(posixpath.basename(it["key"].rstrip("/")))

    def _adopt(self, parent_id, kind, title):
        want = "child_page" if kind == "page" else "child_database"
        mapped = {self._id(x) for x in self.items.values()}
        for b in self.page_blocks(parent_id):
            if b.get("type") == want and b["id"] not in mapped and \
                    (b.get(want, {}).get("title") or "").strip() == title.strip():
                return b["id"]
        return None

    def _push_doc(self, it):
        k = it["key"]
        if it["kind"] == "section" and not it["entry"].get("id") and not self.dry:
            self.find_section(it, create=bool(it.get("path") and it["path"].is_file()))
        v = self.doc_view(it)
        vd = self.judge(v)
        ld = v["local"]
        if vd is None:
            return
        if vd == "missing":
            self.note(vd, k, "heading %r not found on the page" % it["entry"].get("section"))
            return
        if vd == "in-sync":
            s = self.state.get(k)
            if not s or s.get("local_sha") != ld["sha"] or s.get("remote_sha") != _sha(v["remote_canon"] or ""):
                self.record(k, local_sha=ld["sha"], remote_sha=_sha(v["remote_canon"] or ""))
            self.note(vd, k)
            return
        if vd == "new-local":
            if self.dry:
                self.note("would-push", k, "new-local")
            else:
                self.note("error", k, "target page not found — is it shared with the integration?")
            return
        if vd == "local-deleted" and it.get("note_of") and self.prune:
            if not self.dry:
                st, r = N.api("PATCH", "/pages/%s" % v["pid"], body={"archived": True})
                _die(st, r, "archive page %s" % k)
                del it["entry"]["ids"][k]
                self.state.pop(k, None)
            self.note("archived", k, "local file deleted")
            return
        if vd in ("new-remote", "local-deleted"):
            self.note(vd, k, "no local file — pull to fetch it" + (" (push --prune archives it)" if it.get("note_of") else ""))
            return
        if vd == "remote-deleted":
            self.note(vd, k, "gone from Notion (fix the mapping, or push --force after clearing its id)")
            return
        if vd in ("remote-ahead", "conflict") and not self.force:
            self.note(vd, k, "changed in Notion since the last sync — pull first (or push --force)")
            return
        if self.dry:
            self.note("would-push", k, vd)
            return
        pid = v["pid"]
        if it["kind"] == "page" and ld["title"] is not None and v.get("remote_title") != ld["title"]:
            st, r = N.api("PATCH", "/pages/%s" % pid, body={"properties": {"title": {"title": _rich(ld["title"])}}})
            _die(st, r, "set title %s" % k)
        start = v["heading"]["id"] if it["kind"] == "section" else None
        self.apply_body(pid, v["blocks"] or [], v["local_blocks"], self.fr(it), it, start_after=start,
                        exclude=self._attached_ids(pid))
        self.invalidate(pid)
        v2 = self.doc_view(it)
        self._learn_media(it, v2["blocks"] or [], v2["local_blocks"], self.fr(it))
        v2 = self.doc_view(it)
        if v2["remote_canon"] != v2["local_canon"]:
            self.out("  ! %s: Notion normalised the content differently than expected" % k)
        self.record(k, local_sha=ld["sha"], remote_sha=_sha(v2["remote_canon"] or ""))
        self.note("pushed", k, vd)

    def apply_body(self, parent_id, remote_blocks, local_blocks, fr, it, start_after=None, exclude=()):
        """Minimal block diff: keep matching blocks, insert new ones, delete removed ones.
        Blocks Markdown can't express, and `exclude`d ids, are never touched."""
        rhref, lhref = self.href_remote(fr), self.href_local(fr)
        rmedia, lmedia = self.media_remote(it, fr), self.media_local(fr)
        managed = [b for b in remote_blocks if M.is_managed(b) and b["id"] not in exclude]
        rs = [M.signature(b, rhref, rmedia) for b in managed]
        ls = [M.signature(b, lhref, lmedia) for b in local_blocks]
        pairs = lcs_pairs(rs, ls)
        matched_r = {i for i, _ in pairs}
        by_l = {j: i for i, j in pairs}
        groups, anchor, cur = [], start_after, None
        for j, b in enumerate(local_blocks):
            if j in by_l:
                anchor, cur = managed[by_l[j]]["id"], None
                continue
            if cur is None:
                cur = [anchor, []]
                groups.append(cur)
            cur[1].append(b)
        for anchor_id, blocks in groups:
            api_blocks = [x for x in (self.to_api(b, fr) for b in blocks) if x]
            if api_blocks:
                N.append_children(parent_id, api_blocks, after=anchor_id, at_start=anchor_id is None)
        for i, b in enumerate(managed):
            if i not in matched_r:
                N.delete_block(b["id"])

    def to_api(self, block, fr):
        """Parsed block -> API payload: resolve links, chunk long text, upload local images."""
        t = block["type"]
        body = {k: v for k, v in block[t].items()}
        if "rich_text" in body:
            body["rich_text"] = self._api_rich(body["rich_text"], fr)
        if "caption" in body:
            body["caption"] = self._api_rich(body.get("caption") or [], fr)
        if t == "table":
            body["children"] = [{"type": "table_row", "table_row": {
                "cells": [self._api_rich(c, fr) for c in r["table_row"]["cells"]]}}
                for r in body.get("children", [])]
        elif body.get("children"):
            body["children"] = [x for x in (self.to_api(c, fr) for c in body["children"]) if x]
        if t == "image" and "_src" in body:
            src = body.pop("_src")
            path = self.dir / posixpath.normpath(posixpath.join(posixpath.dirname(fr), urllib.parse.unquote(src)))
            if path.is_file():
                body.update({"type": "file_upload", "file_upload": {"id": N.upload_file(path)}})
            else:
                url = self.resolve(src, fr)
                if not url:
                    self.out("  ! image %s not found — skipped" % src)
                    return None
                body.update({"type": "external", "external": {"url": url}})
        return {"type": t, t: body}

    def _api_rich(self, rich, fr):
        out = []
        for r in rich:
            content, link, ann = M._seg_parts(r)
            url = self.resolve(link, fr) if link else None
            for i in range(0, len(content), 1900):
                out.append(M.seg(content[i:i + 1900], url, **dict(zip(M.ANN_KEYS, ann))))
        if len(out) > 100:                     # API cap: fold the tail into plain text
            tail = "".join(M._seg_parts(r)[0] for r in out[99:])
            out = out[:99] + [M.seg(tail[:1900])]
        return out

    # ------------------------------------------------------------- collections
    def _get_db(self, did):
        st, db = N.api("GET", "/databases/%s" % did)
        if st in (400, 404):
            return None
        _die(st, db, "read database %s" % did)
        return db

    def _local_rows(self, db_key):
        rows = {}
        for k, it in self.items.items():
            if it["kind"] != "row" or not k.startswith(db_key):
                continue
            raw = it["path"].read_bytes()
            fm, body = M.split_front_matter(raw.decode("utf-8", errors="replace"))
            try:
                values = _yaml_load(fm) if fm is not None else {}
            except Exception as e:                           # noqa: BLE001
                self.note("error", k, "front-matter: %s" % e)
                continue
            rows[k] = {"item": it, "values": values, "blocks": M.parse_blocks(body), "sha": _sha(raw),
                       "stem": it["path"].stem}
        return rows

    def schema(self, it, remote_props, local_rows):
        """{key: (property name, type)} and the title key. Remote types win, then the entry's
        `schema`, then inference from the local values."""
        spec = {}
        for k, v in (it["entry"].get("schema") or {}).items():
            if isinstance(v, str):
                spec[str(k)] = {"type": v}
            elif isinstance(v, dict):
                spec[str(k)] = {"type": str(v.get("type", "rich_text")), "name": v.get("name")}
        tk = next((k for k, v in spec.items() if v["type"] == "title"), "title")
        remote_props = remote_props or {}
        rtitle = next((n for n, p in remote_props.items() if p.get("type") == "title"), None)
        out = {tk: (rtitle or spec.get(tk, {}).get("name") or prettify(tk), "title")}
        by_name = {n: p.get("type") for n, p in remote_props.items()}
        taken = {out[tk][0], KEY_PROP}
        for k, v in spec.items():
            if k == tk:
                continue
            name = str(v.get("name") or prettify(k))
            out[k] = (name, by_name.get(name) or v["type"])
            taken.add(name)
        for name, t in by_name.items():
            if name in taken or t == "title":
                continue
            out.setdefault(slug(name), (name, t))
            taken.add(name)
        extra = []
        for r in local_rows.values():
            for k in r["values"]:
                if k not in out and k not in extra:
                    extra.append(k)
        for k in extra:
            name = prettify(k)
            if name == KEY_PROP:
                raise SyncError("front-matter key %r collides with the reserved %r property" % (k, KEY_PROP))
            out[k] = (name, infer_type([r["values"].get(k) for r in local_rows.values()]))
        return out, tk

    def _db_create_body(self, it, parent_id, title):
        sch, tk = self.schema(it, None, self._local_rows(it["key"]))
        props = {sch[tk][0]: {"title": {}}, KEY_PROP: {"rich_text": {}}}
        for k, (name, t) in sch.items():
            if k != tk and t in WRITABLE and t != "status":
                props[name] = {t: {}}
        body = {"parent": {"type": "page_id", "page_id": parent_id}, "is_inline": True,
                "title": _rich(title), "properties": props}
        desc = self._db_description(it)
        if desc:
            body["description"] = _rich(desc)
        return body

    def _db_description(self, it):
        e = it["entry"]
        if e.get("description"):
            return str(e["description"])[:1900]
        idx = next((it["path"] / n for n in INDEX_NAMES if it.get("path") and (it["path"] / n).is_file()), None)
        if not idx:
            return ""
        _t, body = M.split_title(M.split_front_matter(idx.read_text(encoding="utf-8"))[1])
        paras = [b for b in M.parse_blocks(body) if b["type"] == "paragraph"]
        return M.plain(paras[0]["paragraph"]["rich_text"])[:1900] if paras else ""

    def _query_rows(self, did):
        out, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            st, resp = N.api("POST", "/databases/%s/query" % did, body=body)
            _die(st, resp, "query database %s" % did)
            out.extend(resp.get("results", []))
            if not resp.get("has_more"):
                return out
            cursor = resp.get("next_cursor")

    @staticmethod
    def _row_key(pg):
        return M.plain((pg.get("properties", {}).get(KEY_PROP) or {}).get("rich_text")).strip()

    def _row_props(self, pg, sch):
        props = pg.get("properties", {})
        vals = {}
        for k, (name, _t) in sch.items():
            p = props.get(name)
            if p is not None:
                v = remote_value(p)
                if v is not None:
                    vals[k] = v
        return vals

    def _local_row_values(self, row, sch, tk):
        vals = {}
        for k, (_name, t) in sch.items():
            if k in row["values"]:
                v = norm_value(row["values"][k], t)
                if v is not None:
                    vals[k] = v
        vals.setdefault(tk, prettify(row["stem"]))
        return vals

    @staticmethod
    def row_canon(vals, body_md):
        return json.dumps({"props": vals, "body": body_md}, sort_keys=True, ensure_ascii=False)

    def collection(self, it):
        """(db id, db, local rows, remote rows by key, keyless remote rows, schema, title key)."""
        did = self._id(it)
        rows = self._local_rows(it["key"])
        if not did or did.startswith("dry:"):
            sch, tk = self.schema(it, None, rows)
            return did, None, rows, {}, [], sch, tk
        db = self._get_db(did)
        if not _alive(db):
            sch, tk = self.schema(it, None, rows)
            return did, None, rows, {}, [], sch, tk
        sch, tk = self.schema(it, db.get("properties"), rows)
        by_key, keyless = {}, []
        for pg in self._query_rows(did):
            key = self._row_key(pg)
            if key:
                rk = it["key"] + key + ".md"
                by_key[rk] = pg
                self.row_ids[rk] = pg["id"]
            else:
                keyless.append(pg)
        return did, db, rows, by_key, keyless, sch, tk

    def _remote_row(self, it, pg, rk, sch, fetch):
        vals = self._row_props(pg, sch)
        s = self.state.get(rk)
        if not fetch and s and s.get("remote_edited") == pg.get("last_edited_time") \
                and s.get("remote_props") == _jsha(vals):
            return vals, None, None                  # unchanged since last sync (cheap path)
        blocks = fetch_blocks(pg["id"])
        return vals, blocks, self.row_canon(vals, self.render_remote(it, blocks, rk))

    def _local_row_canon(self, row, sch, tk, rk):
        vals = self._local_row_values(row, sch, tk)
        return vals, self.row_canon(vals, self.render_local(row["blocks"], rk))

    def _record_row(self, rk, row_sha, pg, vals, remote_canon):
        self.record(rk, local_sha=row_sha, remote_sha=_sha(remote_canon),
                    remote_edited=pg.get("last_edited_time"), remote_props=_jsha(vals))

    def _push_collection(self, it, scope):
        k = it["key"]
        if not it.get("path"):
            self.note("local-deleted" if self._id(it) else "missing", k, "folder not found")
            return
        did, db, rows, by_key, keyless, sch, tk = self.collection(it)
        if not did:
            return
        if did.startswith("dry:"):
            for rk in rows:
                if self.in_scope(rk, scope) or self._db_in_scope(it, scope):
                    self.note("would-push", rk, "new-local")
            return
        if db is None:
            self.note("remote-deleted", k, "database gone from Notion (clear its id to recreate)")
            return
        have = set((db.get("properties") or {}).keys())
        missing = {name: {t: {}} for key, (name, t) in sch.items()
                   if name not in have and t in WRITABLE and t not in ("title", "status")}
        if KEY_PROP not in have:
            missing[KEY_PROP] = {"rich_text": {}}
        patch = {}
        if missing:
            patch["properties"] = missing
        want_title = self._title_for(it)
        if it["entry"].get("title") and M.plain(db.get("title")) != want_title:
            patch["title"] = _rich(want_title)
        desc = self._db_description(it)
        if desc and M.plain(db.get("description")) != desc:
            patch["description"] = _rich(desc)
        if patch and not self.dry:
            st, r = N.api("PATCH", "/databases/%s" % did, body=patch)
            _die(st, r, "update database %s" % k)
            sch, tk = self.schema(it, r.get("properties"), rows)
        todo = []
        for rk, row in rows.items():
            if not (self.in_scope(rk, scope) or self._db_in_scope(it, scope) or not scope):
                continue
            pg = by_key.get(rk)
            if pg is None:
                s = self.state.get(rk)
                if s and s.get("remote_sha") and not self.force:
                    self.note("remote-deleted", rk, "row archived in Notion (push --force recreates it)")
                    continue
                if self.dry:
                    self.note("would-push", rk, "new-local")
                    continue
                vals = self._local_row_values(row, sch, tk)
                st, pgn = N.api("POST", "/pages", body={"parent": {"database_id": did},
                                                        "properties": self._props_body(vals, sch, tk, row["stem"], full=False)})
                _die(st, pgn, "create row %s" % rk)
                self.row_ids[rk] = pgn["id"]
                self.created.add(rk)
                pg = pgn
            todo.append((rk, row, pg))
        for rk, row, pg in todo:
            self._push_row(it, rk, row, pg, sch, tk)
        for rk, pg in by_key.items():
            if rk in rows or not (self.in_scope(rk, scope) or not scope or self._db_in_scope(it, scope)):
                continue
            if self.state.get(rk) and self.prune and not self.dry:
                st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"archived": True})
                _die(st, r, "archive row %s" % rk)
                self.state.pop(rk, None)
                self.note("archived", rk, "local file deleted")
            elif self.state.get(rk):
                self.note("local-deleted", rk, "kept in Notion (push --prune archives it)")
            else:
                self.note("new-remote", rk, "only in Notion — pull to fetch it")
        for pg in keyless:
            self.note("new-remote", k + "?", "row %r has no Key — pull to fetch it" % page_title(pg))

    def _props_body(self, vals, sch, tk, stem, full=True):
        props = {}
        for key, (name, t) in sch.items():
            if t not in WRITABLE:
                continue
            if key in vals:
                payload = prop_payload(t, vals[key])
            elif full and key != tk and t != "status":
                payload = prop_payload(t, None)                # absent locally -> clear
            else:
                continue
            if payload is not None:
                props[name] = payload
        props[KEY_PROP] = {"rich_text": _rich(stem)}
        return props

    def _push_row(self, it, rk, row, pg, sch, tk):
        lvals, local_canon = self._local_row_canon(row, sch, tk, rk)
        if rk in self.created:
            vd, blocks = "new-local", []
        else:
            rvals, blocks, remote_canon = self._remote_row(it, pg, rk, sch, fetch=True)
            vd = verdict(self.state.get(rk), True, True, row["sha"], _sha(remote_canon),
                         remote_canon == local_canon)
            if vd == "in-sync":
                s = self.state.get(rk)
                if not s or s.get("local_sha") != row["sha"] or s.get("remote_sha") != _sha(remote_canon):
                    self._record_row(rk, row["sha"], pg, rvals, remote_canon)
                self.note(vd, rk)
                return
        if vd in ("remote-ahead", "conflict") and not self.force:
            self.note(vd, rk, "changed in Notion since the last sync — pull first (or push --force)")
            return
        if self.dry:
            self.note("would-push", rk, vd)
            return
        st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"properties": self._props_body(lvals, sch, tk, row["stem"])})
        _die(st, r, "update row %s" % rk)
        self.apply_body(pg["id"], blocks or [], row["blocks"], rk, it)
        st, pg2 = N.api("GET", "/pages/%s" % pg["id"])
        _die(st, pg2, "re-read row %s" % rk)
        rvals, rblocks, remote_now = self._remote_row(it, pg2, rk, sch, fetch=True)
        self._learn_media(it, rblocks, row["blocks"], rk)
        rvals, rblocks, remote_now = self._remote_row(it, pg2, rk, sch, fetch=True)
        if remote_now != local_canon:
            self.out("  ! %s: Notion normalised the row differently than expected" % rk)
        self._record_row(rk, row["sha"], pg2, rvals, remote_now)
        self.note("pushed", rk, vd)

    # ------------------------------------------------------------- files
    def _file_target(self, it):
        """(page id, anchor block id to append after or None for the page end)."""
        e = it["entry"]
        if "section" in e:
            pid, head, region = self.find_section(it, create=not self.dry)
            if head is None:
                return pid, "missing"
            last = region[-1]["id"] if region else head["id"]
            return pid, last
        return self.container_id(it), None

    def _push_file(self, it):
        k = it["key"]
        e = it["entry"]
        ids = e.get("ids")
        fid = N.norm_id(str(ids[k])) if ids and ids.get(k) else None
        if not it.get("path"):
            if fid and self.prune and not self.dry:
                N.delete_block(fid)
                del ids[k]
                self.state.pop(k, None)
                self.note("archived", k, "local file deleted")
            elif fid:
                self.note("local-deleted", k, "kept in Notion (push --prune removes it)")
            return
        data = it["path"].read_bytes()
        sha = _sha_raw(data)
        exists = False
        if fid:
            st, b = N.api("GET", "/blocks/%s" % fid)
            exists = st < 300 and _alive(b)
        s = self.state.get(k)
        if exists and (not s or s.get("local_sha") == sha):
            if not s:
                self.record(k, local_sha=sha)
            self.note("in-sync", k)
            return
        vd = "local-ahead" if exists else "new-local"
        if self.dry:
            self.note("would-push", k, vd)
            return
        if len(data) > 20 * 1024 * 1024:
            self.note("error", k, "over the 20MB single-part upload limit")
            return
        pid, anchor = self._file_target(it)
        if anchor == "missing" or not pid:
            self.note("missing", k, "target section/page not found")
            return
        bt, _ = N.infer_type(it["path"])
        new = N.append_children(pid, [N.media_block(bt, N.upload_file(it["path"]), it["path"].name)],
                                after=anchor)[0]
        self.invalidate(pid)
        if exists:
            N.delete_block(fid)
        if ids is None:
            from ruamel.yaml.comments import CommentedMap
            ids = CommentedMap()
            e["ids"] = ids
        ids[k] = new
        self.record(k, local_sha=sha)
        self.note("pushed", k, vd)

    # ================================================================ PULL
    def pull(self, scope=None):
        for it in list(self.items.values()):
            if it["kind"] in ("page", "section") and self.in_scope(it["key"], scope) and not self.body_less(it):
                self._pull_doc(it)
        for it in list(self.items.values()):
            if it.get("container") and self._db_in_scope(it, scope):
                self._pull_new_notes(it)
        for it in list(self.items.values()):
            if it["kind"] == "db" and self._db_in_scope(it, scope):
                self._pull_collection(it, scope)
        return self.results

    def _write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return _sha(data)

    def _doc_text(self, it, v):
        ld, fr = v["local"], self.fr(it)
        head = ("---\n" + ld["fm"] + "---\n\n") if ld.get("fm") is not None else ""
        if it["kind"] == "section":
            title = M.plain(v["heading"][v["heading"]["type"]].get("rich_text"))
            blocks = unshift_headings(v["blocks"], v["level"])
        else:
            title = v.get("remote_title") if (ld["title"] is not None or not ld["exists"]) else None
            blocks = v["blocks"]
        if title:
            head += "# %s\n\n" % M.render_inline([M.seg(title)])
        body = self.render_remote(it, blocks, fr, markers=True, exclude=self._attached_ids(v["pid"]))
        return head + body.rstrip("\n") + "\n"

    def _pull_doc(self, it):
        k = it["key"]
        v = self.doc_view(it)
        vd = self.judge(v)
        ld = v["local"]
        if vd is None:
            return
        if vd == "missing":
            self.note(vd, k, "heading %r not found on the page" % it["entry"].get("section"))
            return
        if vd == "in-sync":
            s = self.state.get(k)
            if not s or s.get("local_sha") != ld["sha"]:
                self.record(k, local_sha=ld["sha"], remote_sha=_sha(v["remote_canon"]))
            self.note(vd, k)
            return
        if vd in ("local-ahead", "new-local"):
            self.note(vd, k, "local changes not pushed yet")
            return
        if vd == "remote-deleted":
            self.note(vd, k)
            return
        if vd == "local-deleted" and not self.force:
            self.note(vd, k, "local file deleted — pull --force restores it")
            return
        if vd == "conflict" and not self.force:
            self.note(vd, k, "changed on both sides — reconcile, then push or pull --force")
            return
        if self.dry:
            self.note("would-pull", k, vd)
            return
        self._download_images(it, v["blocks"], self.fr(it))
        text = self._doc_text(it, v)
        if it.get("path") is None:                      # a note deleted locally: restore it
            it["path"] = self.dir / k
        sha = self._write(it["path"], text)
        v2 = self.doc_view(it)
        self.record(k, local_sha=sha, remote_sha=_sha(v2["remote_canon"] or ""))
        self.note("pulled", k, vd)

    def _pull_new_notes(self, it):
        """Pages people add under a folder page in Notion arrive as new local files."""
        pid = self._id(it)
        if not pid or pid.startswith("dry:") or not _alive(get_page(pid)):
            return
        folder = it["key"]
        mapped = {self._id(x) for x in self.items.values()}
        for b in self.page_blocks(pid):
            if b.get("type") != "child_page" or b["id"] in mapped:
                continue
            title = b["child_page"].get("title") or "untitled"
            stem, n = file_slug(title), 1
            key = folder + stem + ".md"
            while key in self.items or (self.dir / key).exists():
                n += 1
                key = folder + "%s-%d.md" % (stem, n)
            if self.dry:
                self.note("would-pull", key, "new-remote page %r" % title)
                continue
            new = {"key": key, "kind": "page", "path": self.dir / key, "entry": it["entry"], "note_of": folder}
            self.items[key] = new
            self.set_id(new, b["id"])
            v = self.doc_view(new)
            self._download_images(new, v["blocks"], key)
            sha = self._write(new["path"], self._doc_text(new, v))
            v2 = self.doc_view(new)
            self.record(key, local_sha=sha, remote_sha=_sha(v2["remote_canon"] or ""))
            self.note("pulled", key, "new-remote page %r" % title)

    def _pull_collection(self, it, scope):
        k = it["key"]
        did, db, rows, by_key, keyless, sch, tk = self.collection(it)
        if not did or did.startswith("dry:") or db is None:
            for rk in rows:
                self.note("new-local" if not did else "remote-deleted", rk)
            return
        folder = self.dir / k.rstrip("/")
        for rk, pg in list(by_key.items()):
            if scope and not (self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                continue
            row = rows.get(rk)
            rvals, blocks, remote_canon = self._remote_row(it, pg, rk, sch, fetch=True)
            if row is None:
                if self.state.get(rk) and self.state[rk].get("local_sha") and not self.force:
                    self.note("local-deleted", rk, "kept in Notion (pull --force restores the file)")
                    continue
                if self.dry:
                    self.note("would-pull", rk, "new-remote")
                    continue
                self._download_images(it, blocks, rk)
                sha = self._write(folder / posixpath.basename(rk), self._row_text(it, None, rvals, sch, tk, blocks, rk))
                self._record_row(rk, sha, pg, rvals, self.row_canon(rvals, self.render_remote(it, blocks, rk)))
                self.note("pulled", rk, "new-remote")
                continue
            lvals, local_canon = self._local_row_canon(row, sch, tk, rk)
            vd = verdict(self.state.get(rk), True, True, row["sha"], _sha(remote_canon), remote_canon == local_canon)
            if vd == "in-sync":
                s = self.state.get(rk)
                if not s or s.get("local_sha") != row["sha"]:
                    self._record_row(rk, row["sha"], pg, rvals, remote_canon)
                self.note(vd, rk)
                continue
            if vd == "local-ahead":
                self.note(vd, rk, "local changes not pushed yet")
                continue
            if vd == "conflict" and not self.force:
                self.note(vd, rk, "changed on both sides — reconcile, then push or pull --force")
                continue
            if self.dry:
                self.note("would-pull", rk, vd)
                continue
            self._download_images(it, blocks, rk)
            sha = self._write(row["item"]["path"], self._row_text(it, row, rvals, sch, tk, blocks, rk))
            self._record_row(rk, sha, pg, rvals, self.row_canon(rvals, self.render_remote(it, blocks, rk)))
            self.note("pulled", rk, vd)
        for pg in keyless:                                   # rows added in Notion: give them a Key
            title = next((remote_value(p) for p in pg["properties"].values() if p.get("type") == "title"), None)
            stem, n = file_slug(title or "untitled"), 1
            while (folder / (stem + ".md")).exists() or (k + stem + ".md") in by_key:
                n += 1
                stem = "%s-%d" % (file_slug(title or "untitled"), n)
            rk = k + stem + ".md"
            if self.dry:
                self.note("would-pull", rk, "new-remote %r" % title)
                continue
            st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"properties": {KEY_PROP: {"rich_text": _rich(stem)}}})
            _die(st, r, "set Key on %r" % title)
            rvals, blocks, _rc = self._remote_row(it, r, rk, sch, fetch=True)
            self.row_ids[rk] = pg["id"]
            self._download_images(it, blocks, rk)
            sha = self._write(folder / (stem + ".md"), self._row_text(it, None, rvals, sch, tk, blocks, rk))
            self._record_row(rk, sha, r, rvals, self.row_canon(rvals, self.render_remote(it, blocks, rk)))
            self.note("pulled", rk, "new-remote %r" % title)
        for rk in rows:
            if rk not in by_key and (not scope or self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                s = self.state.get(rk)
                self.note("remote-deleted" if s and s.get("remote_sha") else "new-local", rk)

    def _row_text(self, it, row, vals, sch, tk, blocks, rk):
        order = [tk]
        if row:
            order += [x for x in row["values"] if x not in order]
        order += [str(x) for x in (it["entry"].get("schema") or {}) if str(x) not in order]
        order += sorted(x for x in vals if x not in order)
        pairs = [(x, vals[x]) for x in order if x in vals]
        body = self.render_remote(it, blocks, rk, markers=True)
        return dump_front_matter(pairs) + ("\n" + body.rstrip("\n") + "\n" if body.strip() else "")

    # ================================================================ STATUS
    def status(self, scope=None):
        for it in self.items.values():
            if not self.in_scope(it["key"], scope):
                continue
            if it["kind"] in ("page", "section"):
                if self.body_less(it):
                    continue
                vd = self.judge(self.doc_view(it))
                if vd:
                    self.note(vd, it["key"])
            elif it["kind"] == "file":
                ids = it["entry"].get("ids") or {}
                s = self.state.get(it["key"])
                if not it.get("path"):
                    self.note("local-deleted", it["key"])
                elif not ids.get(it["key"]):
                    self.note("new-local", it["key"])
                else:
                    sha = _sha_raw(it["path"].read_bytes())
                    self.note("in-sync" if not s or s.get("local_sha") == sha else "local-ahead", it["key"])
        for it in self.items.values():
            if it["kind"] != "db" or not self._db_in_scope(it, scope):
                continue
            did, db, rows, by_key, keyless, sch, tk = self.collection(it)
            if not did or db is None:
                for rk in rows:
                    self.note("new-local" if not did else "remote-deleted", rk)
                if not rows:
                    self.note("new-local" if not did else "remote-deleted", it["key"])
                continue
            for rk, row in rows.items():
                if scope and not (self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                    continue
                pg = by_key.get(rk)
                if pg is None:
                    s = self.state.get(rk)
                    self.note("remote-deleted" if s and s.get("remote_sha") else "new-local", rk)
                    continue
                rvals, blocks, remote_canon = self._remote_row(it, pg, rk, sch, fetch=self.deep)
                if remote_canon is None:
                    s = self.state[rk]
                    self.note("in-sync" if s.get("local_sha") == row["sha"] else "local-ahead", rk)
                    continue
                lvals, local_canon = self._local_row_canon(row, sch, tk, rk)
                self.note(verdict(self.state.get(rk), True, True, row["sha"], _sha(remote_canon),
                                  remote_canon == local_canon), rk)
            for rk in by_key:
                if rk not in rows:
                    s = self.state.get(rk)
                    self.note("local-deleted" if s and s.get("local_sha") else "new-remote", rk)
            for pg in keyless:
                self.note("new-remote", it["key"] + "?", "row %r without a Key" % page_title(pg))
        return self.results

    # ================================================================ persist
    def save(self):
        if self.dry:
            return
        text = self.mpath.read_text(encoding="utf-8")
        import io
        buf = io.StringIO()
        _yaml_rt().dump(self.man, buf)
        if buf.getvalue() != text:
            self.mpath.write_text(buf.getvalue(), encoding="utf-8")
        save_state(self.root, self.full_state)


# ============================================================ CLI plumbing
def _repo_root():
    _data, path = config.require_connections()
    return path.parent.resolve()


def find_manifests(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
        if MANIFEST in files:
            out.append(Path(d) / MANIFEST)
    return sorted(out)


def manifest_for(root, arg):
    """(manifest path, scope relative to its folder) for a path argument."""
    p = Path(arg)
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    d = p if p.is_dir() else p.parent
    while True:
        if (d / MANIFEST).is_file():
            rel = p.relative_to(d).as_posix()
            scope = None if rel == "." else rel + ("/" if p.is_dir() else "")
            return d / MANIFEST, scope
        if d == root or d.parent == d:
            break
        d = d.parent
    sys.exit("no %s found at or above %s — `bizconnect notion link <dir> <hub-url>` first." % (MANIFEST, arg))


ICON = {"in-sync": " ", "pushed": "↑", "would-push": "↑?", "pulled": "↓", "would-pull": "↓?",
        "local-ahead": "L", "remote-ahead": "R", "conflict": "!", "new-local": "+", "new-remote": "+R",
        "local-deleted": "-", "remote-deleted": "-R", "archived": "x", "adopted": "=", "created": "*",
        "re-anchored": "~", "skipped": ".", "missing": "?", "error": "E"}


def _report(tree, verb):
    from collections import Counter
    c = Counter(v for v, _k, _m in tree.results)
    print("notion %s %s  (hub %s)" % (verb, (tree.key or ".") + "/" + MANIFEST, NOTION_URL + _hex(tree.hub)))
    for v, k, msg in tree.results:
        if v == "in-sync":
            continue
        print("  %-2s %-15s %s%s" % (ICON.get(v, "?"), v, k, ("  — " + msg) if msg else ""))
    print("  " + (", ".join("%s=%d" % (x, n) for x, n in sorted(c.items())) or "nothing mapped"))
    return c


def _run(verb, argv):
    root = _repo_root()
    pos = [a for a in argv if not a.startswith("--")]
    targets = [manifest_for(root, pos[0])] if pos else [(m, None) for m in find_manifests(root)]
    if not targets:
        sys.exit("no %s in this repo — `bizconnect notion link <dir> <hub-url>` first." % MANIFEST)
    fl = {"dry": "--dry-run" in argv, "force": "--force" in argv, "prune": "--prune" in argv,
          "deep": "--deep" in argv}
    rc = 0
    for mpath, scope in targets:
        try:
            tree = Tree(root, mpath, **fl)
        except SyncError as e:
            print("notion %s %s: %s" % (verb, mpath, e))
            rc = 1
            continue
        try:
            tree.check_overlaps()
            getattr(tree, verb)(scope)
        except SyncError as e:
            tree.note("error", scope or ".", str(e))
        tree.save()
        c = _report(tree, verb)
        if fl["dry"]:
            print("  (dry run — nothing written)")
        blocking = {"push": ("conflict", "remote-ahead", "error", "missing"),
                    "pull": ("conflict", "local-ahead", "error", "missing"),
                    "status": ("error",)}[verb]
        if any(c.get(x) for x in blocking):
            rc = 1
    return rc


def cmd_push(argv):
    return _run("push", argv)


def cmd_pull(argv):
    return _run("pull", argv)


def cmd_status(argv):
    return _run("status", argv)


MANIFEST_HEADER = """\
# How this folder maps onto Notion — `bizconnect notion status | push | pull`.
# Each `map` entry ties a local path (relative to this folder) to ONE target:
#   page: <url> | new       the file is that whole page (title = its first `# H1`)
#   section: <heading>      the file is the part of the page under that heading
#   pages: new | <url>      (a folder) each .md is its own child page of that page;
#                           new files are published on the next push
#   database: new | <url>   (a folder) each .md is a row: front-matter -> properties
# `in:` picks the container (another entry's path, a URL; default: the hub).
# `id` (and `ids` / `media`) are filled in by the tool — they keep the mapping pinned
# when pages or headings are renamed or moved in Notion.
"""


def cmd_link(argv):
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) < 2:
        sys.exit("link needs <dir> <hub-page-url>")
    d = Path(pos[0])
    d = (d if d.is_absolute() else Path.cwd() / d).resolve()
    if not d.is_dir():
        sys.exit("not a directory: %s" % pos[0])
    pid = N.norm_id(pos[1])
    pg = get_page(pid)
    if not _alive(pg):
        sys.exit("cannot read page %s — share it with the integration (••• → Connections)." % pid)
    m = d / MANIFEST
    if m.exists():
        y = _yaml_rt()
        data = y.load(m.read_text(encoding="utf-8")) or {}
        data["hub"] = pg.get("url") or pid
        import io
        buf = io.StringIO()
        y.dump(data, buf)
        m.write_text(buf.getvalue(), encoding="utf-8")
        print("re-pointed %s at %r" % (m, page_title(pg)))
        return 0
    m.write_text(MANIFEST_HEADER + "hub: %s\nmap: []\n" % (pg.get("url") or pid), encoding="utf-8")
    print("created %s -> %r\n  %s\n  add entries with `bizconnect notion map <path> --section H | --page URL|new | --database new`"
          % (m, page_title(pg), pg.get("url") or pid))
    return 0


def cmd_map(argv):
    def opt(name):
        v = argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
        return v.strip() if isinstance(v, str) else v
    flags = {"--section", "--page", "--pages", "--database", "--in", "--title", "--level"}
    pos, skip = [], set()
    for i, a in enumerate(argv):
        if a in flags:
            skip.add(i + 1)
    for i, a in enumerate(argv):
        if i not in skip and not a.startswith("--"):
            pos.append(a)
    if not pos:
        sys.exit("map needs <path> and one of --section H | --page URL|new | --database [URL|new]")
    root = _repo_root()
    p = Path(pos[0])
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    mpath, _scope = manifest_for(root, str(p.parent if not p.exists() else p))
    rel = p.relative_to(mpath.parent).as_posix()
    y = _yaml_rt()
    data = y.load(mpath.read_text(encoding="utf-8")) or {}
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    seq = data.get("map")
    if not isinstance(seq, list):
        seq = CommentedSeq()
        data["map"] = seq
    if any(str(e.get("path", "")).strip("/") == rel.strip("/") for e in seq):
        sys.exit("%s is already mapped in %s" % (rel, mpath))
    e = CommentedMap()
    e["path"] = rel
    if opt("--section"):
        e["section"] = opt("--section")
    elif "--page" in argv:
        e["page"] = opt("--page") or "new"
    elif "--database" in argv:
        v = opt("--database")
        e["database"] = v if v and not v.startswith("--") else "new"
    elif "--pages" in argv:
        v = opt("--pages")
        e["pages"] = v if v and not v.startswith("--") else "new"
    else:
        sys.exit("say what it maps to: --section H | --page URL|new | --pages [URL|new] | --database [URL|new]")
    for f, key in (("--in", "in"), ("--title", "title"), ("--level", "level")):
        if opt(f):
            e[key] = int(opt(f)) if key == "level" else opt(f)
    seq.append(e)
    import io
    buf = io.StringIO()
    y.dump(data, buf)
    mpath.write_text(buf.getvalue(), encoding="utf-8")
    print("mapped %s -> %s in %s" % (rel, {k: v for k, v in e.items() if k != "path"}, mpath))
    return 0


def cmd_outline(argv):
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        sys.exit("outline needs <page|url|dir>")
    arg = pos[0]
    pth = Path(arg)
    if pth.is_dir() and (pth / MANIFEST).is_file():
        arg = str((_yaml_rt().load((pth / MANIFEST).read_text(encoding="utf-8")) or {}).get("hub"))
    pid = N.norm_id(arg)
    pg = get_page(pid)
    if not _alive(pg):
        sys.exit("cannot read page %s" % pid)
    print("%s  %s" % (page_title(pg), NOTION_URL + _hex(pid)))
    for b in N.get_children(pid):
        t = b.get("type")
        if t in HEADINGS:
            print("  %s%s  [section id %s]" % ("#" * heading_level(b) + " ", M.plain(b[t].get("rich_text")), b["id"]))
        elif t in ("child_page", "child_database"):
            print("      %s %s  [%s %s]" % ("📄" if t == "child_page" else "🗄️", b[t].get("title"), t, b["id"]))
        elif t in ("file", "pdf", "image", "video", "audio"):
            nm = b[t].get("name") or urllib.parse.unquote((b[t].get(b[t].get("type"), {}) or {}).get("url", "").split("?")[0].split("/")[-1])
            print("      📎 %s  [%s %s]" % (nm, t, b["id"]))
    return 0
