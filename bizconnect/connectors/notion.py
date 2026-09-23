"""notion — sync local files with Notion, read pages, upload local media.

Division of labour:
  * Keep project FILES and Notion    -> THIS tool: `link / map / outline / locate / diff /
    in two-way sync                    status / push / pull` (a notion.yaml maps each file to
                                        a page, a section under a heading, a folder of pages
                                        or a database — see notiontree.py).
  * Ad-hoc search / edits by hand    -> the Notion MCP (notion-fetch, notion-search, ...).
  * Import a LOCAL file (image/PDF/  -> THIS tool (`upload` / `fill`): the File Upload API.
    video/audio) onto a page
  * Headless read / access pre-flight -> THIS tool (no MCP/OAuth needed; uses the token).

Stdlib only (urllib). Token + version come from the central store (secrets.env):
NOTION_TOKEN (required), NOTION_VERSION (optional, default 2022-06-28 — leave it unset:
the two-way sync pins 2022-06-28 whatever it says, via PINNED_VERSION). A repo's
default notes page can be set in connections.yaml under `notion.notes_page`, so
verbs accept "." to mean "this repo's notes page".

Errors raise NotionError, a SystemExit: a CLI run exits with the message as before,
while library callers (the tree sync) can catch it per item and carry on.

Verbs
-----
  whoami                              show which integration the token belongs to
  check  <page|url|.>                 token + access pre-flight for a page
  read   <page|url|.> [--depth N]     dump a page as Markdown
  upload <page|url|.> <file>... [--caption T] [--after BLOCK_ID]
  fill   <page|url|.> --dir DIR       swap [[img: NAME | CAPTION]] placeholders for uploads
  sync   <page|url|.> --out DIR       one-way MIRROR of a hub page (sub-pages, databases,
                                        files, links) into a local dir [--exclude id,id]
                                        [--depth N] [--no-files] [--no-follow]
  link | map | outline | locate | diff | status | push | pull    two-way mapped sync (below)
"""
from __future__ import annotations

import email.utils
import glob as _glob
import http.client
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from .. import config

API = "https://api.notion.com/v1"
# When set, overrides NOTION_VERSION for every call (the tree sync pins "2022-06-28": its
# database/rows code needs that API version's /databases/{id}/query and properties shape).
PINNED_VERSION = None


class NotionError(SystemExit):
    """A Notion call (or id parse) failed. A SystemExit, so an uncaught one ends a CLI run with
    exactly the message `_die` always printed; library callers can catch it and carry on."""


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
    return PINNED_VERSION or config.secret("NOTION_VERSION", default="2022-06-28")


# ------------------------------------------------------------------------- ids
def _dashed(h):
    h = h.lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def norm_id(s: str) -> str:
    """Accept a raw id, dashed id, or any Notion URL; return a dashed UUID.

    A side-peek URL (`.../Hub-<hub id>?p=<page id>&pm=s`, what the address bar shows while a
    sub-page is open in peek mode) means the PEEKED page, so a `p=` query id wins. Otherwise
    the id is the LAST 32 hex chars of the final path segment (Notion appends it to the
    title slug): strip query/fragment, take the last segment, keep hex only, and use the
    trailing 32 — never the first hex run (slugs contain stray hex).
    """
    if s == ".":
        page = config.get_path(config.load_connections()[0], "notion.notes_page")
        if not page:
            raise NotionError("'.' means this repo's notion.notes_page, but it isn't set in connections.yaml.")
        s = str(page)
    s = str(s)
    query = s.strip().split("#")[0].partition("?")[2]
    for p in urllib.parse.parse_qs(query).get("p", []):
        h = p.replace("-", "")
        if re.fullmatch(r"[0-9a-fA-F]{32}", h):
            return _dashed(h)
    seg = s.strip().split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    hexonly = re.sub(r"[^0-9a-fA-F]", "", seg)
    if len(hexonly) < 32:
        raise NotionError(f"could not find a Notion id in: {s!r}")
    return _dashed(hexonly[-32:])


# ------------------------------------------------------------------------- http
def _headers(json_body=True):
    h = {"Authorization": f"Bearer {_token()}", "Notion-Version": _version()}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


# Retry policy. 409 (conflict), 429 (rate limited) and 529 (overloaded) mean Notion did NOT
# apply the request, so any method may be resent. Other 5xx and network errors/timeouts are
# ambiguous — the write may already have landed — so only idempotent calls are resent: GET,
# DELETE, read-only POST queries, and a PATCH that SETS values (/pages/{id}, /blocks/{id},
# /databases/{id}: sending it twice leaves the same state). A create (POST), an append
# (PATCH …/children) or a file-upload send gets the error back and its caller stops. (A
# re-run diffs against what is really there, which is safe; a blind resend of an append or a
# create duplicates content.)
RETRY_ALWAYS = {409, 429, 529}
RETRY_STATUS = RETRY_ALWAYS | {500, 502, 503, 504}      # kept for callers; idempotent set
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE"}
_READ_ONLY_POST = re.compile(r"^/(?:search|(?:databases|data_sources)/[^/?]+/query)(?:[/?]|$)")
NETWORK_ERRORS = (OSError, http.client.HTTPException)   # URLError, timeouts (socket.timeout
MAX_WAIT = 300.0                                         # on 3.9), resets are all OSErrors


def _idempotent(method, path, raw_url=None):
    m = (method or "").upper()
    if m in IDEMPOTENT_METHODS:
        return True
    if raw_url:
        return False
    if m == "PATCH":
        return not (path or "").split("?")[0].rstrip("/").endswith("/children")
    return m == "POST" and bool(_READ_ONLY_POST.match(path or ""))


def _retry_after(headers, default):
    """Seconds to wait: the Retry-After header (seconds or an HTTP date) if any, else `default`."""
    v = headers.get("Retry-After") if headers is not None else None
    if not v:
        return default
    v = str(v).strip()
    try:
        secs = float(v)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(v)
            secs = (when - datetime.now(when.tzinfo)).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return default
    return min(max(secs, 0.0), MAX_WAIT) if secs == secs else default     # NaN -> default


def api(method, path, body=None, raw_url=None, headers=None, data_bytes=None, retries=5):
    """One Notion REST call -> (status, json).

    Rate limits and overload (429/529, honouring Retry-After) and 409 conflicts are retried
    with backoff for every method, so a long tree sync rides out Notion's ~3 req/s limit
    instead of failing half-way. Other 5xx and network errors are retried only for
    idempotent calls (see _idempotent); for a create or an append they come straight back
    (network errors as 599), since Notion may already have applied the write and resending it
    would duplicate content."""
    url = raw_url or (API + path)
    if data_bytes is not None:
        data = data_bytes
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    idem = _idempotent(method, path, raw_url)
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or _headers()).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except NETWORK_ERRORS:
                raw = ""
            retry = e.code in RETRY_ALWAYS or (idem and e.code >= 500)
            if retry and attempt < retries:
                time.sleep(_retry_after(e.headers, delay))
                delay = min(delay * 2, 30)
                continue
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw}
        except NETWORK_ERRORS as e:
            if not idem:
                return 599, {"message": "network error: %s — %s %s was not resent, since Notion may "
                                        "already have applied it; re-run to compare and continue"
                                        % (str(e) or type(e).__name__, method, path or url)}
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            return 599, {"message": "network error: %s" % (str(e) or type(e).__name__)}
    return 599, {"message": "retries exhausted"}


def _die(status, body, what):
    if status >= 300:
        msg = body.get("message") if isinstance(body, dict) else None
        raise NotionError(f"{what} failed [{status}]: {msg or body}")


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
        raise NotionError(f"{path.name} is {size/1e6:.1f}MB > 20MB single-part limit (multi-part not implemented)")
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
        raise NotionError(f"{path.name}: unexpected status {body.get('status')}")
    return fid


def rich(text):
    return [{"type": "text", "text": {"content": text}}] if text else []


def media_block(block_type, file_upload_id, caption=""):
    return {"type": block_type,
            block_type: {"type": "file_upload", "file_upload": {"id": file_upload_id},
                         "caption": rich(caption)}}


def attach(parent_id, children, after=None):
    """Append `children` (at the end, or after block `after`); return the NEW blocks' ids."""
    return append_children(parent_id, children, after=after)


# Request limits of the append-children endpoint (Notion "Request limits"): any array of
# blocks (or rich text) holds at most 100 elements, one request carries at most 1000 block
# elements in all, a request nests at most two levels below its top-level blocks, and the
# payload is capped at ~500KB (we stay under MAX_BYTES to leave headroom).
MAX_CHILDREN = 100          # elements in any one `children` array (so: blocks per append)
MAX_NEST = 2                # levels of nesting below a top-level block per request
MAX_BLOCKS = 1000           # block elements per request, counted recursively
MAX_BYTES = 450_000         # JSON bytes per request
_ENVELOPE = 512             # {"children": [...], "position": {...}} around the blocks
_KIDS_KEY = len(', "children": []')   # json.dumps' default separators
# Blocks that must be CREATED with children, and how many levels below themselves those
# span: a table needs its rows, a column list its columns, a column its content. Such a
# block is never sent where its required children would be cut by MAX_NEST.
_NEEDS_KIDS = {"table": 1, "column": 1, "column_list": 2}


def _jsize(obj):
    return len(json.dumps(obj))       # the same encoding api() sends


def _kids_of(block):
    t = block.get("type")
    body = block.get(t)
    return (body.get("children") or []) if isinstance(body, dict) else []


def _trim(block, level=0, budget=(MAX_BLOCKS, MAX_BYTES)):
    """Copy `block` into something one append request can carry.

    Returns (copy, deferred, n_blocks, n_bytes). In the copy every `children` list holds at
    most MAX_CHILDREN blocks, nesting stops at MAX_NEST levels, and the whole copy fits
    `budget` = (blocks, JSON bytes): a child goes in whole or not at all, and the first child
    always goes in (a table keeps its first rows; an over-large single leaf still gets sent,
    for Notion to judge). Everything cut is listed in `deferred` as [(index-path, children)]:
    append `children`, at the end and in list order, to the block reached from the copy by
    `index-path` once the copy exists (a table's extra rows are appended to the table)."""
    t = block.get("type")
    body = dict(block.get(t) or {})
    kids = list(body.pop("children", None) or [])
    out = {"type": t, t: body}
    n_blocks, n_bytes = 1, _jsize(out)
    if not kids:
        return out, [], n_blocks, n_bytes
    if level >= MAX_NEST:
        return out, [((), kids)], n_blocks, n_bytes
    n_bytes += _KIDS_KEY
    room = (budget[0] - n_blocks, budget[1] - n_bytes)
    trimmed, deferred = [], []
    for k, c in enumerate(kids):
        starved = level + 1 + _NEEDS_KIDS.get(c.get("type"), 0) > MAX_NEST and bool(_kids_of(c))
        if starved or len(trimmed) >= MAX_CHILDREN:
            deferred.append(((), kids[k:]))
            break
        cc, dd, cb, cy = _trim(c, level + 1, room)
        cy += 2 if trimmed else 0                   # the ", " before it
        if trimmed and (n_blocks + cb > budget[0] or n_bytes + cy > budget[1]):
            deferred.append(((), kids[k:]))
            break
        trimmed.append(cc)
        n_blocks += cb
        n_bytes += cy
        deferred.extend(((k,) + path, ch) for path, ch in dd)
    if trimmed:
        body["children"] = trimmed
    elif t in _NEEDS_KIDS:                          # e.g. a column opening with a table
        raise NotionError("cannot create a %s whose first child needs deeper nesting than one request allows" % t)
    else:
        n_bytes -= _KIDS_KEY
    return out, deferred, n_blocks, n_bytes


def _batches(children):
    """Group top-level blocks into requests of <= MAX_CHILDREN blocks, <= MAX_BLOCKS blocks in
    all and <= MAX_BYTES of JSON; each entry is [(trimmed block, its deferred list)]."""
    budget = (MAX_BLOCKS, MAX_BYTES - _ENVELOPE)
    out, cur, n_blocks, n_bytes = [], [], 0, 0
    for b in children:
        tb, dd, cb, cy = _trim(b, 0, budget)
        if cur and (len(cur) >= MAX_CHILDREN or n_blocks + cb > budget[0] or n_bytes + 2 + cy > budget[1]):
            out.append(cur)
            cur, n_blocks, n_bytes = [], 0, 0
        cur.append((tb, dd))
        n_blocks += cb
        n_bytes += cy + (2 if len(cur) > 1 else 0)
    if cur:
        out.append(cur)
    return out


def append_children(parent_id, children, after=None, at_start=False):
    """Insert blocks under `parent_id` — at the end (default), at the start, or after block
    `after` — and return the ids of the new top-level blocks, in order.

    Requests respect Notion's limits (see MAX_*): top-level blocks go in batches of <= 100
    blocks / <= 1000 blocks counted recursively / <= MAX_BYTES of JSON, and whatever a batch
    can't carry — nesting deeper than two levels, the 101st+ entry of any children list (e.g.
    table rows), children past the block/byte budget — is appended in follow-up calls once
    its parent exists, in order, by the same rules.

    (The API answers with the new blocks FIRST, followed by every later sibling, so the
    first len(batch) results are the ones we created.)"""
    ids = []
    for batch in _batches(children):
        trimmed = [tb for tb, _dd in batch]
        body = {"children": trimmed}
        if ids:
            body["position"] = {"type": "after_block", "after_block": {"id": ids[-1]}}
        elif at_start:
            body["position"] = {"type": "start"}
        elif after:
            body["position"] = {"type": "after_block", "after_block": {"id": after}}
        status, resp = api("PATCH", f"/blocks/{parent_id}/children", body=body)
        _die(status, resp, "append blocks")
        new = [b["id"] for b in resp.get("results", [])[:len(trimmed)]]
        if len(new) != len(trimmed):
            raise NotionError("append blocks: Notion returned %d of %d new blocks" % (len(new), len(trimmed)))
        ids.extend(new)
        listed = {}                      # block id -> its children, fetched once per batch
        for k, (_tb, deferred) in enumerate(batch):
            for path, kids in deferred:
                target = new[k]
                for i in path:           # later appends only add at the END, so a cached
                    if target not in listed:     # list's indices stay valid
                        listed[target] = get_children(target)
                    target = listed[target][i]["id"]
                append_children(target, kids)
    return ids


def delete_block(block_id):
    status, resp = api("DELETE", f"/blocks/{block_id}")
    if status >= 300 and status != 404:
        _die(status, resp, "delete block")


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
    def __init__(self, out_dir, exclude, max_depth, download_files, follow_links, catalog_links):
        self.out = Path(out_dir)
        self.exclude = set(exclude or [])
        self.max_depth = max_depth
        self.download_files = download_files
        self.follow_links = follow_links
        self.catalog_links = catalog_links
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
# mapped two-way sync of local files <-> Notion (notion.yaml): see notiontree.py
TREE_VERBS = ("link", "map", "outline", "locate", "diff", "status", "push", "pull")


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        from . import notiontree
        print(notiontree.__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    if verb in TREE_VERBS:
        from . import notiontree
        return getattr(notiontree, "cmd_" + verb)(rest) or 0
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown notion verb {verb!r}. One of: {', '.join(list(VERBS) + list(TREE_VERBS))}")
    fn(rest)
    return 0
