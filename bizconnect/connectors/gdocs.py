"""gdocs — sync a local Markdown file to/from a Google Doc.

Model
-----
A local Markdown file is the source of truth you and Claude edit. `push` converts
it into a Google Doc (Drive imports Markdown natively → real headings, bold, lists,
tables, links); `pull` exports the Doc back to Markdown. The binding (which local
file ↔ which Doc) lives in the repo's connections.yaml under `google.docs`; the
tool fills in the doc id/url on first push. Volatile sync metadata (content hashes,
the Doc's last-synced modifiedTime, timestamps) lives in a tool-owned, git-ignored
`.bizconnect/state.json` so connections.yaml stays clean and human-authored.

Both directions are guarded against clobbering: `pull` refuses to overwrite local
edits and `push` refuses to overwrite Doc-side edits — pass `--force` to override.

Verbs
-----
  push  <file> [--title T] [--folder ID] [--version V] [--new] [--force]   create/update the Doc
                                                     (--new = a NEW Doc instance for a major build)
  pull  <file> [--force]                              overwrite local Markdown from the Doc
  link  <file> <doc-url-or-id>                        bind an existing Doc to a local file
  status [file]                                       show drift (local vs last sync, + Doc mod time)
  list                                                list all bindings in this repo
  unlink <file>                                       remove a binding (does not delete the Doc)
  comments <file> [--json] [--out P]                  dump the Doc's comments (feedback capture)
  diff <file>                                         unified diff: Doc body vs local (direct edits)
  resolve <file> <commentId> [--note T]               resolve a comment thread (close-out)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .. import _google

DOC_MIME = "application/vnd.google-apps.document"
MD_MIME = "text/markdown"
STATE_DIR = ".bizconnect"
STATE_FILE = "state.json"


# --------------------------------------------------------------------------- io
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_sha(b: bytes) -> str:
    """Hash content with line endings normalised, so CRLF<->LF churn (very common on
    Windows / git autocrlf) doesn't register as a spurious edit in the drift guard."""
    return hashlib.sha256(b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def _drive(data=None):
    return _google.build("drive", "v3", [_google.DRIVE],
                         subject=_google.impersonation_subject(data))


def _doc_id_from(s: str) -> str:
    s = s.strip()
    # handles /document/d/, /document/u/0/d/, drive /file/d/, and ?id= forms
    m = re.search(r"/(?:document|file)/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" in s:
        sys.exit(f"could not find a Google Doc id in: {s!r}")
    return s


def _folder_id(s):
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", s)
    return m.group(1) if m else s


def _modified(drive, doc_id):
    try:
        return drive.files().get(fileId=doc_id, fields="modifiedTime",
                                 supportsAllDrives=True).execute().get("modifiedTime")
    except Exception:
        return None


def _git_sha(root):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# --------------------------------------------------------- repo binding + state
def _repo():
    data, path = config.require_connections()
    return data, path, path.parent


def _rel(local: Path, root: Path) -> str:
    p = local.resolve()
    try:
        return p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _docs_map(data, create=False):
    g = data.get("google")
    if g is None:
        if not create:
            return None
        from ruamel.yaml.comments import CommentedMap
        g = CommentedMap()
        data["google"] = g
    docs = g.get("docs")
    if docs is None:
        if not create:
            return None
        from ruamel.yaml.comments import CommentedMap
        docs = CommentedMap()
        g["docs"] = docs
    return docs


def _binding(data, key):
    docs = _docs_map(data)
    if not docs or key not in docs:
        return None
    b = docs[key]
    return b if isinstance(b, dict) else {"doc_id": b}


def _set_binding(data, path, key, doc_id, url):
    from ruamel.yaml.comments import CommentedMap
    docs = _docs_map(data, create=True)
    entry = docs.get(key)
    if not isinstance(entry, CommentedMap):
        entry = CommentedMap()
        docs[key] = entry
    entry["doc_id"] = doc_id
    entry["url"] = url
    config.save_connections(data, path)


def _state_path(root: Path) -> Path:
    return root / STATE_DIR / STATE_FILE


def _load_state(root):
    p = _state_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"docs": {}}


def _save_state(root, state):
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # keep tool-owned state out of git even if the user didn't gitignore it
    gi = p.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")


# ----------------------------------------------------------------- arg helpers
def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _positional(argv, value_flags=()):
    """First non-flag token, skipping any value that belongs to a value-taking flag."""
    skip = set()
    for vf in value_flags:
        if vf in argv:
            skip.add(argv.index(vf) + 1)
    for i, a in enumerate(argv):
        if i in skip or a.startswith("-"):
            continue
        return a
    return None


def _resolve_local(arg):
    p = Path(arg)
    return p if p.is_absolute() else Path.cwd() / p


# --------------------------------------------------------------------- commands
def cmd_push(argv):
    arg = _positional(argv, value_flags=("--title", "--folder", "--version"))
    if not arg:
        sys.exit("push needs <file>")
    local = _resolve_local(arg)
    title = _opt(argv, "--title")
    folder_override = _opt(argv, "--folder")
    version = _opt(argv, "--version")
    new = "--new" in argv          # force a NEW Doc instance (don't update in place)
    force = "--force" in argv
    if not local.exists():
        sys.exit(f"local file not found: {local}")

    data, conn_path, root = _repo()
    key = _rel(local, root)
    md = local.read_bytes()

    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(md, mimetype=MD_MIME, resumable=False)
    drive = _drive(data)

    binding = _binding(data, key)
    doc_id = binding.get("doc_id") if binding else None
    state = _load_state(root)
    st = state.get("docs", {}).get(key, {})

    try:
        if doc_id and not new:
            # guard: don't clobber edits made to the Doc since we last synced it
            if not force and st.get("synced_modified"):
                current = _modified(drive, doc_id)
                if current and current != st["synced_modified"]:
                    sys.exit(
                        f"the Google Doc bound to {key} changed since the last sync "
                        f"(modified {current}).\nPushing would overwrite those edits. "
                        f"`gdoc pull {key}` to take them, or `gdoc push {key} --force` to overwrite.")
            f = drive.files().update(
                fileId=doc_id, media_body=media,
                fields="id,name,webViewLink,modifiedTime",
                supportsAllDrives=True).execute()
            action = "updated"
        else:
            folder = _folder_id(folder_override) or _folder_id(config.get_path(data, "google.drive_folder"))
            name = title or local.stem
            if version:
                name = f"{name} — {version}"
            body = {"name": name, "mimeType": DOC_MIME}
            if folder:
                body["parents"] = [folder]
            f = drive.files().create(
                body=body, media_body=media,
                fields="id,name,webViewLink,modifiedTime",
                supportsAllDrives=True).execute()
            doc_id = f["id"]
            action = "created"
            url = f.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"
            _set_binding(data, conn_path, key, doc_id, url)
            _maybe_share(drive, doc_id, data, folder)
    except SystemExit:
        raise
    except Exception as e:
        _die_google(e, doc_id)

    url = f.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"
    # Re-fetch modifiedTime AFTER create+share (sharing bumps it), so the drift guard
    # and `status` don't see a phantom "doc changed" on the very next call.
    state.setdefault("docs", {})[key] = {
        **st, "doc_id": doc_id,
        "version": version or st.get("version"),
        "last_push_sha256": _content_sha(md),
        "last_push_at": _now(),
        "synced_modified": _modified(drive, doc_id) or f.get("modifiedTime"),
    }
    _save_state(root, state)
    print(f"{action} Google Doc from {key}")
    print(f"  {url}")
    if action == "created":
        print(f"  (binding written to {conn_path.name})")
    # Record this instance in the doc registry (no-op if none bound). A freshly created Doc
    # (first push or --new) is a NEW instance; an update just refreshes the current row.
    try:
        from . import docreg
        msg = docreg.log_instance(data, root, artifact=key, doc_id=doc_id, doc_url=url,
                                  version=version, content_sha=_content_sha(md),
                                  git_sha=_git_sha(root), new=(action == "created"))
        if msg:
            print(f"  registry: {msg}")
    except Exception as e:
        print(f"  (doc registry not updated: {e})")


def cmd_pull(argv):
    arg = _positional(argv)
    if not arg:
        sys.exit("pull needs <file>")
    local = _resolve_local(arg)
    force = "--force" in argv
    data, conn_path, root = _repo()
    key = _rel(local, root)
    binding = _binding(data, key)
    if not binding or not binding.get("doc_id"):
        sys.exit(f"no Doc bound to {key}. Run `bizconnect gdoc push {key}` "
                 f"or `bizconnect gdoc link {key} <doc-url>` first.")
    doc_id = binding["doc_id"]

    state = _load_state(root)
    st = state.get("docs", {}).get(key, {})
    if local.exists() and not force:
        cur = _content_sha(local.read_bytes())
        if cur not in (st.get("last_push_sha256"), st.get("last_pull_sha256")):
            sys.exit(f"local {key} has changes not yet pushed (would be overwritten).\n"
                     f"Push first, or re-run with --force to discard local changes.")

    drive = _drive(data)
    try:
        content = drive.files().export(fileId=doc_id, mimeType=MD_MIME).execute()
    except Exception as e:
        _die_google(e, doc_id)
    if isinstance(content, str):
        content = content.encode("utf-8")

    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(content)
    state.setdefault("docs", {})[key] = {
        **st, "doc_id": doc_id,
        "last_pull_sha256": _content_sha(content), "last_pull_at": _now(),
        "synced_modified": _modified(drive, doc_id),
    }
    _save_state(root, state)
    print(f"pulled Doc -> {key} ({len(content)} bytes)")


def cmd_link(argv):
    pos = [a for a in argv if not a.startswith("-")]
    if len(pos) < 2:
        sys.exit("link needs <file> <doc-url-or-id>")
    local = _resolve_local(pos[0])
    doc_id = _doc_id_from(pos[1])
    data, conn_path, root = _repo()
    key = _rel(local, root)
    drive = _drive(data)
    try:
        f = drive.files().get(fileId=doc_id, fields="id,name,mimeType,webViewLink",
                              supportsAllDrives=True).execute()
    except Exception as e:
        _die_google(e, doc_id)
    if f.get("mimeType") != DOC_MIME:
        sys.exit(f"{doc_id} is not a Google Doc (mimeType={f.get('mimeType')}).")
    url = f.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"
    _set_binding(data, conn_path, key, doc_id, url)
    print(f"linked {key} -> {f.get('name')!r}\n  {url}")


def cmd_unlink(argv):
    arg = _positional(argv)
    if not arg:
        sys.exit("unlink needs <file>")
    local = _resolve_local(arg)
    data, conn_path, root = _repo()
    key = _rel(local, root)
    docs = _docs_map(data)
    if docs and key in docs:
        del docs[key]
        config.save_connections(data, conn_path)
        print(f"unbound {key} (the Google Doc itself was not deleted)")
    else:
        print(f"no binding for {key}")


def cmd_status(argv):
    data, conn_path, root = _repo()
    docs = _docs_map(data) or {}
    only = _rel(_resolve_local(argv[0]), root) if argv and not argv[0].startswith("-") else None
    state = _load_state(root)
    if not docs:
        print("no Doc bindings in this repo. `bizconnect gdoc push <file>` creates one.")
        return
    drive = None
    for key, b in docs.items():
        if only and key != only:
            continue
        doc_id = b.get("doc_id") if isinstance(b, dict) else b
        st = state.get("docs", {}).get(key, {})
        local = root / key
        flags = []
        if not local.exists():
            flags.append("LOCAL MISSING")
        else:
            cur = _content_sha(local.read_bytes())
            if cur in (st.get("last_push_sha256"), st.get("last_pull_sha256")):
                flags.append("in sync")
            elif st.get("last_push_sha256") or st.get("last_pull_sha256"):
                flags.append("LOCAL AHEAD (unpushed edits)")
            else:
                flags.append("never pushed")
        modified = ""
        if doc_id:
            try:
                drive = drive or _drive(data)
                modified = _modified(drive, doc_id) or ""
                if modified and st.get("synced_modified") and modified != st["synced_modified"]:
                    flags.append("DOC CHANGED since last sync")
            except Exception as e:
                status, _ = _google.http_error(e)
                modified = f"[{status}]"
        print(f"{key}")
        print(f"  doc_id={doc_id}  doc_modified={modified}")
        print(f"  {' | '.join(flags)}  last_push={st.get('last_push_at','-')}")


def cmd_list(argv):
    data, conn_path, root = _repo()
    docs = _docs_map(data) or {}
    if not docs:
        print("no Doc bindings in this repo.")
        return
    for key, b in docs.items():
        url = b.get("url") if isinstance(b, dict) else ""
        print(f"{key}\n  {url}")


# ------------------------------------------------------------------- helpers
def _maybe_share(drive, doc_id, data, folder):
    """Share a newly-created Doc back to a human so it's not stranded in the SA's Drive."""
    share = config.get_path(data, "google.share_with")
    if not share:
        if not folder:
            print("  WARNING: new Doc is owned by the service account and not shared.\n"
                  "  Set google.share_with (your email) or google.drive_folder in connections.yaml.")
        return
    try:
        drive.permissions().create(
            fileId=doc_id, sendNotificationEmail=False,
            body={"type": "user", "role": "writer", "emailAddress": share},
            fields="id", supportsAllDrives=True).execute()
        print(f"  shared with {share} (Editor)")
    except Exception as e:
        status, detail = _google.http_error(e)
        print(f"  WARN could not share with {share} [{status}]: {detail}")


def _die_google(e, doc_id):
    status, detail = _google.http_error(e)
    email = _google.client_email() or "<service-account email>"
    print(f"Google API error [{status}]: {detail}")
    low = (detail or "").lower()
    if "quota" in low:
        print(
            "\nThe service account has no Drive storage of its own, so it cannot OWN a new\n"
            "Doc. Pick one (so the file is owned by you / a Shared Drive instead):\n"
            f"  A. Enable domain-wide delegation for {email} (scopes: drive, documents),\n"
            "     then set GOOGLE_IMPERSONATE_SUBJECT=you@domain in the central secrets.env.\n"
            "  B. Set google.drive_folder in connections.yaml to a SHARED DRIVE folder.\n"
            "  C. Create the Doc yourself, share it with the SA, then `gdoc link` + push.")
    elif "unauthorized_client" in low or "delegation" in low:
        print(f"\nImpersonation is configured but the Workspace hasn't authorised {email} for the\n"
              "drive/documents scopes. Add the SA client id under Admin → Domain-wide delegation,\n"
              "or unset GOOGLE_IMPERSONATE_SUBJECT to act as the service account directly.")
    elif str(status) in ("403", "404") and doc_id:
        print(f"Fix: open the Doc, Share it with  {email}  as Editor, then re-run.")
    sys.exit(1)


# ----------------------------------------------------------- feedback capture
def _comments_md(key, comments):
    L = [f"# Comments — {key} ({len(comments)})", ""]
    for c in comments:
        who = (c.get("author") or {}).get("displayName", "?")
        L.append(f"## comment {c.get('id')} · {who} · {c.get('createdTime','')} · "
                 f"resolved={c.get('resolved', False)}")
        q = (c.get("quotedFileContent") or {}).get("value", "")
        if q:
            L.append(f"> quoted: {q!r}")
        L.append((c.get("content") or "").strip())
        for r in c.get("replies", []):
            rwho = (r.get("author") or {}).get("displayName", "?")
            L.append(f"- reply ({rwho}): {(r.get('content') or '').strip()}")
        L.append("")
    return "\n".join(L)


def cmd_comments(argv):
    arg = _positional(argv, value_flags=("--out",))
    if not arg:
        sys.exit("comments needs <file>")
    local = _resolve_local(arg)
    data, conn_path, root = _repo()
    key = _rel(local, root)
    binding = _binding(data, key)
    if not binding or not binding.get("doc_id"):
        sys.exit(f"no Doc bound to {key}. `gdoc push`/`gdoc link` first.")
    doc_id = binding["doc_id"]
    drive = _drive(data)
    try:
        comments = _google.comments_list(drive, doc_id)
    except Exception as e:
        _die_google(e, doc_id)
    if "--json" in argv:
        text = json.dumps({"file": key, "doc_id": doc_id, "comments": comments},
                          indent=2, ensure_ascii=False)
    else:
        text = _comments_md(key, comments)
    out_path = _opt(argv, "--out")
    if out_path:
        p = _resolve_local(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"wrote {len(comments)} comment(s) -> {out_path}")
    else:
        print(text)


def cmd_diff(argv):
    arg = _positional(argv)
    if not arg:
        sys.exit("diff needs <file>")
    local = _resolve_local(arg)
    data, conn_path, root = _repo()
    key = _rel(local, root)
    binding = _binding(data, key)
    if not binding or not binding.get("doc_id"):
        sys.exit(f"no Doc bound to {key}.")
    drive = _drive(data)
    try:
        content = drive.files().export(fileId=binding["doc_id"], mimeType=MD_MIME).execute()
    except Exception as e:
        _die_google(e, binding["doc_id"])
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    base = local.read_text(encoding="utf-8") if local.exists() else ""
    import difflib
    diff = list(difflib.unified_diff(base.splitlines(), content.splitlines(),
                fromfile=f"a/{key} (local)", tofile=f"b/{key} (Doc)", lineterm=""))
    print("\n".join(diff) if diff else f"no direct edits: Doc matches {key}")


def cmd_resolve(argv):
    pos = [a for a in argv if not a.startswith("-")]
    if len(pos) < 2:
        sys.exit("resolve needs <file> <commentId>")
    local = _resolve_local(pos[0])
    note = _opt(argv, "--note", "Resolved via biz-connect.")
    data, conn_path, root = _repo()
    key = _rel(local, root)
    binding = _binding(data, key)
    if not binding or not binding.get("doc_id"):
        sys.exit(f"no Doc bound to {key}.")
    drive = _drive(data)
    try:
        _google.comment_resolve(drive, binding["doc_id"], pos[1], note)
    except Exception as e:
        _die_google(e, binding["doc_id"])
    print(f"resolved comment {pos[1]} on {key}")


VERBS = {
    "push": cmd_push, "pull": cmd_pull, "link": cmd_link, "unlink": cmd_unlink,
    "status": cmd_status, "list": cmd_list,
    "comments": cmd_comments, "diff": cmd_diff, "resolve": cmd_resolve,
}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown gdoc verb {verb!r}. One of: {', '.join(VERBS)}")
    fn(rest)
    return 0
