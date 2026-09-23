"""notion tree sync — keep local project files and a Notion hub in step, via a mapping file.

The mapping file
----------------
A folder is bound to Notion by a `notion.yaml` in it (committed). Each `map` entry ties a
local path (relative to that folder) to ONE Notion target:

    hub: https://www.notion.so/...Project-Hub-0123...        # default container for targets
    link_base: https://github.com/you/repo/blob/main/proj/   # optional: links out of the folder
    map:
      - path: thinking.md
        section: Current thinking                    # the blocks under that heading on the hub,
                                                     # up to the next heading of the same/higher level
      - path: plan.md
        section: Next steps
        create: true                                 # push may add this heading if it's missing
      - path: insights.md
        page: https://www.notion.so/...Insights-4567...   # the whole page (title = file's `# H1`)
      - path: notes/overview.md
        page: new                                    # a new child page, created on first push
        in: insights.md                              # container: a PAGE entry, a URL, or the hub
      - path: research                               # a FOLDER of notes: each .md becomes its
        pages: new                                   # own child page of a folder page (new or
        title: Research notes                        # a URL); new files publish on next push
      - path: log                                    # a FOLDER of .md files ...
        database: new                                # ... = a database: one row per file,
        title: Public log                            #     front-matter -> properties, body -> page
        schema: {date: date, status: select, tags: multi_select, url: {type: url, name: URL}}
      - path: context/*.pdf                          # files (glob) uploaded into a section/page
        section: Background

The tool writes each target's `id` (page, heading block or database id) back into its entry, so
a mapping keeps working when the page or heading is renamed or moved in Notion, and `synced`: a
fingerprint of the content both sides had at the last sync (one per file for `pages:` and
`database:` folders). Database rows are matched by a `Key` property (= the file's stem); image
files pulled from Notion are recorded under the entry's `media` (Notion file id -> local path).
Nothing outside the mapped targets is ever touched: other sections, child pages, databases and
file blocks on the page stay as they are.

Sync semantics
--------------
Markdown is the working copy agents edit and git versions; Notion is where people read and edit.
Both directions are GUARDED: each item's `synced` fingerprint says what both sides held at the
last sync, so either side's changes since then are known —

    push    sends local changes, refusing items changed in Notion since the last sync;
    pull    takes Notion changes, refusing items changed locally since the last sync;
    status  shows each item's verdict (no writes): in-sync | local-ahead | remote-ahead |
            conflict | no-baseline | new-local | new-remote | local-deleted | remote-deleted |
            missing | error.

`synced` lives in the committed notion.yaml, so every machine and clone shares it: commit
notion.yaml with the files. (A per-machine `.bizconnect/state.json`, git-ignored, also records
each sync; it decides only for items synced before `synced` existed.) With neither record and
differing sides, the verdict is `no-baseline`: compare with `diff`, then `pull --force` or
`push --force` that path. A pull that overwrites uncommitted local changes keeps a copy in
`.bizconnect/backup/`.
`--force` overrides the guard; `--prune` lets push archive notes, rows and uploaded files whose
local file was deleted (only ones this machine has synced).
Push reports what it changed (blocks added / removed / unchanged, and where), and warns on
paragraphs over the house-style limit (`style: {max_para_words: N}` in notion.yaml or
connections.yaml `notion.style`; default 80, 0 = off). The mapping file and sync state are
merged on save, so concurrent sessions syncing the same folder don't drop each other's ids.
Pushes are minimal: blocks are diffed by signature (a block's signature is its Markdown), so
unchanged Notion blocks — with their comments — are untouched, and blocks Markdown can't express
(child pages, databases, embeds, files ...) are never removed.

Verbs (under `bizconnect notion`)
-----
  link    <dir> <hub-url>                    create <dir>/notion.yaml bound to a hub page
  map     <path> --section H | --page URL|new | --pages [URL|new] | --database [URL|new]
                                             [--in X] [--title T] [--level N] [--create]
                                             add a mapping entry (path relative to cwd);
                                             --create: let push add a section heading that
                                             resembles an existing one
  outline <page|url|dir>                     a page's headings / child pages / databases, with ids
  locate  <page-url#block-id> [--map F]      where a linked block lives: its text, its section, the
                                             file that maps it; --map F maps + pulls an unmapped section
  diff    <mapped path>                      unified diff: the Notion version vs the local file
  status  [path] [--deep]                    per-item verdicts (makes no writes)
  push    [path] [--dry-run] [--force] [--prune]
  pull    [path] [--dry-run] [--force]
With no path, status/push/pull cover every notion.yaml in the repo.
"""
from __future__ import annotations

import difflib
import glob as _glob
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import time
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
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".bizconnect", "dist", "build",
             "target", "site-packages", ".tox"}
API_VERSION = "2022-06-28"      # databases/rows use this API shape; the sync pins it
N.PINNED_VERSION = API_VERSION
# a database row's Key is its file name: one path segment that is valid on every OS
SAFE_KEY = re.compile(r'^[^\x00-\x1f<>:"/\\|?*.\s][^\x00-\x1f<>:"/\\|?*]{0,119}$')
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}
INDEX_STEMS = {n.lower().rsplit(".", 1)[0] for n in INDEX_NAMES}
TARGET_KEYS = ("page", "section", "pages", "database")
KNOWN_KEYS = set(TARGET_KEYS) | {"path", "in", "title", "level", "schema", "description", "create",
                                 "id", "ids", "media", "synced"}


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


def _fp(canon) -> str:
    """A short fingerprint of an item's canonical content (what `synced` records)."""
    return _sha(canon or "")[:16]


ACRONYMS = {"url": "URL", "id": "ID", "api": "API", "ai": "AI"}


def prettify(name):
    s = re.sub(r"[-_]+", " ", name or "").strip()
    s = " ".join(ACRONYMS.get(w.lower(), w) for w in s.split(" "))
    return (s[:1].upper() + s[1:]) if s else "Untitled"


def slug(s):
    return re.sub(r"[^0-9a-z]+", "_", (s or "").strip().lower()).strip("_") or "field"


def file_slug(s):
    """A safe one-segment file stem from a title (Unicode letters kept: '日本語メモ' stays readable)."""
    out = re.sub(r"[^\w.-]+", "-", (s or "").strip().lower())
    out = re.sub(r"\.{2,}", ".", re.sub(r"-{2,}", "-", out)).strip("-._")[:80].strip("-._") or "untitled"
    out = out + "-page" if out in RESERVED_NAMES or out in INDEX_STEMS else out
    return out if safe_key(out) else "untitled"


def safe_key(key):
    """Is `key` (a database row's Key, i.e. its file stem) one path segment, valid on every OS?"""
    k = key or ""
    return bool(SAFE_KEY.match(k)) and ".." not in k and not k.endswith((".", " ")) \
        and k == k.strip() and k.lower() not in RESERVED_NAMES


def read_text(path):
    """A Markdown file's text: UTF-8 (a leading BOM is dropped). Anything else is refused with a
    clear message rather than silently mangled into U+FFFD on its way to Notion."""
    raw = Path(path).read_bytes()
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SyncError("%s is not UTF-8 (byte 0x%02x at offset %d) — re-save it as UTF-8"
                        % (Path(path).name, raw[e.start], e.start))


def index_of(folder):
    """A folder's README.md / index.md, matched case-insensitively (one answer on every OS)."""
    if not folder or not Path(folder).is_dir():
        return None
    names = {n.lower() for n in INDEX_NAMES}
    hits = sorted(f for f in Path(folder).iterdir() if f.is_file() and f.name.lower() in names)
    return hits[0] if hits else None


def is_index(path):
    return Path(path).name.lower() in {n.lower() for n in INDEX_NAMES}


def git_clean(root, path):
    """True if `path` is tracked by git and unchanged since HEAD (False when unsure)."""
    try:
        rel = str(Path(path).resolve().relative_to(Path(root).resolve()))
        kw = {"capture_output": True, "timeout": 15, "cwd": str(root)}
        if subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel], **kw).returncode:
            return False
        return subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], **kw).returncode == 0
    except Exception:
        return False


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


_PLAIN_OK = re.compile(r"^[A-Za-z0-9À-￿][^#\[\]{},&*!|>'\"%@`\n\r\t]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", "y", "n"}


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if (not _PLAIN_OK.match(s) or s != s.strip() or s.lower() in _RESERVED or s.endswith(":")
            or re.fullmatch(r"[-+]?[\d_.]+([eE][-+]?\d+)?", s) or ": " in s or " #" in s
            or re.fullmatch(r"[\d:.]+", s)):                  # 12:30 would read as a sexagesimal int
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


# ============================================================ manifest merge & style
TOOL_KEYS = ("id", "ids", "media")       # the fields the tool (not people) writes into map entries
DEFAULT_STYLE = {"max_para_words": 80}   # push warns (never blocks) on paragraphs longer than this


def _atomic_write(path, text):
    """Write `text` (LF line endings on every OS) via a unique temp file + os.replace, retrying
    the replace briefly on Windows sharing violations (another session reading the file)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.replace("\r\n", "\n").encode("utf-8"))
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class _Lock:
    """A best-effort lock file around read-merge-write of a shared file (stale after 60s)."""

    def __init__(self, path):
        self.path = str(path) + ".lock"
        self.fd = None

    def __enter__(self):
        for _ in range(200):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 60:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
        return self                                   # give up waiting; the merge still applies

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            try:
                os.remove(self.path)
            except OSError:
                pass


def _entry_path(e):
    return str(e.get("path") or "").strip().strip("/")


def _tool_fields(man):
    """path -> {tool key: plain value} for every map entry (malformed entries are skipped here;
    _build reports them)."""
    entries = man.get("map") if isinstance(man, dict) else None
    return {_entry_path(e): {k: (dict(e[k]) if isinstance(e[k], dict) else e[k]) for k in TOOL_KEYS if k in e}
            for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)}


def _merge_tool_fields(man, base, mine):
    """Apply the tool-field changes we made (base -> mine) onto `man`, the disk version.
    Entries only the disk version has are kept; entries it dropped are not resurrected."""
    from ruamel.yaml.comments import CommentedMap
    disk = {_entry_path(e): e for e in (man.get("map") or []) if isinstance(e, dict)}
    for path, fields in mine.items():
        e = disk.get(path)
        if e is None:
            continue
        old_fields = base.get(path, {})
        for k in TOOL_KEYS:
            new, old = fields.get(k), old_fields.get(k)
            if new == old:
                continue
            if isinstance(new, dict) or isinstance(old, dict):
                new, old = new or {}, old or {}
                cur = e.get(k) if isinstance(e.get(k), dict) else CommentedMap()
                for kk in set(old) - set(new):
                    cur.pop(kk, None)
                for kk, vv in new.items():
                    if old.get(kk) != vv:
                        cur[kk] = vv
                if cur:
                    e[k] = cur
                else:
                    e.pop(k, None)
            elif new is None:
                e.pop(k, None)
            else:
                e[k] = new


def _style(man):
    """House-style limits: DEFAULT_STYLE < connections.yaml `notion.style` < notion.yaml `style`."""
    out = dict(DEFAULT_STYLE)
    try:
        data, _p = config.load_connections()
        out.update(((data or {}).get("notion") or {}).get("style") or {})
    except Exception:
        pass
    out.update(man.get("style") or {})
    return out


# ============================================================ state
def _state_path(root):
    return Path(root) / STATE_DIR / STATE_FILE


def load_state(root):
    p = _state_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:                        # keep it for inspection, start afresh
            bad = p.with_name("%s.bad-%s" % (p.name, datetime.now().strftime("%Y%m%d-%H%M%S")))
            try:
                os.replace(p, bad)
            except OSError:
                bad = p
            sys.stderr.write("  ! sync state %s was unreadable (%s); kept as %s — this machine will "
                             "re-baseline on the next pull\n" % (p, e, bad.name))
    return {}


def save_state(root, full):
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, json.dumps(full, indent=2, ensure_ascii=False) + "\n")
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
    """Index pairs (i, j) of a longest common subsequence of sequences a and b. The common
    prefix/suffix is matched directly (the usual case: an edit in the middle or an append), and
    very large middles fall back to difflib, so big pages don't take quadratic time and memory."""
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]:
        suf += 1
    ma, mb = a[pre:len(a) - suf], b[pre:len(b) - suf]
    if len(ma) * len(mb) > 2_000_000:
        sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
        mid = [(i + k, j + k) for i, j, n in sm.get_matching_blocks() for k in range(n)]
    else:
        mid = _lcs_dp(ma, mb)
    return ([(i, i) for i in range(pre)] + [(i + pre, j + pre) for i, j in mid]
            + [(len(a) - suf + k, len(b) - suf + k) for k in range(suf)])


def _lcs_dp(a, b):
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


def verdict(st, local_exists, remote_exists, local_sha, remote_sha, same, remote_empty=False,
            moved=None):
    """The sync decision for one item. `same` = local and remote canonical forms are equal.
    `moved` = which side changed since the committed `synced` fingerprint ('local' | 'remote' |
    'both' | 'neither'), when the item has one: it decides. Otherwise this machine's sync record
    (`st`) decides; with neither, an empty remote is local-ahead and anything else is
    no-baseline — nothing is guessed."""
    if not remote_exists:
        if not local_exists:
            return None
        return "remote-deleted" if st and st.get("remote_sha") else "new-local"
    if not local_exists:
        return "local-deleted" if st and st.get("local_sha") else "new-remote"
    if same:
        return "in-sync"
    if moved:
        return {"both": "conflict", "local": "local-ahead", "remote": "remote-ahead"}.get(moved, "in-sync")
    if not st:
        return "local-ahead" if remote_empty else "no-baseline"
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
        self._loaded_text = self.mpath.read_text(encoding="utf-8-sig")
        try:
            self.man = _yaml_rt().load(self._loaded_text) or {}
        except Exception as e:                                   # noqa: BLE001 — YAML syntax
            raise SyncError("%s is not valid YAML: %s" % (self.mpath, str(e).splitlines()[0] if str(e) else e))
        if not isinstance(self.man, dict):
            raise SyncError("%s must be a mapping with `hub:` and `map:`" % self.mpath)
        if not self.man.get("hub"):
            raise SyncError("%s has no `hub:`" % self.mpath)
        if not _nid(str(self.man["hub"]).split("?")[0].split("#")[0]) and not _nid(str(self.man["hub"])):
            raise SyncError("%s: hub %r has no Notion page id in it" % (self.mpath, str(self.man["hub"])))
        self.hub = N.norm_id(str(self.man["hub"]))
        self.link_base = self.man.get("link_base") or None
        self.style = _style(self.man)
        self.dry, self.force, self.prune, self.deep, self.out = dry, force, prune, deep, out
        self._loaded_tool = _tool_fields(self.man)     # what save() diffs against (see save)
        self.full_state = load_state(self.root)
        self.state = self.full_state.setdefault(STATE_KEY, {}).setdefault(self.key or ".", {})
        self._state0 = json.loads(json.dumps(self.state))
        self.items = {}
        self.row_ids = {}                 # row key -> page id
        self.created = set()
        self.results = []
        self.errored = set()              # keys that already have an error this run
        self.bad_rows = {}                # row key -> why its file couldn't be read (never "deleted")
        self.blocked = {}                 # key -> why it can't sync (e.g. nested mapped sections)
        self._blocks = {}                 # page id -> fetched top-level blocks (cache)
        self._build()

    # ------------------------------------------------------------- build items
    def _build(self):
        entries = self.man.get("map") or []
        if not isinstance(entries, list):
            raise SyncError("%s: `map:` must be a list of entries (each starting `- path: ...`)" % self.mpath)
        for e in entries:
            if not isinstance(e, dict):
                raise SyncError("%s: map entry %r must be a mapping (`- path: ...` plus a target)" % (self.mpath, e))
            raw = str(e.get("path") or "").strip().replace("\\", "/")
            path = posixpath.normpath(raw).strip("/") if raw else ""
            if not path or path == "." or path.startswith("..") or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
                raise SyncError("%s: map entry %r needs a `path` inside this folder" % (self.mpath, raw or e))
            unknown = [k for k in e if k not in KNOWN_KEYS]
            if not any(k in e for k in TARGET_KEYS):
                raise SyncError("%s: map entry %r needs one of page / section / pages / database%s"
                                % (self.mpath, path, (" (unknown keys %s — written for a newer biz-connect? "
                                                      "`/plugin update biz-connect`)" % unknown) if unknown else ""))
            if unknown:
                self.out("  ~ %s: map entry %r: ignoring unknown key(s) %s (newer biz-connect?)"
                         % (self.mpath.name, path, ", ".join(map(str, unknown))))
            if "database" in e:
                folder = self.dir / path
                self._add(path + "/", "db", folder if folder.is_dir() else None, e)
                if folder.is_dir():
                    for f in sorted(folder.glob("*.md"), key=lambda x: x.name.lower()):
                        if not is_index(f):
                            self._add(path + "/" + f.name, "row", f, e)
            elif "pages" in e:                              # a folder of notes: one page each
                folder = self.dir / path
                idx = index_of(folder)
                self._add(path + "/", "page", idx, e)
                self.items[path + "/"].update(container=True, fr=path + "/" + (idx.name if idx else "README.md"))
                if folder.is_dir():
                    for f in sorted(folder.glob("*.md"), key=lambda x: x.name.lower()):
                        if not is_index(f):
                            self._add(path + "/" + f.name, "page", f, e)
                            self.items[path + "/" + f.name]["note_of"] = path + "/"
                for rel in (e.get("ids") or {}):
                    if str(rel) not in self.items:
                        self._add(str(rel), "page", None, e)
                        self.items[str(rel)]["note_of"] = path + "/"
            elif re.search(r"[*?\[]", path):
                matched = False
                for f in sorted(_glob.glob(_glob.escape(str(self.dir)) + "/" + path, recursive=True)):
                    fp = Path(f)
                    if fp.is_file():
                        matched = True
                        self._add(fp.relative_to(self.dir).as_posix(), "file", fp, e)
                for rel in (e.get("ids") or {}):
                    if str(rel) not in self.items:
                        self._add(str(rel), "file", None, e)
                if not matched and not e.get("ids"):
                    self.out("  ~ %s: %r matches no files" % (self.mpath.name, path))
            elif "section" in e:
                self._add(path, "section", self.dir / path, e)
            else:
                self._add(path, "page", self.dir / path, e)

    def check_overlaps(self, scope=None):
        """A page mapped whole can't also have mapped sections: its diff would eat them. And a
        mapped section can't sit inside another (a lower-level heading under it): both would own
        the same blocks — those two items are blocked with the reason."""
        whole = {self._id(it): k for k, it in self.items.items()
                 if it["kind"] == "page" and self._id(it) and not self.body_less(it)}
        by_page = {}
        for k, it in self.items.items():
            if it["kind"] in ("section", "file") and "section" in it["entry"]:
                try:
                    cid = self.container_id(it)
                except SyncError:
                    continue
                if cid in whole:
                    raise SyncError("%s is a section of the page mapped whole by %s — map the page "
                                    "whole OR by sections, not both" % (k, whole[cid]))
                if it["kind"] == "section" and cid and not cid.startswith("dry:"):
                    by_page.setdefault(cid, []).append(it)
        for cid, secs in by_page.items():
            if len(secs) < 2 or (scope and not any(self.in_scope(s["key"], scope) for s in secs)):
                continue
            try:
                found = []
                for s in secs:
                    _p, head, region = self.find_section(s)
                    if head is not None:
                        found.append((s, head, {b["id"] for b in region}))
            except (SyncError, SystemExit):
                continue
            for a, ha, ra in found:
                for b, hb, _rb in found:
                    if a is not b and hb["id"] in ra:
                        why = ("%s's heading %r lies inside %s's section %r — both would own the same blocks; "
                               "give the headings the same level in Notion, or map only one"
                               % (b["key"], b["entry"].get("section"), a["key"], a["entry"].get("section")))
                        self.blocked[a["key"]] = self.blocked[b["key"]] = why

    def maps(self, scope):
        """Does `scope` (a path under this folder) name anything this notion.yaml maps?"""
        return any(self.in_scope(k, scope) or (k.endswith("/") and scope.startswith(k)) for k in self.items)

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
        tgt = self.items.get(ref) or self.items.get(ref + "/")
        if tgt is not None:
            if tgt["kind"] != "page":
                raise SyncError("%s: `in: %s` must name a page entry (a `page:` or `pages:` folder), "
                                "not a %s" % (it["key"], ref, tgt["kind"]))
            return self._id(tgt)
        if not _nid(ref):
            raise SyncError("%s: `in: %s` is neither a mapped page entry nor a Notion URL" % (it["key"], ref))
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
        it.pop("_why", None)
        it.pop("_bad_heading", None)
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
            if idx is not None and blocks[idx][blocks[idx]["type"]].get("is_toggleable"):
                it["_why"] = "%r is a toggle heading — its content is hidden inside it; use a plain heading" % e.get("section")
                it["_bad_heading"] = True
                return pid, None, None
            nested = self._nested_heading(blocks, str(e.get("section", ""))) if idx is None else None
            if nested:
                it["_why"], it["_bad_heading"] = nested, True
                return pid, None, None
            if idx is None and create and not self.dry:
                # the page's outermost heading level, so no existing section swallows the new one
                lvl = int(e.get("level") or min((heading_level(b) for b in blocks if b.get("type") in HEADINGS), default=2))
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
            it["_why"] = self._why_missing(blocks, str(e.get("section", "")), bool(e.get("create")))
            return pid, None, None
        head = blocks[idx]
        lvl = heading_level(head)
        region = []
        for b in blocks[idx + 1:]:
            if b.get("type") in HEADINGS and heading_level(b) <= lvl:
                break
            region.append(b)
        return pid, head, region

    @staticmethod
    def _nested_heading(blocks, section):
        """If `section` is a heading nested in a column / toggle / callout, say so (else None)."""
        want = _norm_heading(section)
        for b in blocks:
            if b.get("type") in ("column_list", "toggle", "callout", "synced_block") and b.get("has_children"):
                try:
                    stack = N.get_children(b["id"])
                    for _ in range(3):
                        nxt = []
                        for c in stack:
                            if c.get("type") in HEADINGS and _norm_heading(M.plain(c[c["type"]].get("rich_text"))) == want:
                                return ("heading %r is inside a %s — only top-level headings can be sections; "
                                        "move it out" % (section, b["type"].replace("_", " ")))
                            if c.get("has_children"):
                                nxt.extend(N.get_children(c["id"]))
                        stack = nxt
                except SystemExit:
                    pass
        return None

    @staticmethod
    def _why_missing(blocks, section, create=False):
        """Why a section heading wasn't found at the top level of its page — and what's close."""
        texts = [M.plain(b[b["type"]].get("rich_text")) for b in blocks if b.get("type") in HEADINGS]
        close = difflib.get_close_matches(section, texts, n=3, cutoff=0.5)
        return "heading %r not found on the page%s%s" % (
            section, ("; nearest: " + ", ".join(repr(c) for c in close)) if close else "",
            "" if create else " (fix the name, or add `create: true` to the entry to have push add it)")

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
        raw, text = read_text(p)
        fm, rest = M.split_front_matter(text)
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
        it, ld = v["it"], v["local"]
        if v.get("missing"):
            if it["kind"] == "section" and it["entry"].get("create") and not it.get("_bad_heading"):
                sec = it["entry"].get("section")
                if ld["exists"]:
                    it["_why"] = "heading %r isn't on the page yet — push adds it at the end" % sec
                    return "new-local"
                it["_why"] = "heading %r isn't on the page yet — push adds it once the file exists" % sec
                return "skipped"
            return "missing"
        if it["key"] in self.created:
            return "new-local" if ld["exists"] else "in-sync"
        same = v["remote_canon"] is not None and v["remote_canon"] == v["local_canon"]
        return verdict(self.state.get(it["key"]), ld["exists"], v["remote_exists"], ld["sha"],
                       _sha(v["remote_canon"]) if v["remote_canon"] is not None else None, same,
                       remote_empty=v.get("remote_empty", False),
                       moved=self._moved(it, v["local_canon"] if ld["exists"] else None, v["remote_canon"]))

    # ------------------------------------------------------------- the committed baseline
    @staticmethod
    def _multi(it):
        """Items of a `pages:` / `database:` folder keep their `synced` in a per-file map."""
        return "pages" in it["entry"] or "database" in it["entry"]

    def baseline(self, it):
        """(local, remote) fingerprints of the item's content at its last sync, by anyone: the
        `synced` field in notion.yaml (committed, so every machine shares it), or None."""
        v = it["entry"].get("synced")
        if self._multi(it):
            v = v.get(it["key"]) if isinstance(v, dict) else None
        if not v or not isinstance(v, str):
            return None
        l, _, r = v.partition(":")
        return l, (r or l)

    def _moved(self, it, local_canon, remote_canon):
        """Which side changed since the baseline — 'local' | 'remote' | 'both' | 'neither' — or
        None when the item has no baseline (never synced by a biz-connect that records one)."""
        b = self.baseline(it)
        if not b or local_canon is None or remote_canon is None:
            return None
        lm, rm = _fp(local_canon) != b[0], _fp(remote_canon) != b[1]
        return {(True, True): "both", (True, False): "local", (False, True): "remote"}.get((lm, rm), "neither")

    def _mark_synced(self, it, local_canon, remote_canon, pid=None):
        """Record what both sides hold after a sync (and a page's id), for every machine."""
        if self.dry:
            return
        if it["kind"] == "page" and not it.get("note_of") and not it["entry"].get("id") and pid \
                and not str(pid).startswith("dry:"):
            self.set_id(it, pid)
        lf, rf = _fp(local_canon), _fp(remote_canon)
        val = lf if lf == rf else "%s:%s" % (lf, rf)
        e = it["entry"]
        if not self._multi(it):
            if e.get("synced") != val:
                e["synced"] = val
            return
        m = e.get("synced")
        if not isinstance(m, dict):
            from ruamel.yaml.comments import CommentedMap
            m = CommentedMap()
            e["synced"] = m
        if m.get(it["key"]) != val:
            m[it["key"]] = val

    @staticmethod
    def _unmark(entry, key):
        """Forget a folder file's baseline (its note/row was archived)."""
        m = entry.get("synced")
        if isinstance(m, dict):
            m.pop(key, None)
            if not m:
                entry.pop("synced", None)

    def path_of(self, key):
        """A mapped key as a repo-relative path (for messages the user can paste)."""
        return posixpath.join(self.key, key) if self.key else key

    def refusal(self, vd, key):
        p = self.path_of(key)
        if vd == "no-baseline":
            dirty = (self.dir / key).is_file() and not git_clean(self.root, self.dir / key)
            return ("no sync record for it (in notion.yaml or on this machine) and Notion differs — compare "
                    "with `bizconnect notion diff %s`, then `pull --force %s` (take Notion's) or `push --force "
                    "%s` (publish yours)%s" % (p, p, p, "; the file has uncommitted changes — pull --force keeps "
                                                        "a copy in .bizconnect/backup/" if dirty else ""))
        return {"remote-ahead": "changed in Notion since the last sync — pull first",
                "conflict": "changed on both sides since the last sync — `bizconnect notion diff %s`, merge by "
                            "hand, then `push --force %s` (or `pull --force %s` to take Notion's)" % (p, p, p),
                "local-ahead": "local changes not pushed yet"}.get(vd, "")

    def _guard(self, key, fn, *a):
        """Run one item's sync; a failure is that item's error, not the end of the run."""
        try:
            return fn(*a)
        except SyncError as e:
            self.note("error", key, str(e))
        except SystemExit as e:                     # a notion.py helper gave up (NotionError)
            self.note("error", key, str(e))

    def _check_hub(self):
        hub = get_page(self.hub)
        if not _alive(hub):
            raise SyncError("hub page %s is missing or archived — is it shared with the integration "
                            "(page ••• → Connections)?" % self.hub)

    # ------------------------------------------------------------- recording
    def note(self, verdict_, key, msg=""):
        self.results.append((verdict_, key, msg))
        if verdict_ == "error":
            self.errored.add(key)

    def record(self, key, **kw):
        if self.dry:
            return
        cur = dict(self.state.get(key) or {})
        cur.update(kw)
        cur["synced_at"] = _now()
        self.state[key] = cur

    # ================================================================ PUSH
    def push(self, scope=None):
        self._check_hub()
        self._ensure_targets(scope)
        for it in list(self.items.values()):
            if it["kind"] == "db" and self._db_in_scope(it, scope) and it["key"] not in self.errored:
                self._guard(it["key"], self._push_collection, it, scope)
        for it in list(self.items.values()):
            if it["kind"] in ("page", "section") and self.in_scope(it["key"], scope) and not self.body_less(it) \
                    and it["key"] not in self.errored:
                self._guard(it["key"], self._push_doc, it)
        for it in list(self.items.values()):
            if it["kind"] == "file" and self.in_scope(it["key"], scope):
                self._guard(it["key"], self._push_file, it)
        return self.results

    def _is_blocked(self, it):
        why = self.blocked.get(it["key"])
        if why:
            self.note("error", it["key"], why)
        return bool(why)

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
                self._guard(it["key"], self._ensure_one, it, parent)   # one bad target stops only itself
            if not progress:
                break
        for it in pending:
            self.note("error", it["key"], "container `in: %s` could not be resolved" % it["entry"].get("in"))

    def _ensure_one(self, it, parent):
        if it["kind"] == "page" and not it.get("container") and \
                not (it.get("path") and it["path"].is_file()):
            self.note("skipped", it["key"], "no local file yet — nothing to create")
            return
        title = self._title_for(it)
        if it["kind"] == "page" and it.get("path") and it["path"].is_file():
            ld = self.local_doc(it)                   # refuse before creating: no empty page left behind
            prob = limit_problem(ld["blocks"]) or self.image_problem(ld["blocks"], self.fr(it))
            if prob:
                self.note("error", it["key"], prob)
                return
        if parent.startswith("dry:"):
            self.set_id(it, "dry:" + it["key"])
            self.created.add(it["key"])
            return
        adopted = self._adopt(parent, it["kind"], title)
        if adopted:
            self.set_id(it, adopted)
            self.note("adopted", it["key"], "existing %s %r" % ("database" if it["kind"] == "db" else "page", title))
            return
        if self.dry:
            self.set_id(it, "dry:" + it["key"])
            self.created.add(it["key"])
            return
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

    def image_problem(self, blocks, fr):
        """A local image Notion would refuse (checked before anything is written)."""
        for src in _image_srcs(blocks):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src or ""):
                continue
            p = self.dir / posixpath.normpath(posixpath.join(posixpath.dirname(fr), urllib.parse.unquote(src)))
            if p.is_file() and p.stat().st_size > 20 * 1024 * 1024:
                return "image %s is over Notion's 20MB upload limit — shrink it" % src
        return None

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
        idx = index_of(it.get("path"))
        if idx:
            t, _ = M.split_title(M.split_front_matter(read_text(idx)[1])[1])
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
        if self._is_blocked(it):
            return
        if it["kind"] == "section" and not self.dry:
            self.find_section(it, create=bool(it["entry"].get("create")) and bool(it.get("path") and it["path"].is_file()))
        v = self.doc_view(it)
        vd = self.judge(v)
        ld = v["local"]
        if vd is None:
            return
        if vd in ("missing", "skipped"):
            self.note(vd, k, it.get("_why") or "heading %r not found on the page" % it["entry"].get("section"))
            return
        if v.get("missing"):                            # create: true, dry run — the heading comes on push
            prob = limit_problem(ld["blocks"]) or self.image_problem(ld["blocks"], self.fr(it))
            self.note("error" if prob else "would-push" if self.dry else "missing", k, prob or it.get("_why"))
            return
        if vd == "in-sync":
            s = self.state.get(k)
            if not s or s.get("local_sha") != ld["sha"] or s.get("remote_sha") != _sha(v["remote_canon"] or ""):
                self.record(k, local_sha=ld["sha"], remote_sha=_sha(v["remote_canon"] or ""))
            self._mark_synced(it, v["local_canon"], v["remote_canon"], v["pid"])
            self.note(vd, k)
            return
        if vd == "new-local" and k not in self.created:
            if self._id(it) and not str(self._id(it)).startswith("dry:"):
                self.note("error", k, "cannot read its Notion page — deleted, or not shared with the integration?")
            elif self.dry:
                self.note("would-push", k, "new-local")
            else:
                self.note("error", k, "target page not found — is it shared with the integration?")
            return
        if vd == "local-deleted" and it.get("note_of") and self.prune:
            if not self.dry:
                st, r = N.api("PATCH", "/pages/%s" % v["pid"], body={"archived": True})
                _die(st, r, "archive page %s" % k)
                del it["entry"]["ids"][k]
                self._unmark(it["entry"], k)
                self.state.pop(k, None)
            self.note("archived", k, "local file deleted")
            return
        if vd in ("new-remote", "local-deleted"):
            self.note(vd, k, "no local file — pull to fetch it" + (" (push --prune archives it)" if it.get("note_of") else ""))
            return
        if vd == "remote-deleted":
            self.note(vd, k, "gone from Notion (fix the mapping, or push --force after clearing its id)")
            return
        if vd in ("remote-ahead", "conflict", "no-baseline") and not self.force:
            self.note(vd, k, self.refusal(vd, k))
            return
        prob = limit_problem(v["local_blocks"]) or self.image_problem(v["local_blocks"], self.fr(it))
        if prob:
            self.note("error", k, prob)
            return
        self.style_check(k, v["local_blocks"])
        if self.dry:
            self.note("would-push", k, vd)
            return
        pid = v["pid"]
        if it["kind"] == "page" and ld["title"] is not None and v.get("remote_title") != ld["title"]:
            st, r = N.api("PATCH", "/pages/%s" % pid, body={"properties": {"title": {"title": _rich(ld["title"])}}})
            _die(st, r, "set title %s" % k)
        start = v["heading"]["id"] if it["kind"] == "section" else None
        n = self.apply_body(pid, v["blocks"] or [], v["local_blocks"], self.fr(it), it, start_after=start,
                            exclude=self._attached_ids(pid))
        where = ("under %r" % M.plain(v["heading"][v["heading"]["type"]].get("rich_text"))
                 if it["kind"] == "section" else "on its page")
        summary = "%s · %d block(s) added, %d removed, %d unchanged — only %s" % (
            vd, n["added"], n["removed"], n["kept"], where)
        self.invalidate(pid)
        v2 = self.doc_view(it)
        self._learn_media(it, v2["blocks"] or [], v2["local_blocks"], self.fr(it))
        v2 = self.doc_view(it)
        if v2["remote_canon"] != v2["local_canon"]:
            self.out("  ! %s: Notion normalised the content differently than expected" % k)
        self.record(k, local_sha=ld["sha"], remote_sha=_sha(v2["remote_canon"] or ""))
        self._mark_synced(it, v2["local_canon"], v2["remote_canon"], pid)
        self.note("pushed", k, summary)

    def style_check(self, key, blocks):
        """Warn (never block) on paragraphs over the house-style word limit."""
        mx = self.style.get("max_para_words")
        if not mx:
            return
        n = 0
        for b in blocks:
            if b.get("type") != "paragraph":
                continue
            n += 1
            words = len(M.plain(b["paragraph"].get("rich_text")).split())
            if words > int(mx):
                self.out("  ~ %s: paragraph %d is %d words (house style max %s) — split it: a lead "
                         "line, then short bullets" % (key, n, words, mx))

    def apply_body(self, parent_id, remote_blocks, local_blocks, fr, it, start_after=None, exclude=()):
        """Minimal block diff: keep matching blocks, insert new ones, delete removed ones.
        Blocks Markdown can't express, and `exclude`d ids, are never touched.
        Returns {"added", "removed", "kept"} top-level block counts."""
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
        # build every payload (uploads included) before the first write: a failure here changes nothing
        built = [(a, [x for x in (self.to_api(b, fr) for b in bl) if x]) for a, bl in groups]
        added = 0
        for anchor_id, api_blocks in built:
            if api_blocks:
                N.append_children(parent_id, api_blocks, after=anchor_id, at_start=anchor_id is None)
                added += len(api_blocks)
        for i, b in enumerate(managed):
            if i not in matched_r:
                N.delete_block(b["id"])
        return {"added": added, "removed": len(managed) - len(matched_r), "kept": len(pairs)}

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
        if len(out) > 100:                     # API cap — never truncate (limit_problem refuses first)
            raise SyncError("a block needs %d rich-text pieces; Notion allows 100 — split it" % len(out))
        return out

    # ------------------------------------------------------------- collections
    def _inside(self, folder, path):
        try:
            Path(path).resolve().relative_to(Path(folder).resolve())
            return True
        except ValueError:
            return False

    def _get_db(self, did):
        st, db = N.api("GET", "/databases/%s" % did)
        if st == 404:
            return None
        if st == 400:
            raise SyncError("cannot read database %s [400]: %s — if someone added a second data source "
                            "to it in Notion, the sync can't use it (Notion API %s); remove the extra "
                            "source or map a new database" % (did, db.get("message") or db, API_VERSION))
        _die(st, db, "read database %s" % did)
        return db

    def _local_rows(self, db_key):
        """The readable row files of a database folder. An unreadable one (bad name, not UTF-8,
        broken front-matter) is an error, and is listed in `bad_rows` so that nothing treats it
        as deleted: push never archives its row, pull never writes over it."""
        rows = {}
        for k, it in self.items.items():
            if it["kind"] != "row" or not k.startswith(db_key):
                continue
            try:
                if not safe_key(it["path"].stem):
                    raise SyncError("rename %s — a row's file name is its Key: at most 120 characters, no "
                                    "leading '.' or space, none of \\ / : * ? \" < > |" % it["path"].name)
                raw, text = read_text(it["path"])
                rows[k] = self._parse_row(it, raw, text)
            except SyncError as e:
                self._bad_row(k, str(e))
            except Exception as e:                           # noqa: BLE001 — YAML front-matter
                self._bad_row(k, "front-matter: %s" % e)
        return rows

    def _bad_row(self, k, msg):
        if k not in self.bad_rows:
            self.bad_rows[k] = msg
            self.note("error", k, msg)

    @staticmethod
    def _parse_row(it, raw, text):
        fm, body = M.split_front_matter(text)
        values = _yaml_load(fm) if fm is not None else {}
        return {"item": it, "values": values, "blocks": M.parse_blocks(body), "sha": _sha(raw),
                "stem": it["path"].stem, "text": text}

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
        idx = index_of(it.get("path"))
        if not idx:
            return ""
        _t, body = M.split_title(M.split_front_matter(read_text(idx)[1])[1])
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
            if key and safe_key(key):
                rk = it["key"] + key + ".md"
                by_key[rk] = pg
                self.row_ids[rk] = pg["id"]
            else:
                if key:
                    pg["_unsafe_key"] = key            # re-keyed with a safe name on pull
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

    def _row_verdict(self, rk, row, remote_canon, local_canon):
        same = remote_canon == local_canon
        return verdict(self.state.get(rk), True, True, row["sha"], _sha(remote_canon), same,
                       moved=self._moved(row["item"], local_canon, remote_canon))

    def _local_row_canon(self, row, sch, tk, rk):
        vals = self._local_row_values(row, sch, tk)
        return vals, self.row_canon(vals, self.render_local(row["blocks"], rk))

    def _record_row(self, rk, row_sha, pg, vals, remote_canon):
        self.record(rk, local_sha=row_sha, remote_sha=_sha(remote_canon), page=pg.get("id"),
                    remote_edited=pg.get("last_edited_time"), remote_props=_jsha(vals))

    def _known_rows(self, db_key):
        """page id -> row key, for rows this machine has synced (to recognise a re-keyed row)."""
        return {s["page"]: rk for rk, s in self.state.items()
                if isinstance(s, dict) and s.get("page") and rk.startswith(db_key)}

    def _row_gone(self, s, rk):
        """A synced row no longer found by its Key: archived, or its Key changed in Notion?
        Returns (message, is it safe to recreate it with push --force)."""
        stem = posixpath.basename(rk)[:-3]
        pid = s.get("page")
        if pid:
            pg = get_page(pid)
            if _alive(pg):
                return ("its Key in Notion is now %r, not %r — set the Key back to %r (or rename the file to "
                        "match); push --force would add a duplicate row" % (self._row_key(pg), stem, stem)), False
            return "row archived in Notion (push --force recreates it)", True
        return ("no row with Key %r in Notion — archived, or its Key was changed? Check before push --force "
                "(it creates a new row)" % stem), True

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
        for rk, row in rows.items():
            if not (self.in_scope(rk, scope) or self._db_in_scope(it, scope) or not scope):
                continue
            self._guard(rk, self._push_one_row, it, rk, row, by_key.get(rk), did, sch, tk)   # one bad row stops only itself
        for rk, pg in by_key.items():
            if rk in rows or rk in self.bad_rows or not (self.in_scope(rk, scope) or not scope or self._db_in_scope(it, scope)):
                continue
            if self.state.get(rk) and self.prune and not self.dry:
                st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"archived": True})
                _die(st, r, "archive row %s" % rk)
                self.state.pop(rk, None)
                self._unmark(it["entry"], rk)
                self.note("archived", rk, "local file deleted")
            elif self.state.get(rk):
                self.note("local-deleted", rk, "kept in Notion (push --prune archives it)")
            else:
                self.note("new-remote", rk, "only in Notion — pull to fetch it")
        for pg in keyless:
            self.note("new-remote", k + "?", "row %r has no Key — pull to fetch it" % page_title(pg))

    def _push_one_row(self, it, rk, row, pg, did, sch, tk):
        if pg is None:
            s = self.state.get(rk)
            if s and s.get("remote_sha"):
                why, recreate = self._row_gone(s, rk)
                if not (self.force and recreate):
                    self.note("remote-deleted", rk, why)
                    return
            prob = limit_problem(row["blocks"]) or self.image_problem(row["blocks"], rk)
            if prob:                                  # refuse before creating: no half-made row
                self.note("error", rk, prob)
                return
            if self.dry:
                self.note("would-push", rk, "new-local")
                return
            vals = self._local_row_values(row, sch, tk)
            st, pg = N.api("POST", "/pages", body={"parent": {"database_id": did},
                                                   "properties": self._props_body(vals, sch, tk, row["stem"], full=False)})
            _die(st, pg, "create row %s" % rk)
            self.row_ids[rk] = pg["id"]
            self.created.add(rk)
            # a baseline straight away (the local side counts as unsent): if the body fails below,
            # the row is local-ahead next time — a pull never takes the empty Notion row
            rv = self._row_props(pg, sch)
            empty = self.row_canon(rv, "")
            self._record_row(rk, "", pg, rv, empty)
            self._mark_synced(row["item"], "", empty)
        self._push_row(it, rk, row, pg, sch, tk)

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
            vd = self._row_verdict(rk, row, remote_canon, local_canon)
            if vd == "in-sync":
                s = self.state.get(rk)
                if not s or s.get("local_sha") != row["sha"] or s.get("remote_sha") != _sha(remote_canon):
                    self._record_row(rk, row["sha"], pg, rvals, remote_canon)
                self._mark_synced(row["item"], local_canon, remote_canon)
                self.note(vd, rk)
                return
        if vd in ("remote-ahead", "conflict", "no-baseline") and not self.force:
            self.note(vd, rk, self.refusal(vd, rk))
            return
        prob = limit_problem(row["blocks"]) or self.image_problem(row["blocks"], rk)
        if prob:
            self.note("error", rk, prob)
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
        self._mark_synced(row["item"], local_canon, remote_now)
        self.note("pushed", rk, vd)

    # ------------------------------------------------------------- files
    def _file_target(self, it):
        """(page id, anchor block id to append after or None for the page end)."""
        e = it["entry"]
        if "section" in e:
            pid, head, region = self.find_section(it, create=bool(e.get("create")) and not self.dry)
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
            if fid and self.prune and not self.dry and self.state.get(k):
                N.delete_block(fid)
                del ids[k]
                self.state.pop(k, None)
                self.note("archived", k, "local file deleted")
            elif fid and self.state.get(k):
                self.note("local-deleted", k, "kept in Notion (push --prune removes it)")
            elif fid:
                self.note("local-deleted", k, "not on this machine and never synced from it — kept in Notion")
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
        self._check_hub()
        for it in list(self.items.values()):
            if it["kind"] in ("page", "section") and self.in_scope(it["key"], scope) and not self.body_less(it):
                self._guard(it["key"], self._pull_doc, it)
        for it in list(self.items.values()):
            if it.get("container") and self._db_in_scope(it, scope):
                self._guard(it["key"], self._pull_new_notes, it)
        for it in list(self.items.values()):
            if it["kind"] == "db" and self._db_in_scope(it, scope):
                self._guard(it["key"], self._pull_collection, it, scope)
        return self.results

    def _write(self, path, text):
        """Write a pulled file; returns its sha. On a forced pull, uncommitted local changes it
        replaces are kept in .bizconnect/backup/ first, since git can't give them back (`_kept()`
        reports the copy). An unforced pull only replaces a file unchanged since the last sync."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        saved = self._backup(path, data) if self.force else None
        path.write_bytes(data)
        self._saved = saved
        return _sha(data)

    def _backup(self, path, data):
        try:
            old = Path(path).read_bytes() if Path(path).is_file() else None
            if old is None or _sha(old) == _sha(data) or git_clean(self.root, path):
                return None
            rel = Path(path).resolve().relative_to(self.root).as_posix()
            dest = self.root / STATE_DIR / "backup" / ("%s.%s" % (rel, datetime.now().strftime("%Y%m%d-%H%M%S")))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)
            return dest.relative_to(self.root).as_posix()
        except (OSError, ValueError) as e:
            raise SyncError("could not back up %s before overwriting it (%s) — nothing written" % (Path(path).name, e))

    def _kept(self):
        """' (your uncommitted copy: …)' after a write that backed a file up."""
        s, self._saved = getattr(self, "_saved", None), None
        return " (uncommitted local copy kept: %s)" % s if s else ""

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
        if self._is_blocked(it):
            return
        v = self.doc_view(it)
        vd = self.judge(v)
        ld = v["local"]
        if vd is None:
            return
        if vd in ("missing", "skipped"):
            self.note(vd, k, it.get("_why") or "heading %r not found on the page" % it["entry"].get("section"))
            return
        if vd == "in-sync":
            s = self.state.get(k)
            if not s or s.get("local_sha") != ld["sha"]:
                self.record(k, local_sha=ld["sha"], remote_sha=_sha(v["remote_canon"]))
            self._mark_synced(it, v["local_canon"], v["remote_canon"], v["pid"])
            self.note(vd, k)
            return
        if vd in ("local-ahead", "new-local"):
            self.note(vd, k, (it.get("_why") if v.get("missing") else None) or "local changes not pushed yet")
            return
        if vd == "no-baseline" and not self.force:
            self.note(vd, k, self.refusal(vd, k))
            return
        if vd == "remote-deleted":
            self.note(vd, k)
            return
        if vd == "local-deleted" and not self.force:
            self.note(vd, k, "local file deleted — pull --force restores it")
            return
        if vd == "conflict" and not self.force:
            self.note(vd, k, self.refusal(vd, k))
            return
        if self.dry:
            self.note("would-pull", k, vd)
            return
        self._download_images(it, v["blocks"], self.fr(it))
        text = self._doc_text(it, v)
        if it.get("path") is None:                      # a note deleted locally: restore it
            it["path"] = self.dir / k
        sha = self._write(it["path"], text)
        kept = self._kept()
        v2 = self.doc_view(it)
        self.record(k, local_sha=sha, remote_sha=_sha(v2["remote_canon"] or ""))
        self._mark_synced(it, v2["local_canon"], v2["remote_canon"], v["pid"])
        self.note("pulled", k, vd + kept)

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
            self._mark_synced(new, v2["local_canon"], v2["remote_canon"])
            self.note("pulled", key, "new-remote page %r" % title)

    def _pull_collection(self, it, scope):
        k = it["key"]
        did, db, rows, by_key, keyless, sch, tk = self.collection(it)
        if not did or did.startswith("dry:") or db is None:
            for rk in rows:
                self.note("new-local" if not did else "remote-deleted", rk)
            return
        folder = self.dir / k.rstrip("/")
        known = self._known_rows(k)
        for rk, pg in list(by_key.items()):
            if scope and not (self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                continue
            if rk in self.bad_rows:                          # its file is unreadable: reported, left alone
                continue
            self._guard(rk, self._pull_row, it, rk, pg, rows.get(rk), folder, known, sch, tk)
        for pg in keyless:                                   # rows added in Notion: give them a Key
            title = next((remote_value(p) for p in pg["properties"].values() if p.get("type") == "title"), None)
            self._guard(k + "?", self._pull_keyless, it, pg, title, rows, by_key, folder, known, sch, tk)
        for rk in rows:                                      # (row_ids also has rows re-keyed above)
            if rk not in self.row_ids and (not scope or self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                s = self.state.get(rk)
                if s and s.get("remote_sha"):
                    self.note("remote-deleted", rk, self._row_gone(s, rk)[0])
                elif rk not in self.errored:
                    self.note("new-local", rk)

    def _pull_row(self, it, rk, pg, row, folder, known, sch, tk):
        rvals, blocks, remote_canon = self._remote_row(it, pg, rk, sch, fetch=True)
        if row is None:
            if self.state.get(rk) and self.state[rk].get("local_sha") and not self.force:
                self.note("local-deleted", rk, "kept in Notion (pull --force restores the file)")
                return
            old = known.get(pg["id"])
            if old and old != rk and (folder / posixpath.basename(old)).exists():
                self.note("error", rk, "this is the row synced as %s — its Key was changed in Notion; set it back "
                                       "to %r (or rename the file to match)" % (self.path_of(old), posixpath.basename(old)[:-3]))
                return
            target = folder / posixpath.basename(rk)
            if not self._inside(folder, target):
                self.note("error", rk, "refusing to write outside %s" % folder)
                return
            if target.exists():                      # a Key differing only in case from an existing file
                self.note("error", rk, "%s already exists here — two Keys that differ only in letter case? "
                                       "rename one in Notion" % target.name)
                return
            if self.dry:
                self.note("would-pull", rk, "new-remote")
                return
            self._write_row(it, rk, target, None, pg, rvals, blocks, sch, tk)
            self.note("pulled", rk, "new-remote")
            return
        lvals, local_canon = self._local_row_canon(row, sch, tk, rk)
        vd = self._row_verdict(rk, row, remote_canon, local_canon)
        if vd == "in-sync":
            s = self.state.get(rk)
            if not s or s.get("local_sha") != row["sha"]:
                self._record_row(rk, row["sha"], pg, rvals, remote_canon)
            self._mark_synced(row["item"], local_canon, remote_canon)
            self.note(vd, rk)
            return
        if vd == "local-ahead":
            self.note(vd, rk, "local changes not pushed yet")
            return
        if vd in ("conflict", "no-baseline") and not self.force:
            self.note(vd, rk, self.refusal(vd, rk))
            return
        if self.dry:
            self.note("would-pull", rk, vd)
            return
        self._write_row(it, rk, row["item"]["path"], row, pg, rvals, blocks, sch, tk)
        self.note("pulled", rk, vd + self._kept())

    def _write_row(self, it, rk, path, row, pg, rvals, blocks, sch, tk):
        """Write a pulled row file and record both baselines (per machine, and in notion.yaml)."""
        self._download_images(it, blocks, rk)
        text = self._row_text(it, row, rvals, sch, tk, blocks, rk)
        sha = self._write(path, text)
        remote_canon = self.row_canon(rvals, self.render_remote(it, blocks, rk))
        self._record_row(rk, sha, pg, rvals, remote_canon)
        item = row["item"] if row else {"key": rk, "kind": "row", "path": path, "entry": it["entry"]}
        written = self._parse_row(item, text.encode("utf-8"), text)
        self._mark_synced(item, self._local_row_canon(written, sch, tk, rk)[1], remote_canon)

    def _pull_keyless(self, it, pg, title, rows, by_key, folder, known, sch, tk):
        k = it["key"]
        why = (" (its Key %r isn't a safe file name)" % pg["_unsafe_key"]) if pg.get("_unsafe_key") else ""
        old = known.get(pg["id"])
        if old and old in rows:                      # a row we synced whose Key was cleared or mangled
            stem = posixpath.basename(old)[:-3]
            if self.dry:
                self.note("would-pull", old, "Key %r in Notion — would set it back to %r" % (pg.get("_unsafe_key") or "", stem))
                return
            st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"properties": {KEY_PROP: {"rich_text": _rich(stem)}}})
            _die(st, r, "set Key on %r" % title)
            self.row_ids[old] = pg["id"]
            self.note("re-keyed", old, "its Key in Notion was %r — set back to %r (the file's name)"
                      % (pg.get("_unsafe_key") or "", stem))
            self._pull_row(it, old, r, rows[old], folder, known, sch, tk)
            return
        stem, n = file_slug(title or "untitled"), 1
        while (folder / (stem + ".md")).exists() or (k + stem + ".md") in by_key:
            n += 1
            stem = "%s-%d" % (file_slug(title or "untitled"), n)
        rk = k + stem + ".md"
        if not self._inside(folder, folder / (stem + ".md")):
            self.note("error", rk, "refusing to write outside %s" % folder)
            return
        if self.dry:
            self.note("would-pull", rk, "new-remote %r%s" % (title, why))
            return
        st, r = N.api("PATCH", "/pages/%s" % pg["id"], body={"properties": {KEY_PROP: {"rich_text": _rich(stem)}}})
        _die(st, r, "set Key on %r" % title)
        rvals, blocks, _rc = self._remote_row(it, r, rk, sch, fetch=True)
        self.row_ids[rk] = pg["id"]
        self._write_row(it, rk, folder / (stem + ".md"), None, r, rvals, blocks, sch, tk)
        self.note("pulled", rk, "new-remote %r%s — Key %r written to the row" % (title, why, stem))

    def _row_text(self, it, row, vals, sch, tk, blocks, rk):
        order = [tk]
        if row:
            order += [x for x in row["values"] if x not in order]
        order += [str(x) for x in (it["entry"].get("schema") or {}) if str(x) not in order]
        order += sorted(x for x in vals if x not in order)
        stem = posixpath.basename(rk)[:-3]
        # a title that is just the file name, in a file that never wrote one, stays implicit
        implicit = row is not None and tk not in row["values"] and vals.get(tk) == prettify(stem)
        pairs = [(x, vals[x]) for x in order if x in vals and not (x == tk and implicit)]
        body = self.render_remote(it, blocks, rk, markers=True)
        sep = "\n" if row is None or re.match(r"^---\r?\n.*?\r?\n---\r?\n\r?\n", row.get("text", "") or "", re.S) else ""
        return (dump_front_matter(pairs) if pairs else "") + (sep + body.rstrip("\n") + "\n" if body.strip() else "")

    # ================================================================ STATUS
    def status(self, scope=None):
        self._check_hub()
        for it in list(self.items.values()):
            if not self.in_scope(it["key"], scope):
                continue
            if it["kind"] in ("page", "section"):
                if it.get("container") and self._id(it) and not str(self._id(it)).startswith("dry:"):
                    self._guard(it["key"], self._status_new_notes, it)
                if self.body_less(it):
                    continue
                self._guard(it["key"], self._status_doc, it)
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
        for it in list(self.items.values()):
            if it["kind"] != "db" or not self._db_in_scope(it, scope):
                continue
            self._guard(it["key"], self._status_collection, it, scope)
        return self.results

    def _status_collection(self, it, scope):
        did, db, rows, by_key, keyless, sch, tk = self.collection(it)
        if not did or db is None:
            for rk in rows:
                self.note("new-local" if not did else "remote-deleted", rk)
            if not rows:
                self.note("new-local" if not did else "remote-deleted", it["key"])
            return
        known = self._known_rows(it["key"])
        for rk, row in rows.items():
            if scope and not (self.in_scope(rk, scope) or self._db_in_scope(it, scope)):
                continue
            pg = by_key.get(rk)
            if pg is None:
                s = self.state.get(rk)
                if s and s.get("remote_sha"):
                    self.note("remote-deleted", rk, self._row_gone(s, rk)[0])
                else:
                    self.note("new-local", rk)
                continue
            rvals, blocks, remote_canon = self._remote_row(it, pg, rk, sch, fetch=self.deep)
            if remote_canon is None:
                s = self.state[rk]
                self.note("in-sync" if s.get("local_sha") == row["sha"] else "local-ahead", rk)
                continue
            lvals, local_canon = self._local_row_canon(row, sch, tk, rk)
            vd = self._row_verdict(rk, row, remote_canon, local_canon)
            self.note(vd, rk, self.refusal(vd, rk) if vd == "no-baseline" else "")
        for rk in by_key:
            if rk not in rows and rk not in self.bad_rows:
                s = self.state.get(rk)
                self.note("local-deleted" if s and s.get("local_sha") else "new-remote", rk)
        for pg in keyless:
            old = known.get(pg["id"])
            self.note("new-remote", it["key"] + "?", "row %r %s" % (page_title(pg), (
                "(synced as %s) lost its Key — pull sets it back" % self.path_of(old)) if old and old in rows else (
                "has an unsafe Key %r — pull renames it" % pg["_unsafe_key"]) if pg.get("_unsafe_key")
                else "has no Key — pull fetches it"))

    def _status_doc(self, it):
        if self._is_blocked(it):
            return
        v = self.doc_view(it)
        vd = self.judge(v)
        if vd:
            self.note(vd, it["key"], (it.get("_why") or "") if v.get("missing") else
                      (self.refusal(vd, it["key"]) if vd == "no-baseline" else ""))

    def _status_new_notes(self, it):
        mapped = {self._id(x) for x in self.items.values()}
        for b in self.page_blocks(self._id(it)):
            if b.get("type") == "child_page" and b["id"] not in mapped:
                title = b["child_page"].get("title") or "untitled"
                self.note("new-remote", it["key"] + file_slug(title) + ".md",
                          "page %r was added in Notion — pull to fetch it" % title)

    # ================================================================ persist
    def save(self):
        """Write back the ids/media we learned and the sync state. Several sessions may sync
        the same folder at once, so both files are merged, not overwritten: if the file
        changed on disk since we read it (another session's `map` or `pull`), only OUR
        changes (tool-written fields / touched state keys) are applied to the disk version."""
        if self.dry:
            return
        import io
        y = _yaml_rt()
        with _Lock(self.mpath):
            disk = self.mpath.read_text(encoding="utf-8-sig")
            man = self.man
            if disk != self._loaded_text:
                man = y.load(disk) or {}
                _merge_tool_fields(man, self._loaded_tool, _tool_fields(self.man))
            buf = io.StringIO()
            y.dump(man, buf)
            if buf.getvalue() != disk:
                _atomic_write(self.mpath, buf.getvalue())
        _state_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        with _Lock(_state_path(self.root)):
            full = load_state(self.root)
            cur = full.setdefault(STATE_KEY, {}).setdefault(self.key or ".", {})
            for k in set(self._state0) | set(self.state):
                if k not in self.state:
                    cur.pop(k, None)
                elif self.state[k] != self._state0.get(k):
                    cur[k] = self.state[k]
            save_state(self.root, full)


# ============================================================ CLI plumbing
def _repo_root():
    """The repo root: where connections.yaml lives, else the git top-level."""
    _data, path = config.load_connections()
    if path:
        return path.parent.resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:
        pass
    sys.exit("no connections.yaml or git repository found above %s — run `bizconnect init` in your "
             "repo root" % Path.cwd())


def find_manifests(root):
    """Every notion.yaml under `root`, skipping tool/build dirs and nested repos (they sync themselves)."""
    out = []
    for d, dirs, files in os.walk(root):
        keep = []
        for x in dirs:
            if x.startswith(".") or x in SKIP_DIRS:
                continue
            sub = Path(d) / x
            if (sub / ".git").exists() or (sub / config.CONN_NAME).exists():
                continue
            keep.append(x)
        dirs[:] = keep
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
        "re-anchored": "~", "re-keyed": "~", "skipped": ".", "missing": "?", "no-baseline": "?B", "error": "E"}


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
            if scope and not tree.maps(scope):
                raise SyncError("%s is not mapped in %s — map it first (`bizconnect notion map %s --section "
                                "H | --page URL|new`), or check the path" % (
                                    tree.path_of(scope), tree.path_of(MANIFEST), tree.path_of(scope)))
            tree.check_overlaps(scope)
            getattr(tree, verb)(scope)
        except SyncError as e:
            tree.note("error", scope or ".", str(e))
        except SystemExit as e:                     # a notion.py helper gave up (NotionError)
            tree.note("error", scope or ".", str(e))
        except Exception as e:                      # noqa: BLE001 — report it; keep what we learned
            tree.note("error", scope or ".", "%s: %s" % (type(e).__name__, e))
        finally:
            if verb != "status":                    # status makes no writes
                tree.save()
        c = _report(tree, verb)
        if fl["dry"]:
            print("  (dry run — nothing written)")
        blocking = {"push": ("conflict", "remote-ahead", "no-baseline", "error", "missing"),
                    "pull": ("conflict", "local-ahead", "no-baseline", "error", "missing"),
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
# when pages or headings are renamed or moved in Notion. `synced` (also the tool's)
# fingerprints each item's last sync, so every machine knows which side changed since:
# commit this file with the synced files.
"""


def cmd_link(argv):
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) < 2:
        sys.exit("link needs <dir> <hub-page-url>")
    d = Path(pos[0])
    d = (d if d.is_absolute() else Path.cwd() / d).resolve()
    if d.exists() and not d.is_dir():
        sys.exit("not a directory: %s" % pos[0])
    pid = N.norm_id(pos[1])
    pg = get_page(pid)
    if not _alive(pg):
        sys.exit("cannot read page %s — share it with the integration (••• → Connections)." % pid)
    if not d.exists():
        d.mkdir(parents=True)
        print("created folder %s" % d)
    m = d / MANIFEST
    if m.exists():
        y = _yaml_rt()
        data = y.load(m.read_text(encoding="utf-8")) or {}
        data["hub"] = pg.get("url") or pid
        import io
        buf = io.StringIO()
        y.dump(data, buf)
        _atomic_write(m, buf.getvalue())
        print("re-pointed %s at %r" % (m, page_title(pg)))
        return 0
    _atomic_write(m, MANIFEST_HEADER + "hub: %s\nmap: []\n" % (pg.get("url") or pid))
    print("created %s -> %r\n  %s\n  add entries with `bizconnect notion map <path> --section H | --page URL|new "
          "| --pages [URL|new] | --database [URL|new]`" % (m, page_title(pg), pg.get("url") or pid))
    try:
        rel = d.relative_to(_repo_root()).as_posix()
    except (ValueError, SystemExit):
        rel = str(d)
    print("\nTip: paste this into your repo's CLAUDE.md so agents keep both sides in step:\n")
    print(CLAUDE_SNIPPET.replace("<dir>", rel))
    return 0


CLAUDE_SNIPPET = """## Notion sync (biz-connect)
- `<dir>/notion.yaml` maps files in `<dir>` to Notion. Before working there: `bizconnect notion pull <dir>`.
- Edit the Markdown files, not the Notion pages. When done: `bizconnect notion push <dir>`, then commit
  the files and `notion.yaml`.
- Never `--force` over edits people made in Notion. On a refusal, run `bizconnect notion diff <file>`
  and reconcile (or ask).
"""


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
    mpath, rel = _manifest_and_rel(root, pos[0])
    from ruamel.yaml.comments import CommentedMap
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
    for key in ("page", "pages", "database"):           # a URL target: check it, show its title
        v = e.get(key)
        if v and str(v).lower() != "new":
            nid = N.norm_id(str(v))
            obj = (lambda r: r[1] if r[0] < 300 else None)(N.api("GET", "/databases/%s" % nid)) \
                if key == "database" else get_page(nid)
            if not _alive(obj):
                sys.exit("cannot read %s %s — wrong link, or not shared with the integration?" % (key, nid))
            print("  %s: %r" % (key, M.plain(obj.get("title")) if key == "database" else page_title(obj)))
    if e.get("section"):
        _check_new_section(root, mpath, e, "--create" in argv)
    _append_entry(mpath, e)
    print("mapped %s -> %s in %s" % (rel, {k: v for k, v in e.items() if k != "path"}, mpath))
    return 0


def _check_new_section(root, mpath, e, create):
    """`map --section`: is the heading on its page? `create: true` is written only for a heading
    that is genuinely new. A near-miss (a typo) or a heading nested in a column / toggle is
    reported instead, so push can't add a near-duplicate (`--create` overrides the near-miss)."""
    try:
        t = Tree(root, mpath, dry=True, out=lambda *a: None)
        cid = t.container_id({"key": e["path"], "kind": "section", "path": None, "entry": dict(e)})
        if not cid:                                  # its page is new too: so is the heading
            e["create"] = True
            print("  its page is created on the first push, with heading %r (create: true)" % e["section"])
            return
        blocks = t.page_blocks(cid)
    except (SyncError, SystemExit, Exception) as ex:     # noqa: BLE001 — checking is best-effort
        print("  (couldn't check heading %r on its page: %s)" % (e["section"], ex))
        return
    texts = [M.plain(b[b["type"]].get("rich_text")) for b in blocks if b.get("type") in HEADINGS]
    if _norm_heading(e["section"]) in {_norm_heading(x) for x in texts}:
        return
    nested = Tree._nested_heading(blocks, e["section"])
    if nested:
        print("  ! %s — mapped without `create: true`, so push reports it as missing" % nested)
        return
    close = difflib.get_close_matches(e["section"], texts, n=3, cutoff=0.6)
    if close and not create:
        print("  ! no heading %r on the page — did you mean %s? Mapped without `create: true` (push reports "
              "it as missing): fix the name in notion.yaml, or re-run `map` with --create to add a new heading"
              % (e["section"], " or ".join(repr(c) for c in close)))
        return
    e["create"] = True
    print("  heading %r isn't on the page yet — push will add it at the end (create: true)" % e["section"])


def _manifest_and_rel(root, arg):
    """(manifest governing a local path, that path relative to the manifest's folder)."""
    p = Path(arg)
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    mpath, _scope = manifest_for(root, str(p.parent if not p.exists() else p))
    return mpath, p.relative_to(mpath.parent).as_posix()


def _append_entry(mpath, e):
    y = _yaml_rt()
    data = y.load(mpath.read_text(encoding="utf-8")) or {}
    from ruamel.yaml.comments import CommentedSeq
    seq = data.get("map")
    if not isinstance(seq, list):
        seq = CommentedSeq()
        data["map"] = seq
    if any(_entry_path(x) == _entry_path(e) for x in seq):
        sys.exit("%s is already mapped in %s" % (e["path"], mpath))
    seq.append(e)
    import io
    buf = io.StringIO()
    y.dump(data, buf)
    _atomic_write(mpath, buf.getvalue())


def cmd_outline(argv):
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        sys.exit("outline needs <page|url|dir>")
    arg = pos[0]
    pth = Path(arg)
    if pth.is_dir():
        if not (pth / MANIFEST).is_file():
            sys.exit("no %s in %s — `bizconnect notion link %s <hub-url>` first, or pass a page URL"
                     % (MANIFEST, arg, arg))
        arg = str((_yaml_rt().load((pth / MANIFEST).read_text(encoding="utf-8-sig")) or {}).get("hub"))
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


PLACEHOLDER = re.compile(r"^\s*(\[.*\]|tbc|tbd|todo|to follow|to come|placeholder|\.\.\.|…)?\s*$", re.I)


def _block_text(b):
    t = b.get("type")
    return M.plain((b.get(t) or {}).get("rich_text")) if t else ""


def cmd_locate(argv):
    """Resolve a block link (…#<block-id>): its text, the section holding it, and the file that
    maps it. `--map <file>` maps an unmapped section to a new file and pulls it, ready to edit."""
    map_to = argv[argv.index("--map") + 1] if "--map" in argv and argv.index("--map") + 1 < len(argv) else None
    pos = [a for a in argv if not a.startswith("--") and a != map_to]
    if not pos:
        sys.exit("locate needs a block link: <page-url>#<block-id>")
    pid, bid = _url_ids(pos[0])
    if not bid:
        sys.exit("that link has no #<block-id> — for a whole page use `outline`")
    st, blk = N.api("GET", "/blocks/%s" % bid)
    if st >= 300 or not _alive(blk):
        sys.exit("cannot read block %s [%s] — deleted, or not shared with the integration?" % (bid, st))
    top = blk                                   # climb to its top-level ancestor on the page
    for _ in range(20):
        par = top.get("parent") or {}
        if par.get("type") != "block_id":
            pid = N.norm_id(par["page_id"]) if par.get("type") == "page_id" else pid
            break
        st, top = N.api("GET", "/blocks/%s" % par["block_id"])
        _die(st, top, "read parent block")
    kids = N.get_children(pid)
    idx = next((i for i, b in enumerate(kids) if b["id"] == top["id"]), None)
    if idx is None:
        sys.exit("block %s is not on page %s" % (bid, pid))
    head = kids[idx] if kids[idx].get("type") in HEADINGS else \
        next((b for b in reversed(kids[:idx]) if b.get("type") in HEADINGS), None)
    region = []
    if head is not None:
        lvl, start = heading_level(head), kids.index(head)
        for b in kids[start + 1:]:
            if b.get("type") in HEADINGS and heading_level(b) <= lvl:
                break
            region.append(b)
    text = _block_text(blk)
    ph = "  (looks like a placeholder)" if PLACEHOLDER.match(text) else ""
    print("block    %s  %s: %r%s" % (blk["id"], blk.get("type"), text[:120] + ("…" if len(text) > 120 else ""), ph))
    print("page     %s  %s" % (page_title(get_page(pid)), NOTION_URL + _hex(pid)))
    htext = _block_text(head) if head is not None else None
    if head is None:
        print("section  none — it sits above the page's first heading (only a whole-page mapping holds it)")
    else:
        print("section  %s %s  — %d block(s) under it%s" % ("#" * heading_level(head), htext, len(region),
                                                      " (only this one)" if len(region) == 1 and region[0]["id"] == top["id"] else ""))
    # which mapped file (if any) owns it — the innermost mapped section, or a whole-page entry
    root = _repo_root()
    hits, folder_of = [], None
    for m in find_manifests(root):
        try:
            t = Tree(root, m, dry=True, out=lambda *a: None)
        except SyncError:
            continue
        for k, it in t.items.items():
            try:
                if it["kind"] == "section" and t.container_id(it) == pid:
                    _p, h, reg = t.find_section(it)
                    if h is not None and top["id"] in {h["id"]} | {b["id"] for b in reg}:
                        hits.append((len(reg), m, k))
                elif it["kind"] == "page" and t._id(it) == pid:
                    if t.body_less(it):              # a folder page's own text syncs only via its README
                        folder_of = (m.parent / k).relative_to(root).as_posix()
                    else:
                        hits.append((10 ** 9, m, k))
            except (SyncError, SystemExit):
                continue
    rel = lambda m, k: (m.parent / k).relative_to(root).as_posix()
    if hits:
        _n, m, k = min(hits, key=lambda h: h[0])
        f = rel(m, k)
        print("mapped   %s  (in %s)" % (f, m.relative_to(root).as_posix()))
        print("next     notion pull %s  →  edit that block's text in the file  →  notion push %s" % (f, f))
        return 0
    if not map_to:
        print("mapped   no" + ("  (%s is a folder of pages; the folder page's own text isn't synced — "
                               "add %sREADME.md to sync all of it)" % (folder_of, folder_of) if folder_of else ""))
        if head is not None:
            print("next     notion locate <this link> --map <project-folder>/<name>.md   (maps %r, pulls it)" % htext)
        return 0
    if head is None:
        sys.exit("can't --map: the block isn't under a heading")
    mpath, frel = _manifest_and_rel(root, map_to)
    if (mpath.parent / frel).exists():
        sys.exit("%s already exists — pick a new file name, or map it by hand with `map`" % map_to)
    from ruamel.yaml.comments import CommentedMap
    e = CommentedMap()
    e["path"], e["section"], e["id"] = frel, htext, head["id"]
    man = _yaml_rt().load(mpath.read_text(encoding="utf-8")) or {}
    if N.norm_id(str(man.get("hub"))) != pid:
        e["in"] = NOTION_URL + _hex(pid)
    _append_entry(mpath, e)
    t = Tree(root, mpath)
    t.pull(frel)
    t.save()
    _report(t, "pull")
    f = rel(mpath, frel)
    print("next     edit %s (write only that block's replacement)  →  notion push %s" % (f, f))
    return 0


def limit_problem(blocks):
    """Why Notion would reject (or we would have to truncate) these blocks, else None."""
    def pieces(rich):
        n = 0
        for r in M._merge(rich or []):
            c = M._seg_parts(r)[0]
            n += max(1, -(-len(c) // 1900))
        return n

    for b in blocks:
        t = b.get("type")
        body = b.get(t) or {}
        rich = body.get("rich_text") or []
        if pieces(rich) > 100:
            snippet = M.plain(rich)[:60].replace("\n", " ")
            return ("a %s starting %r needs %d rich-text pieces (styled runs, links, 1900-char chunks); "
                    "Notion allows 100 — split it" % (t.replace("_", " "), snippet, pieces(rich)))
        if t == "table":
            for r in body.get("children", []):
                for c in r["table_row"]["cells"]:
                    if pieces(c) > 100:
                        return "a table cell has too much styled text for Notion (100 pieces max) — simplify it"
        sub = limit_problem(body.get("children") or []) if t != "table" else None
        if sub:
            return sub
    return None


def cmd_diff(argv):
    """Show how a mapped file differs from its Notion side (Notion first, then local)."""
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        sys.exit("diff needs a mapped file path")
    root = _repo_root()
    mpath, scope = manifest_for(root, pos[0])
    try:
        t = Tree(root, mpath, dry=True, out=lambda *a: None)
        it = t.items.get(scope or "")
        if it is None or not scope or scope.endswith("/"):
            sys.exit("%s is not a mapped file in %s — diff needs one mapped Markdown file, not a folder"
                     % (pos[0], mpath))
        if it["kind"] in ("page", "section"):
            v = t.doc_view(it)
            if v.get("missing"):
                t.judge(v)
                sys.exit(it.get("_why") or "its section wasn't found")
            remote, local = v["remote_canon"], v["local_canon"]
        elif it["kind"] == "row":
            db = next(x for x in t.items.values() if x["kind"] == "db" and x["entry"] is it["entry"])
            did, dbo, rows, by_key, keyless, sch, tk = t.collection(db)
            row, pg = rows.get(scope), by_key.get(scope)
            if row is None:
                sys.exit("%s couldn't be read: %s" % (scope, t.bad_rows.get(scope, "")))
            lvals = t._local_row_values(row, sch, tk)
            local = dump_front_matter(sorted(lvals.items())) + t.render_local(row["blocks"], scope)
            remote = None
            if pg is not None:
                rvals, blocks, _rc = t._remote_row(db, pg, scope, sch, fetch=True)
                remote = dump_front_matter(sorted(rvals.items())) + t.render_remote(db, blocks, scope)
        else:
            sys.exit("diff works on mapped Markdown files (pages, sections, notes, rows)")
    except SyncError as e:
        sys.exit(str(e))
    if remote is None:
        print("%s is not in Notion yet" % scope)
        return 0
    lines = list(difflib.unified_diff(remote.splitlines(), local.splitlines(), "notion: " + scope,
                                      "local: " + scope, lineterm=""))
    print("\n".join(lines) if lines else "no differences (%s)" % scope)
    return 0
