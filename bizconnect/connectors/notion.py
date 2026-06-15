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


VERBS = {"whoami": cmd_whoami, "check": cmd_check, "read": cmd_read,
         "upload": cmd_upload, "fill": cmd_fill}


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
