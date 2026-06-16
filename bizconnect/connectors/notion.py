"""notion — read pages, upload local media, and manage notes on Notion.

Division of labour (mirrors the proven notion-tools pattern):
  * Search / read / write TEXT      -> the Notion MCP (notion-fetch, notion-search,
                                       notion-update-page). Richer; nothing to add here.
  * Import a LOCAL file (image/PDF/  -> THIS tool. The MCP's image syntax is URL-only;
    video/audio) onto a page           the File Upload API is the only way to attach a
                                        local file.
  * Headless read / access pre-flight -> THIS tool (no MCP/OAuth needed; uses the token).

Stdlib only (urllib). Token + version come from the central store (secrets.env):
NOTION_TOKEN (required), NOTION_VERSION (optional, default 2022-06-28). A repo's
default notes page can be set in connections.yaml under `notion.notes_page`, so
verbs accept "." to mean "this repo's notes page".

Verbs
-----
  whoami                              show which integration the token belongs to
  check  <page|url|.>                 token + access pre-flight for a page
  read   <page|url|.> [--depth N]     dump a page as Markdown
  upload <page|url|.> <file>... [--caption T] [--after BLOCK_ID]
  fill   <page|url|.> --dir DIR       swap [[img: NAME | CAPTION]] placeholders for uploads
  sync   <page|url|.> --out DIR       mirror a hub page (sub-pages, databases, files,
                                        links) into a local dir [--exclude id,id]
                                        [--depth N] [--no-files] [--no-follow]
"""
from __future__ import annotations

import glob as _glob
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .. import config

API = "https://api.notion.com/v1"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}
PDF_EXT = {".pdf"}
# Explicit fallbacks for extensions Python's registry-driven mimetypes often misses
# on Windows (would otherwise become application/octet-stream and be rejected as media).
EXPLICIT_MIME = {".webp": "image/webp", ".heic": "image/heic", ".svg": "image/svg+xml",
                 ".m4v": "video/x-m4v", ".m4a": "audio/mp4", ".aac": "audio/aac"}


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _token():
    return config.secret("NOTION_TOKEN", required=True)


def _version():
    return config.secret("NOTION_VERSION", default="2022-06-28")


# ------------------------------------------------------------------------- ids
def norm_id(s: str) -> str:
    """Accept a raw id, dashed id, or any Notion URL; return a dashed UUID.

    The id is the LAST 32 hex chars of the final path segment (Notion appends it to
    the title slug), so we strip query/fragment, take the last segment, keep hex
    only, and use the trailing 32 — never the first hex run (slugs contain stray hex).
    """
    if s == ".":
        page = config.get_path(config.load_connections()[0], "notion.notes_page")
        if not page:
            sys.exit("'.' means this repo's notion.notes_page, but it isn't set in connections.yaml.")
        s = page
    seg = s.strip().split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    hexonly = re.sub(r"[^0-9a-fA-F]", "", seg)
    if len(hexonly) < 32:
        sys.exit(f"could not find a Notion id in: {s!r}")
    h = hexonly[-32:].lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ------------------------------------------------------------------------- http
def _headers(json_body=True):
    h = {"Authorization": f"Bearer {_token()}", "Notion-Version": _version()}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def api(method, path, body=None, raw_url=None, headers=None, data_bytes=None):
    url = raw_url or (API + path)
    if data_bytes is not None:
        data = data_bytes
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or _headers()).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}


def _die(status, body, what):
    if status >= 300:
        sys.exit(f"{what} failed [{status}]: {body.get('message') or body}")


# --------------------------------------------------------------------- uploads
def infer_type(path: Path):
    ext = path.suffix.lower()
    block = ("image" if ext in IMAGE_EXT else "pdf" if ext in PDF_EXT
             else "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else "file")
    ctype = mimetypes.guess_type(path.name)[0] or EXPLICIT_MIME.get(ext) or "application/octet-stream"
    return block, ctype


def upload_file(path: Path) -> str:
    size = path.stat().st_size
    if size > 20 * 1024 * 1024:
        sys.exit(f"{path.name} is {size/1e6:.1f}MB > 20MB single-part limit (multi-part not implemented)")
    _, ctype = infer_type(path)
    status, body = api("POST", "/file_uploads", body={"filename": path.name, "content_type": ctype})
    _die(status, body, f"create upload for {path.name}")
    fid = body["id"]
    upload_url = body.get("upload_url") or f"{API}/file_uploads/{fid}/send"
    boundary = "----bizconnect" + uuid.uuid4().hex
    pre = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
           f"Content-Type: {ctype}\r\n\r\n").encode()
    multipart = pre + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    hdrs = {"Authorization": f"Bearer {_token()}", "Notion-Version": _version(),
            "Content-Type": f"multipart/form-data; boundary={boundary}"}
    status, body = api("POST", "", raw_url=upload_url, headers=hdrs, data_bytes=multipart)
    _die(status, body, f"send bytes for {path.name}")
    if body.get("status") != "uploaded":
        sys.exit(f"{path.name}: unexpected status {body.get('status')}")
    return fid


def rich(text):
    return [{"type": "text", "text": {"content": text}}] if text else []


def media_block(block_type, file_upload_id, caption=""):
    return {"type": block_type,
            block_type: {"type": "file_upload", "file_upload": {"id": file_upload_id},
                         "caption": rich(caption)}}


def attach(parent_id, children, after=None):
    body = {"children": children}
    if after:
        body["after"] = after
    status, body = api("PATCH", f"/blocks/{parent_id}/children", body=body)
    _die(status, body, "attach block")
    return [b["id"] for b in body.get("results", [])]


# --------------------------------------------------------------------- reading
def get_children(block_id):
    out, cursor = [], None
    while True:
        q = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            q += f"&start_cursor={cursor}"
        status, body = api("GET", q)
        _die(status, body, "list children")
        out.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        cursor = body.get("next_cursor")
    return out


def plain_text(block):
    t = block.get(block.get("type"), {})
    return "".join(r.get("plain_text", "") for r in t.get("rich_text", []))


def walk(parent_id, depth=0, max_depth=4):
    if depth >= max_depth:
        return
    for b in get_children(parent_id):
        yield b, parent_id, depth
        if b.get("has_children"):
            yield from walk(b["id"], depth + 1, max_depth)


# --------------------------------------------------------------------- commands
def cmd_whoami(_argv):
    status, body = api("GET", "/users/me")
    _die(status, body, "whoami")
    bot = body.get("bot", {})
    print(f"integration: {body.get('name')!r}  id={body.get('id')}  "
          f"type={body.get('type')}  owner={bot.get('owner', {}).get('type')}")


def cmd_check(argv):
    if not argv:
        sys.exit("check needs <page|url|.>")
    pid = norm_id(argv[0])
    status, body = api("GET", f"/pages/{pid}")
    if status >= 300:
        print(f"NO ACCESS [{status}]: {body.get('message')}")
        print("Fix: open the page -> ... -> Connections -> add the integration that owns "
              "NOTION_TOKEN, then re-run.")
        sys.exit(1)
    title = ""
    for p in body.get("properties", {}).values():
        if p.get("type") == "title":
            title = "".join(r.get("plain_text", "") for r in p.get("title", []))
            break
    print(f"OK — integration can access page {pid}")
    print(f"title: {title!r}")


def cmd_read(argv):
    if not argv:
        sys.exit("read needs <page|url|.>")
    pid = norm_id(argv[0])
    dv = _opt(argv, "--depth")
    if "--depth" in argv and dv is None:
        sys.exit("--depth needs a number")
    depth = int(dv) if dv else 4
    H = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
         "bulleted_list_item": "- ", "numbered_list_item": "1. ",
         "to_do": "- [ ] ", "quote": "> ", "callout": "> "}
    for b, _parent, d in walk(pid, max_depth=depth):
        bt, ind, txt = b.get("type"), "  " * d, plain_text(b)
        if bt in H:
            print(f"{ind}{H[bt]}{txt}")
        elif bt == "image":
            cap = "".join(r.get("plain_text", "") for r in b["image"].get("caption", []))
            print(f"{ind}![{cap}](<image>)")
        elif bt == "divider":
            print(f"{ind}---")
        elif txt:
            print(f"{ind}{txt}")


def cmd_upload(argv):
    if not argv:
        sys.exit("upload needs <page|url|.> <file>...")
    pid = norm_id(argv[0])
    caption, after, files, i = "", None, [], 1
    while i < len(argv):
        a = argv[i]
        if a == "--caption":
            if i + 1 >= len(argv):
                sys.exit("--caption needs a value")
            caption = argv[i + 1]; i += 2
        elif a == "--after":
            if i + 1 >= len(argv):
                sys.exit("--after needs a block id")
            after = norm_id(argv[i + 1]); i += 2
        else:
            files.append(Path(a)); i += 1
    if not files:
        sys.exit("no files given")
    for f in files:
        if not f.exists():
            sys.exit(f"missing file: {f}")
        bt, _ = infer_type(f)
        fid = upload_file(f)
        ids = attach(pid, [media_block(bt, fid, caption)], after=after)
        print(f"attached {f.name} as {bt} block {ids[0]}")


PLACEHOLDER = re.compile(r"\[\[\s*img:\s*([^\]|]+?)\s*(?:\|\s*(.*?)\s*)?\]\]")


def cmd_fill(argv):
    if not argv:
        sys.exit("fill needs <page|url|.> --dir DIR")
    pid = norm_id(argv[0])
    dv = _opt(argv, "--dir")
    if not dv:
        sys.exit("fill needs --dir DIR")
    d = Path(dv)
    if not d.is_absolute():                       # resolve against repo root, like the original
        root = config.repo_root()
        d = (root / d) if root else (Path.cwd() / d)
    found = []
    for b, parent, _depth in walk(pid):
        if b.get("type") != "paragraph":
            continue
        m = PLACEHOLDER.search(plain_text(b))
        if m:
            found.append((b["id"], parent, m.group(1).strip(), (m.group(2) or "").strip()))
    if not found:
        print("no [[img: ...]] placeholders found")
        return
    print(f"found {len(found)} placeholder(s)")
    for block_id, parent, name, caption in found:
        matches = [m for m in sorted(_glob.glob(str(d / f"{name}.*"))) if Path(m).suffix.lower() != ".json"]
        if not matches:
            print(f"  SKIP {name}: no file in {d}")
            continue
        path = Path(matches[0])
        bt, _ = infer_type(path)
        fid = upload_file(path)
        attach(parent, [media_block(bt, fid, caption)], after=block_id)
        api("DELETE", f"/blocks/{block_id}")
        print(f"  {name}: {path.name} -> {bt} (caption={caption!r})")


# ----------------------------------------------------------------- scrape (sync)
# Mirror a Notion "hub" page into a local directory: render each page to Markdown
# (links preserved), recurse into child/linked sub-pages, dump linked databases as
# tables, DOWNLOAD embedded file/pdf/image/video attachments, and catalogue every
# external URL. Visited-set + an exclude list (e.g. the self-referential output DBs
# that already round-trip via their own connectors) keep it from self-recursing.
TEXT_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
               "bulleted_list_item": "- ", "numbered_list_item": "1. ",
               "to_do": "- [ ] ", "quote": "> ", "callout": "> "}
MEDIA_BLOCKS = ("file", "pdf", "image", "video", "audio")
CONTAINER_BLOCKS = ("toggle", "column_list", "column", "synced_block")


def rich_md(rt, links=None):
    """rich_text array -> Markdown, preserving links/code/bold/italic. Append any
    external (non-relative) hrefs to `links` as (text, url)."""
    out = []
    for r in rt or []:
        txt = r.get("plain_text", "")
        if not txt and r.get("type") == "equation":
            txt = r.get("equation", {}).get("expression", "")
        ann = r.get("annotations", {}) or {}
        if ann.get("code"):
            txt = "`%s`" % txt
        if ann.get("bold"):
            txt = "**%s**" % txt
        if ann.get("italic"):
            txt = "*%s*" % txt
        href = r.get("href")
        if href:
            if links is not None and not href.startswith("/"):
                links.append((txt or href, href))
            txt = "[%s](%s)" % (txt or href, href)
        out.append(txt)
    return "".join(out)


def _file_url(payload):
    if not isinstance(payload, dict):
        return None, None
    t = payload.get("type")
    if t == "file":
        return payload.get("file", {}).get("url"), "file"
    if t == "external":
        return payload.get("external", {}).get("url"), "external"
    return None, None


def _fetch_bytes(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "biz-connect-notion-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _slug(title, nid):
    s = re.sub(r"[^0-9A-Za-z._-]+", "-", (title or "untitled").strip()).strip("-").lower() or "page"
    return "%s.%s" % (s[:60], (nid or "").replace("-", "")[:8])


def _prop_value(p):
    t = p.get("type"); v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(r.get("plain_text", "") for r in v or [])
    if t in ("select", "status"):
        return (v or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in v or [])
    if t == "date":
        if not v:
            return ""
        return (v.get("start") or "") + (" → " + v["end"] if v.get("end") else "")
    if t == "number":
        return "" if v is None else str(v)
    if t == "checkbox":
        return "✓" if v else ""
    if t in ("url", "email", "phone_number"):
        return v or ""
    if t == "people":
        return ", ".join(x.get("name", "") for x in v or [])
    if t == "relation":
        return ", ".join((x.get("id", "") or "").replace("-", "")[:8] for x in v or [])
    if t == "files":
        return ", ".join(f.get("name", "") for f in v or [])
    if t == "formula":
        f = v or {}
        return str(f.get(f.get("type"), ""))
    if t == "rollup":
        f = v or {}
        return str(f.get(f.get("type"), ""))
    return ""


class _Scraper:
    def __init__(self, out_dir, exclude, max_depth, download_files, follow_links, catalog_links,
                 flat=False):
        self.out = Path(out_dir)
        self.exclude = set(exclude or [])
        self.max_depth = max_depth
        self.download_files = download_files
        self.follow_links = follow_links
        self.catalog_links = catalog_links
        # flat: directed page->markdown render (page_to_markdown). Never write sidecar files,
        # never query/dump linked databases, never recurse — just render this page's own blocks.
        self.flat = flat
        self.visited_pages, self.visited_dbs = set(), set()
        self.links = []                # (text, url, source-rel)
        self.manifest = {"root": None, "pages": [], "databases": [], "files": [], "links": []}
        self.refreshed = 0
        self._by_url = {}              # downloaded url -> rel path (dedupe)

    # -- io
    def _write(self, rel, text):
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        changed = (not p.exists()) or p.read_bytes() != data
        if changed:
            p.write_bytes(data); self.refreshed += 1
        return rel

    def download(self, url, kind):
        if url in self._by_url:
            return self._by_url[url]
        name = re.sub(r"[^0-9A-Za-z._-]+", "_", url.split("?")[0].rstrip("/").split("/")[-1] or "")
        if not name or "." not in name:
            name = "%s-%s%s" % (kind, uuid.uuid4().hex[:8], "" if "." in name else "")
        rel = "_files/" + name
        if (self.out / rel).exists() and self._by_url.get(url) is None and rel in self._by_url.values():
            stem, dot, ext = name.partition(".")
            rel = "_files/%s-%s%s%s" % (stem, uuid.uuid4().hex[:6], dot, ext)
        try:
            data = _fetch_bytes(url)
        except Exception as e:                       # noqa: BLE001 — a dead attachment shouldn't abort the scrape
            sys.stderr.write("  ! download failed (%s): %s\n" % (kind, e))
            return None
        dest = self.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if (not dest.exists()) or dest.read_bytes() != data:
            dest.write_bytes(data); self.refreshed += 1
        self._by_url[url] = rel
        self.manifest["files"].append({"name": Path(rel).name, "url": url.split("?")[0],
                                       "path": rel, "bytes": len(data)})
        return rel

    # -- notion
    def page_title(self, pid):
        st, body = api("GET", "/pages/%s" % pid)
        if st >= 300:
            return None
        for pr in body.get("properties", {}).values():
            if pr.get("type") == "title":
                return "".join(r.get("plain_text", "") for r in pr.get("title", [])) or "untitled"
        return "untitled"

    def scrape_page(self, pid, depth, rel=None):
        if pid in self.visited_pages or pid in self.exclude:
            return None
        self.visited_pages.add(pid)
        title = self.page_title(pid) or "untitled"
        if rel is None:
            rel = _slug(title, pid) + ".md"
        page_links = []
        body = self.render_children(pid, depth, page_links, rel)
        header = ("# %s\n\n> Notion page `%s` — mirrored by `biz-connect notion sync`.\n\n" % (title, pid))
        self._write(rel, header + body + "\n")
        self.manifest["pages"].append({"id": pid, "title": title, "path": rel, "depth": depth})
        for text, url in page_links:
            self.links.append((text, url, rel))
        return rel

    def dump_database(self, did, depth, source_rel):
        if did in self.visited_dbs or did in self.exclude:
            return None
        self.visited_dbs.add(did)
        st, meta = api("GET", "/databases/%s" % did)
        title = "database"
        cols = []
        if st < 300:
            title = "".join(r.get("plain_text", "") for r in meta.get("title", [])) or "database"
            props = meta.get("properties", {})
            titles = [k for k, v in props.items() if v.get("type") == "title"]
            cols = titles + [k for k in props if k not in titles]
        rows = self._query_db(did)
        rel = "databases/" + _slug(title, did) + ".md"
        out = ["# %s\n" % title, "> Notion database `%s` — %d rows, mirrored by `biz-connect notion sync`.\n" % (did, len(rows))]
        if cols:
            out.append("| " + " | ".join(cols) + " |")
            out.append("|" + "|".join(["---"] * len(cols)) + "|")
            for row in rows:
                pr = row.get("properties", {})
                cells = [(_prop_value(pr.get(c, {})) or "").replace("\n", " ").replace("|", "\\|") for c in cols]
                out.append("| " + " | ".join(cells) + " |")
        else:
            out.append("_(could not read schema; %d rows)_" % len(rows))
        self._write(rel, "\n".join(out) + "\n")
        self.manifest["databases"].append({"id": did, "title": title, "path": rel, "rows": len(rows)})
        return rel, title

    def _query_db(self, did):
        out, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            st, resp = api("POST", "/databases/%s/query" % did, body=body)
            if st >= 300:
                break
            out.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return out

    # -- rendering
    def render_children(self, block_id, depth, links, source_rel, indent=0):
        return "\n".join(x for x in (self.render_block(b, depth, links, source_rel, indent)
                                     for b in get_children(block_id)) if x)

    def render_block(self, b, depth, links, source_rel, indent):
        bt, pad = b.get("type"), "  " * indent

        if bt == "child_page":
            title = b.get("child_page", {}).get("title", "untitled")
            rel = self.scrape_page(b["id"], depth + 1) if depth + 1 <= self.max_depth else None
            return "%s- 📄 [%s](%s)" % (pad, title, rel) if rel else "%s- 📄 %s _(not expanded)_" % (pad, title)

        if bt == "child_database":
            title = b.get("child_database", {}).get("title", "database")
            if self.flat:                              # flat page render: never query/dump a DB
                return "%s- 🗄️ %s _(not expanded)_" % (pad, title)
            res = self.dump_database(b["id"], depth, source_rel)
            return "%s- 🗄️ [%s](%s)" % (pad, title, res[0]) if res else "%s- 🗄️ %s _(excluded)_" % (pad, title)

        if bt == "link_to_page":
            lp = b.get("link_to_page", {}); tt = lp.get("type"); tgt = lp.get(tt or "")
            if self.follow_links and tt == "page_id" and depth + 1 <= self.max_depth:
                rel = self.scrape_page(tgt, depth + 1)
                if rel:
                    return "%s- 🔗 [linked page](%s)" % (pad, rel)
            if self.follow_links and tt == "database_id":
                res = self.dump_database(tgt, depth, source_rel)
                if res:
                    return "%s- 🔗 [linked database](%s)" % (pad, res[0])
            return "%s- 🔗 _(link to %s %s)_" % (pad, tt, (tgt or "").replace("-", "")[:8])

        if bt in MEDIA_BLOCKS:
            payload = b.get(bt, {})
            url, kind = _file_url(payload)
            cap = rich_md(payload.get("caption", []), links) or bt
            if url and self.download_files and kind == "file":
                rel = self.download(url, bt)
                if rel:
                    return "%s%s[%s](%s)" % (pad, "!" if bt == "image" else "", cap, rel)
            if url:
                links.append((cap, url))
                return "%s[%s](%s)" % (pad, cap, url)
            return None

        if bt == "table":
            rows, lines = get_children(b["id"]), []
            for i, rw in enumerate(rows):
                cells = rw.get("table_row", {}).get("cells", [])
                lines.append("%s| %s |" % (pad, " | ".join(rich_md(c, links).replace("|", "\\|") for c in cells)))
                if i == 0:
                    lines.append("%s|%s" % (pad, "|".join(["---"] * len(cells))))
            return "\n".join(lines)

        if bt == "code":
            code = "".join(r.get("plain_text", "") for r in b.get("code", {}).get("rich_text", []))
            return "%s```%s\n%s\n%s```" % (pad, b.get("code", {}).get("language", ""), code, pad)

        if bt == "divider":
            return "%s---" % pad
        if bt == "equation":
            return "%s$$%s$$" % (pad, b.get("equation", {}).get("expression", ""))

        if bt == "bookmark" or bt == "embed" or bt == "link_preview":
            url = b.get(bt, {}).get("url", "")
            cap = rich_md(b.get(bt, {}).get("caption", []), None) or url
            if url:
                links.append((cap, url))
            return "%s- 🔗 [%s](%s)" % (pad, cap, url) if url else None

        if bt in CONTAINER_BLOCKS:
            txt = rich_md(b.get(bt, {}).get("rich_text", []), links) if isinstance(b.get(bt), dict) else ""
            inner = self.render_children(b["id"], depth, links, source_rel, indent + (1 if bt == "toggle" else 0)) if b.get("has_children") else ""
            head = "%s- %s" % (pad, txt) if (bt == "toggle" and txt) else ""
            return "\n".join(x for x in (head, inner) if x) or None

        # generic text-bearing block
        payload = b.get(bt, {})
        txt = rich_md(payload.get("rich_text", []), links) if isinstance(payload, dict) else ""
        line = (pad + TEXT_PREFIX.get(bt, "") + txt) if txt else (pad + TEXT_PREFIX[bt] if bt in TEXT_PREFIX else None)
        if b.get("has_children"):
            inner = self.render_children(b["id"], depth, links, source_rel, indent + 1)
            if inner:
                line = (line + "\n" + inner) if line else inner
        return line

    def finalize(self, root_id):
        self.manifest["root"] = root_id
        if self.catalog_links:
            seen, rows = set(), []
            for text, url, src in self.links:
                if url in seen:
                    continue
                seen.add(url)
                rows.append("| %s | %s | %s |" % ((text or "(link)").replace("|", "\\|"), url, src))
            md = ["# External links referenced from the hub\n",
                  "> Catalogued by `biz-connect notion sync`. One row per distinct external URL.\n",
                  "| Link text | URL | Found in |", "|---|---|---|", *rows]
            self._write("_links.md", "\n".join(md) + "\n")
            self.manifest["links"] = [{"text": t, "url": u, "source": s} for (t, u, s) in self.links]
        self._write("_manifest.json", json.dumps(self.manifest, indent=2, ensure_ascii=False))


def sync_to_dir(page, out_dir, *, exclude=None, max_depth=3, download_files=True,
                follow_links=True, catalog_links=True, repo_root=None):
    """Mirror a Notion hub `page` into `out_dir` (a directory). Returns a summary dict.
    `exclude` is a list of page/database ids or URLs to never descend into."""
    out = Path(out_dir)
    if not out.is_absolute():
        out = (Path(repo_root) / out) if repo_root else (Path.cwd() / out)
    root_id = norm_id(page)
    exc = set()
    for x in (exclude or []):
        try:
            exc.add(norm_id(str(x)))
        except SystemExit:
            pass
    sc = _Scraper(out, exc, int(max_depth), bool(download_files), bool(follow_links), bool(catalog_links))
    sc.scrape_page(root_id, 0, rel="index.md")
    sc.finalize(root_id)
    return {"pages": len(sc.manifest["pages"]), "databases": len(sc.manifest["databases"]),
            "files": len(sc.manifest["files"]), "links": len(sc.manifest["links"]),
            "refreshed": sc.refreshed}


def page_to_markdown(page) -> str:
    """Render ONE Notion page's OWN blocks to a Markdown string (directed page->file sync).

    Flat by design: the title is dropped (the file body is the content), child pages are
    NOT expanded, linked pages/databases are NOT followed, and media is NOT downloaded —
    definitional docs are plain prose/prompt text. Headings, lists, paragraphs and FENCED
    CODE BLOCKS are rendered faithfully via the shared `_Scraper` block renderer, and inline
    text (including `{{PLACEHOLDER}}` tokens) is preserved verbatim.

    Reuses the exact same `render_children`/`render_block`/`rich_md` path as `sync_to_dir`,
    but configured to never recurse or fetch: max_depth=0 keeps `child_page`/`link_to_page`
    from expanding, and download_files/follow_links are off."""
    pid = norm_id(page)
    sc = _Scraper(out_dir=Path("."), exclude=None, max_depth=0,
                  download_files=False, follow_links=False, catalog_links=False, flat=True)
    body = sc.render_children(pid, 0, [], None)
    return body.strip() + "\n" if body.strip() else ""


def cmd_sync(argv):
    if not argv or argv[0].startswith("--"):
        sys.exit("sync needs <page|url|.> --out DIR [--exclude id,id] [--depth N] [--no-files] [--no-follow]")
    out = _opt(argv, "--out")
    if not out:
        sys.exit("sync needs --out DIR")
    exclude = [x for x in re.split(r"[,\s]+", _opt(argv, "--exclude", "") or "") if x]
    summary = sync_to_dir(argv[0], out, exclude=exclude, max_depth=int(_opt(argv, "--depth", "3")),
                          download_files="--no-files" not in argv, follow_links="--no-follow" not in argv,
                          repo_root=config.repo_root())
    print("notion sync %s -> %s" % (norm_id(argv[0]), out))
    print("  pages=%(pages)d  databases=%(databases)d  files=%(files)d  links=%(links)d  (refreshed %(refreshed)d)" % summary)


VERBS = {"whoami": cmd_whoami, "check": cmd_check, "read": cmd_read,
         "upload": cmd_upload, "fill": cmd_fill, "sync": cmd_sync}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown notion verb {verb!r}. One of: {', '.join(VERBS)}")
    fn(rest)
    return 0
