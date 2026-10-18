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

`push` replaces the Doc's whole body (a fresh Markdown import). For a Doc that people
edit, use the passage verbs instead: `insert`, `replace` and `delete` change one passage
through the Docs API and leave everything else (formatting, comments, other text) untouched.
With `--suggest` they write Google Docs suggestions for the Doc's editors to accept or reject.

Verbs
-----
  push  <file> [--title T] [--folder ID] [--version V] [--new] [--force]   create/update the Doc
                                                     (--new = a NEW Doc instance for a major build)
  pull  <file> [--force]                              overwrite local Markdown from the Doc
  insert  <file|doc> (--before|--after) <anchor> (--md M | --from F) [--suggest] [--dry-run]
                                                      add Markdown paragraphs next to the one
                                                      paragraph containing <anchor>, in place
  replace <file|doc> <find> <with> [--suggest] [--dry-run]
                                                      replace text that occurs exactly once, in place
  delete  <file|doc> <anchor> [--through <anchor2>] [--suggest] [--dry-run]
                                                      remove the one paragraph containing <anchor>
                                                      (--through: and everything up to the paragraph
                                                      containing <anchor2>, tables included)
                                                      (--suggest: write as suggestions to accept/reject)
  read    <file|doc> [--md] [--ids]                   print the Doc as text to quote from: exact
                                                      characters, # headings, * bullets, | rows |,
                                                      pending suggestions as [+ins+] / [-del-]
  accept|reject <file|doc> (--all | <id>...) [--dry-run]
                                                      accept or reject pending suggestions
  rows    <file|doc> --after <cell text> --row "A | B" [--row ...] [--suggest] [--dry-run]
                                                      add rows to a table, below the row with <cell text>
  link  <file> <doc-url-or-id>                        bind an existing Doc to a local file
  status [file]                                       show drift (local vs last sync, + Doc mod time)
  list                                                list all bindings in this repo
  unlink <file>                                       remove a binding (does not delete the Doc)
  comments <file> [--json] [--out P]                  dump the Doc's comments (feedback capture)
  diff <file>                                         unified diff: Doc body vs local (direct edits)
  resolve <file> <commentId> [--note T]               resolve a comment thread (close-out)
  docx <file> [--out P] [--save]                      Markdown -> .docx (Drive convert; if the target is
                                                      open in Word, refreshes the LIVE doc via COM)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .. import _google

DOC_MIME = "application/vnd.google-apps.document"
MD_MIME = "text/markdown"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
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


def _export_md(drive, doc_id) -> bytes:
    content = drive.files().export(fileId=doc_id, mimeType=MD_MIME).execute()
    return content.encode("utf-8") if isinstance(content, str) else content


def _baseline_sha(st):
    """Content hash of the Doc's export at the last sync, or None if unknown."""
    if st.get("synced_doc_sha256"):
        return st["synced_doc_sha256"]
    # state written before synced_doc_sha256 existed: a pulled file IS the Doc's export
    if st.get("last_pull_sha256") and st.get("last_pull_at", "") > st.get("last_push_at", ""):
        return st["last_pull_sha256"]
    return None


def _doc_changed(drive, doc_id, st, current=None):
    """True if the Doc was edited since the last sync. Drive's modifiedTime alone is not
    reliable for a Google Doc (it can move, even backwards, with no edit), so a differing
    time is confirmed against the content of the Doc's export."""
    synced = st.get("synced_modified")
    if not synced:
        return False
    current = current or _modified(drive, doc_id)
    if not current or current == synced:
        return False
    base = _baseline_sha(st)
    if not base:
        return True
    try:
        return _content_sha(_export_md(drive, doc_id)) != base
    except Exception:
        return True


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
    arg = _positional(argv, value_flags=("--title", "--folder", "--version",
                                         "--footnotes", "--footnote-kinds"))
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
                if _doc_changed(drive, doc_id, st, current):
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
            folder = _folder_id(folder_override) or _folder_id(config.scoped(data, "google.drive_folder"))
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
    synced_modified = _modified(drive, doc_id) or f.get("modifiedTime")
    try:
        doc_sha = _content_sha(_export_md(drive, doc_id))
    except Exception:
        doc_sha = None
    state.setdefault("docs", {})[key] = {
        **st, "doc_id": doc_id,
        "version": version or st.get("version"),
        "last_push_sha256": _content_sha(md),
        "last_push_at": _now(),
        "synced_modified": synced_modified,
        "synced_doc_sha256": doc_sha,
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

    # Re-apply anchored footnotes (a push REPLACES the Doc body, wiping any prior footnotes).
    # Idempotent: skips any ISS already footnoted. Keeps the open-issue annotations on every
    # new revision, each still deep-linked to its Notion register row.
    fnsrc = _opt(argv, "--footnotes")
    if fnsrc:
        try:
            pts = json.loads(_resolve_local(fnsrc).read_text(encoding="utf-8"))
            fk = _opt(argv, "--footnote-kinds")
            if fk:
                want = {k.strip().lower() for k in fk.split(",") if k.strip()}
                pts = [p for p in pts if str(p.get("kind", "")).lower() in want]
            print("  footnotes: " + _annotate_footnotes(data, doc_id, pts, _iss_url_map(data)))
        except Exception as e:
            st, detail = _google.http_error(e)
            print(f"  (footnotes not applied [{st}]: {detail[:80]})")


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
    # Read the time BEFORE exporting: an edit landing in between then shows up as a change.
    synced_modified = _modified(drive, doc_id)
    try:
        content = _export_md(drive, doc_id)
    except Exception as e:
        _die_google(e, doc_id)

    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(content)
    # The pulled content is the sync baseline: a later push is refused only if the Doc's
    # content has moved on from it, so pull, edit, push needs no --force.
    state.setdefault("docs", {})[key] = {
        **st, "doc_id": doc_id,
        "last_pull_sha256": _content_sha(content), "last_pull_at": _now(),
        "synced_modified": synced_modified,
        "synced_doc_sha256": _content_sha(content),
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
                if modified and _doc_changed(drive, doc_id, st, modified):
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


# --------------------------------------------------- annotate (open points -> Doc comments)
def _open_point_comment(p):
    """Render one harvested open point as a Doc comment. The body of the Doc stays clean —
    open points live in the register and surface HERE, as comments, not as inline prose. The
    trailing [harvest:<src_id>] tag makes re-annotation idempotent."""
    head = "%s · %s (%s)" % (p.get("iss") or "open point",
                             str(p.get("kind", "")).upper() or "NOTE", p.get("question", ""))
    lines = [head]
    if p.get("anchor"):
        lines.append('re: "%s"' % p["anchor"])
    if p.get("note"):
        lines.append(str(p["note"]))
    lines.append("— raised by the build · [harvest:%s]" % p.get("src_id", ""))
    return "\n".join(lines)


# ------------------------------------------- footnotes (open points -> anchored footnotes)
# Google's API cannot create text-ANCHORED comments on a Doc (the editor treats API-created
# comment anchors as unanchored). Footnotes are the supported inline-anchored mechanism: a
# superscript reference sits at the exact phrase, and the footnote text carries the open point
# with its ISS id hyperlinked to the matching Notion register row. Needs the Docs API enabled.
def _docs(data):
    return _google.build("docs", "v1", [_google.DOCS],
                         subject=_google.impersonation_subject(data))


def _norm_for_match(s):
    """Normalise smart punctuation 1:1 so anchor matching survives Markdown->Doc import
    (curly quotes, en/em dashes). Length-preserving, so a char-index map stays aligned."""
    return (s.replace("’", "'").replace("‘", "'")
             .replace("“", '"').replace("”", '"')
             .replace("—", "-").replace("–", "-").replace(" ", " "))


def _flatten_doc_body(doc):
    """(flat_text, index_map) over BODY paragraph text runs only — skips tables/headers so a
    footnote reference is never placed somewhere the Docs API forbids. index_map[i] is the
    document index of flat_text[i]."""
    flat, idxmap = [], []
    for el in doc.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []):
            tr = pe.get("textRun")
            start = pe.get("startIndex")
            if not tr or start is None:
                continue
            for i, ch in enumerate(tr.get("content", "")):
                flat.append(ch)
                idxmap.append(start + i)
    return "".join(flat), idxmap


def _anchor_end_index(flat_norm, idxmap, anchor):
    """Document index just AFTER the anchor phrase's first occurrence (where the footnote ref
    goes), or None if not found. Case-insensitive (a drafter's anchor may lowercase a word the
    prose capitalises at a sentence start); normalisation/lowercasing is length-preserving so
    the char-index map stays aligned."""
    a = _norm_for_match(anchor or "").strip()
    if not a:
        return None
    hay, needle = flat_norm.lower(), a.lower()
    pos = hay.find(needle)
    if pos >= 0:
        return idxmap[pos + len(a) - 1] + 1
    import re as _re                              # fallback: collapse internal whitespace
    a2 = _re.sub(r"\s+", " ", needle)
    hay2 = _re.sub(r"\s+", " ", hay)
    if a2 and a2 in hay2:
        first = needle.split()[0]
        p0 = hay.find(first)
        if p0 >= 0:
            return idxmap[min(p0 + len(a) - 1, len(idxmap) - 1)] + 1
    return None


def _iss_url_map(data):
    """ISS id -> Notion register page URL, so a footnote can deep-link to its issue row."""
    try:
        from . import register
        out = {}
        for pg in register._query(data):
            iss = register._row_get(pg, "ISS")
            if iss:
                out[iss] = pg.get("url", "")
        return out
    except Exception:
        return {}


def _annotate_footnotes(data, doc_id, points, iss_url):
    """Place a footnote at each open point's anchor phrase. Footnote text = '<ISS> (<KIND>):
    <note>', with <ISS> hyperlinked to its Notion row. Idempotent-ish: skips a point whose ISS
    already appears in an existing footnote."""
    docs = _docs(data)
    doc = docs.documents().get(documentId=doc_id).execute()
    # already-present ISS ids (avoid duplicating footnotes on re-run)
    present = set()
    for fid, seg in (doc.get("footnotes") or {}).items():
        txt = "".join(pe.get("textRun", {}).get("content", "")
                      for el in seg.get("content", []) for pe in el.get("paragraph", {}).get("elements", []))
        present |= set(re.findall(r"\bISS-\d+\b", txt))
    flat, idxmap = _flatten_doc_body(doc)
    flat_norm = _norm_for_match(flat)
    placed, missing, dup = [], [], 0
    for p in points:
        iss = p.get("iss") or ""
        if iss and iss in present:
            dup += 1
            continue
        end = _anchor_end_index(flat_norm, idxmap, p.get("anchor", ""))
        if end is None:
            missing.append(iss or p.get("src_id", "?"))
            continue
        placed.append((end, p))
    placed.sort(key=lambda x: x[0], reverse=True)   # high->low so inserts don't shift pending indices

    import time

    def _bu(requests, tries=5):
        """batchUpdate with backoff on the Docs write-per-minute quota (HTTP 429)."""
        for attempt in range(tries):
            try:
                return docs.documents().batchUpdate(
                    documentId=doc_id, body={"requests": requests}).execute()
            except Exception as e:
                st, _ = _google.http_error(e)
                if str(st) == "429" and attempt < tries - 1:
                    time.sleep(22)
                    continue
                raise

    done = failed = 0
    for end, p in placed:
        iss = p.get("iss") or ""
        kind = str(p.get("kind", "")).upper()
        prefix = ("%s (%s): " % (iss, kind)) if iss else ("(%s): " % kind)
        text = prefix + (p.get("note", "") or "")
        try:
            r = _bu([{"createFootnote": {"location": {"index": end}}}])
            fnid = r["replies"][0]["createFootnote"]["footnoteId"]
            reqs = [{"insertText": {"location": {"segmentId": fnid, "index": 1}, "text": text}}]
            url = iss_url.get(iss)
            if url and iss:
                reqs.append({"updateTextStyle": {
                    "range": {"segmentId": fnid, "startIndex": 1, "endIndex": 1 + len(iss)},
                    "textStyle": {"link": {"url": url}}, "fields": "link"}})
            _bu(reqs)
            done += 1
            time.sleep(0.7)                         # pace under the 60 writes/min quota
        except Exception as e:
            failed += 1
            st, detail = _google.http_error(e)
            print(f"  WARN footnote for {iss or p.get('src_id')} failed [{st}]: {detail[:100]}")
    msg = f"{done} footnote(s) placed"
    if dup:
        msg += f", {dup} already present"
    if missing:
        msg += f", {len(missing)} anchor(s) not found ({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''})"
    if failed:
        msg += f", {failed} failed"
    return msg


def cmd_annotate(argv):
    """Surface harvested open points (compose's <feedback>/harvest.json) on the bound Doc.
      --as comments  (default) one Drive comment per point (unanchored; quotes its text).
      --as footnotes a footnote at each point's anchor phrase, text = '<ISS> (<KIND>): <note>'
                     with the ISS hyperlinked to its Notion register row (needs the Docs API).
    Optional: --kinds decision,reconcile to annotate only the substantive items. Idempotent:
    comments skip by [harvest:…] tag; footnotes skip an ISS already present in a footnote."""
    arg = _positional(argv, value_flags=("--from", "--kinds", "--as"))
    src = _opt(argv, "--from")
    if not arg or not src:
        sys.exit("annotate needs <file> --from <harvest.json>")
    mode = (_opt(argv, "--as", "comments") or "comments").lower()  # comments | footnotes
    local = _resolve_local(arg)
    data, conn_path, root = _repo()
    key = _rel(local, root)
    binding = _binding(data, key)
    if not binding or not binding.get("doc_id"):
        sys.exit(f"no Doc bound to {key}. `gdoc push`/`gdoc link` first.")
    doc_id = binding["doc_id"]
    points = json.loads(_resolve_local(src).read_text(encoding="utf-8"))
    if not isinstance(points, list):
        sys.exit("harvest file must be a JSON list of open points")
    kinds = _opt(argv, "--kinds")        # e.g. "decision,reconcile" — annotate only the substantive items
    if kinds:
        want = {k.strip().lower() for k in kinds.split(",") if k.strip()}
        points = [p for p in points if str(p.get("kind", "")).lower() in want]
    if mode == "footnotes":
        try:
            print(f"annotated {key}: " + _annotate_footnotes(data, doc_id, points, _iss_url_map(data)))
        except Exception as e:
            _die_google(e, doc_id)
        return
    drive = _drive(data)
    posted = set()
    try:
        for c in _google.comments_list(drive, doc_id):
            m = re.search(r"\[harvest:([^\]]+)\]", c.get("content") or "")
            if m:
                posted.add(m.group(1))
    except Exception:
        pass
    created = skipped = failed = 0
    for p in points:
        sid = p.get("src_id", "")
        if sid and sid in posted:
            skipped += 1
            continue
        try:
            drive.comments().create(fileId=doc_id, fields="id",
                                    body={"content": _open_point_comment(p)}).execute()
            created += 1
        except Exception as e:
            failed += 1
            status, detail = _google.http_error(e)
            print(f"  WARN comment for {p.get('iss') or sid} failed [{status}]: {detail}")
    print(f"annotated {key}: {created} posted, {skipped} already present"
          + (f", {failed} failed" if failed else ""))


# ------------------------------------------- passage edits (insert / replace, in place)
# `push` re-imports the whole body, which resets the formatting of a Doc that people edit.
# These verbs change one passage through the Docs API and leave the rest alone. Each is ONE
# batchUpdate guarded by writeControl.requiredRevisionId: if anyone edits the Doc between our
# read and our write, Google rejects it and nothing is written. A new paragraph copies the
# paragraph and text style of the nearest existing paragraph of the same kind, so it matches
# the Doc's own look rather than the Markdown importer's defaults.
PARA_FIELDS = ("namedStyleType", "alignment", "lineSpacing", "direction", "spacingMode",
               "spaceAbove", "spaceBelow", "indentFirstLine", "indentStart", "indentEnd",
               "keepLinesTogether", "keepWithNext", "avoidWidowAndOrphan")
TEXT_FIELDS = ("bold", "italic", "underline", "strikethrough", "smallCaps", "backgroundColor",
               "foregroundColor", "fontSize", "weightedFontFamily", "baselineOffset", "link")
_BASE_TEXT = ("underline", "strikethrough", "smallCaps", "backgroundColor", "foregroundColor",
              "fontSize", "weightedFontFamily", "baselineOffset")
_DIMENSIONS = ("spaceAbove", "spaceBelow", "indentFirstLine", "indentStart", "indentEnd")
_MD_ESCAPABLE = set("\\`*_{}[]()#+-.!|$&<>~\"'")
_LINK = re.compile(r"\[((?:\\.|[^\]\\])+)\]\(([^)\s]+)\)")


def _u16(s):
    """Length in UTF-16 code units: the unit of every Docs API index."""
    return len(s.encode("utf-16-le")) // 2


def _md_inline(s):
    """Markdown inline text -> (plain text, spans), spans being (start, end, textStyle) in
    UTF-16 units. Handles **bold**, *italic*, [links](url) and backslash escapes; anything
    else stays literal."""
    out, spans = [], []
    pos = 0
    opened = {}                                    # "bold"/"italic" -> start offset
    i, n = 0, len(s)

    def toggle(name):
        if name in opened:
            a = opened.pop(name)
            if pos > a:
                spans.append((a, pos, {name: True}))
        else:
            opened[name] = pos

    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _MD_ESCAPABLE:
            out.append(s[i + 1]); pos += _u16(s[i + 1]); i += 2
            continue
        if s.startswith("**", i):
            toggle("bold"); i += 2
            continue
        if c == "*":
            toggle("italic"); i += 1
            continue
        if c == "[":
            m = _LINK.match(s, i)
            if m:
                text, sub = _md_inline(m.group(1))
                spans.extend((a + pos, b + pos, st) for a, b, st in sub)
                spans.append((pos, pos + _u16(text), {"link": {"url": m.group(2)}}))
                out.append(text); pos += _u16(text); i = m.end()
                continue
        out.append(c); pos += _u16(c); i += 1
    for name in list(opened):                      # unclosed: runs to the end
        toggle(name)
    return "".join(out), spans


def _md_blocks(md):
    """Markdown -> one block per paragraph: [{"kind": "heading"|"para"|"bullet", "level",
    "text", "spans"}]. A leading '> ' is dropped (Drive's export quotes indented lists).
    Refuses what it can't place faithfully: tables, numbered or nested lists, code, images."""
    blocks, cur = [], None

    def flush():
        nonlocal cur
        if cur:
            text, spans = _md_inline(" ".join(cur[2]))
            blocks.append({"kind": cur[0], "level": cur[1], "text": text, "spans": spans})
        cur = None

    for raw in md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        while line.lstrip().startswith(">"):
            line = line.lstrip()[1:]
        if not line.strip():
            flush()
            continue
        s = line.strip()
        indented = len(line) - len(line.lstrip()) >= 2
        bad = ("a table" if s.startswith("|") else "a code block" if s.startswith("```")
               else "an image" if s.startswith("![") else "a numbered list item" if re.match(r"\d+[.)]\s", s)
               else "a horizontal rule" if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", s)
               else "a nested list item" if indented and re.match(r"[*+-]\s", s) else None)
        if bad:
            sys.exit(f"insert can't place {bad} ({s[:40]!r}); headings, paragraphs and "
                     f"top-level bullets only")
        h = re.match(r"(#{1,6})\s+(.*?)\s*#*$", s)
        b = re.match(r"[*+-]\s+(.*)", s)
        if h:
            flush()
            cur = ("heading", len(h.group(1)), [h.group(2)])
            flush()
        elif b:
            flush()
            cur = ("bullet", 0, [b.group(1)])
        elif cur:                                  # continuation of a paragraph or bullet
            cur[2].append(s)
        else:
            cur = ("para", 0, [s])
    flush()
    return blocks


def _para_text(p, accepted=False):
    """A paragraph's text. accepted=True leaves out text a pending suggestion deletes."""
    return "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", [])
                   if not (accepted and e.get("textRun", {}).get("suggestedDeletionIds")))


def _tight(el):
    """True if a paragraph followed by `el` sits flush against it (a list or a table)."""
    return bool(el) and ("table" in el or bool(el.get("paragraph", {}).get("bullet")))


def _body_paras(tab):
    """Top-level body paragraphs, with what a new paragraph next to one needs to know."""
    els = [e for e in tab.get("body", {}).get("content", []) if "paragraph" in e or "table" in e]
    out = []
    for i, el in enumerate(els):
        if "paragraph" not in el:
            continue
        p = el["paragraph"]
        out.append({"start": el["startIndex"], "end": el["endIndex"], "text": _para_text(p),
                    "style": p.get("paragraphStyle", {}), "bullet": bool(p.get("bullet")),
                    "runs": [e["textRun"] for e in p.get("elements", []) if "textRun" in e],
                    "tight": _tight(els[i + 1] if i + 1 < len(els) else None)})
    return out


def _all_texts(tab, accepted=False):
    """Every paragraph's text in a tab: body (tables included), headers, footers, footnotes.
    accepted=True reads it as if pending suggestions were accepted (suggested deletions out)."""
    out = []

    def walk(content):
        for el in content or []:
            if "paragraph" in el:
                out.append(_para_text(el["paragraph"], accepted))
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content"))
            elif "tableOfContents" in el:
                walk(el["tableOfContents"].get("content"))
    walk(tab.get("body", {}).get("content"))
    for group in ("headers", "footers", "footnotes"):
        for seg in (tab.get(group) or {}).values():
            walk(seg.get("content"))
    return out


def _match_key(s):
    return re.sub(r"\s+", " ", _norm_for_match(s)).strip().lower()


def _find_anchor(paras, anchor):
    a = _match_key(anchor or "")
    if not a:
        sys.exit("the anchor text is empty")
    hits = [p for p in paras if a in _match_key(p["text"])]
    if len(hits) != 1:
        found = "; ".join(repr(p["text"].strip()[:60]) for p in hits[:5])
        sys.exit(f"anchor {anchor!r} is in {len(hits)} paragraphs; it must pick out exactly one"
                 + (f": {found}" if hits else " (table cells are not searched)"))
    return hits[0]


def _kind(block):
    if block["kind"] == "heading":
        return (f"HEADING_{block['level']}", False)
    return ("NORMAL_TEXT", block["kind"] == "bullet")


def _template(paras, kind, tight, near):
    """The existing paragraph a new one should look like: same named style and bullet-ness,
    preferring the same spacing context (flush before a list or table, or not), nearest first."""
    same = [p for p in paras
            if (p["style"].get("namedStyleType", "NORMAL_TEXT"), p["bullet"]) == kind]
    pool = [p for p in same if p["tight"] == tight] or same
    return min(pool, key=lambda p: abs(p["start"] - near)) if pool else None


def _para_style(tpl, named):
    st = {}
    for f in PARA_FIELDS:
        v = (tpl["style"] if tpl else {}).get(f)
        if v is None:
            continue
        if f in _DIMENSIONS and isinstance(v, dict):    # an explicit zero comes back bare
            v = {"magnitude": v.get("magnitude", 0), "unit": v.get("unit", "PT")}
        st[f] = v
    st["namedStyleType"] = named
    return st


def _base_text(tpl):
    """The text style a new paragraph starts from: its template's plain (non-bold) run,
    without the emphasis and links that the Markdown sets explicitly. A linked run is never
    copied: its blue underline would make plain text look like a broken link."""
    runs = [r for r in (tpl["runs"] if tpl else []) if not r.get("textStyle", {}).get("link")]
    if not runs:
        return {}
    run = next((r for r in runs if not r.get("textStyle", {}).get("bold")), runs[0])
    ts = run.get("textStyle", {})
    return {k: ts[k] for k in _BASE_TEXT if k in ts}


def _plan_insert(tab, tab_id, blocks, anchor, after):
    """(requests, anchor paragraph) for one batchUpdate that inserts `blocks` as new
    paragraphs before or after the one paragraph containing `anchor`."""
    paras = _body_paras(tab)
    p = _find_anchor(paras, anchor)
    existing = {_match_key(q["text"]) for q in paras}
    dup = [b["text"] for b in blocks if _match_key(b["text"]) in existing]
    if dup:
        sys.exit(f"already in the Doc: {dup[0][:60]!r}. Nothing was written (inserted before?).")

    def where(d):
        return {**d, "tabId": tab_id} if tab_id else d

    texts = [b["text"] for b in blocks]
    if after:                      # split the anchor at its end: new paragraphs follow it
        at, first, payload = p["end"] - 1, p["end"], "".join("\n" + t for t in texts)
        last_tight = p["tight"]
    else:                          # new paragraphs go in front of the anchor
        at, first, payload = p["start"], p["start"], "".join(t + "\n" for t in texts)
        last_tight = p["bullet"]
    starts, s = [], first
    for t in texts:
        starts.append(s)
        s += _u16(t) + 1

    def span(k, j=None):           # paragraph k, or k..j inclusive, newline included
        j = k if j is None else j
        return where({"startIndex": starts[k], "endIndex": starts[j] + _u16(texts[j]) + 1})

    reqs = [{"insertText": {"location": where({"index": at}), "text": payload}}]
    # New paragraphs inherit the anchor's bullet; add or remove bullets where they differ.
    k = 0
    while k < len(blocks):
        want = blocks[k]["kind"] == "bullet"
        j = k
        while j + 1 < len(blocks) and (blocks[j + 1]["kind"] == "bullet") == want:
            j += 1
        if want != p["bullet"]:
            reqs.append({"createParagraphBullets": {"range": span(k, j),
                                                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}}
                        if want else {"deleteParagraphBullets": {"range": span(k, j)}})
        k = j + 1
    for k, b in enumerate(blocks):
        kind = _kind(b)
        tight = (blocks[k + 1]["kind"] == "bullet") if k + 1 < len(blocks) else last_tight
        tpl = _template(paras, kind, tight, at)
        reqs.append({"updateParagraphStyle": {"range": span(k),
                                              "paragraphStyle": _para_style(tpl, kind[0]),
                                              "fields": ",".join(PARA_FIELDS)}})
        reqs.append({"updateTextStyle": {"range": span(k), "textStyle": _base_text(tpl),
                                         "fields": ",".join(TEXT_FIELDS)}})
        for a, z, style in b["spans"]:
            reqs.append({"updateTextStyle": {
                "range": where({"startIndex": starts[k] + a, "endIndex": starts[k] + z}),
                "textStyle": style, "fields": ",".join(style)}})
    return reqs, p


def _edit_target(arg):
    """(connections data or None, doc id, label) for a passage edit: a Doc URL or id, or a
    local file bound to a Doc in connections.yaml."""
    if re.search(r"docs\.google\.com|drive\.google\.com", arg) or (
            re.fullmatch(r"[A-Za-z0-9_-]{25,}", arg) and not _resolve_local(arg).exists()):
        return None, _doc_id_from(arg), arg
    data, conn_path, root = _repo()
    key = _rel(_resolve_local(arg), root)
    b = _binding(data, key)
    if not b or not b.get("doc_id"):
        sys.exit(f"no Doc bound to {key}. Pass the Doc's URL instead, or `gdoc link` it first.")
    return data, b["doc_id"], key


def _fetch_doc(docs, doc_id):
    """(document, first tab's content, tab id). Passage edits work on the first tab."""
    try:
        d = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    except Exception as e:
        _die_google(e, doc_id)
    tabs = d.get("tabs") or []
    if tabs:
        return d, tabs[0].get("documentTab", {}), tabs[0].get("tabProperties", {}).get("tabId")
    return d, d, None


def _send_edit(docs, doc_id, rev, requests, dry, done, suggest=False):
    """One guarded batchUpdate. suggest=True writes every request as a suggestion (Docs API
    writeMode SUGGEST, Sep 2026) for the Doc's editors to accept or reject."""
    control = {"requiredRevisionId": rev}
    if suggest:
        control["writeMode"] = "SUGGEST"
    body = {"requests": requests, "writeControl": control}
    if dry:
        print(json.dumps(body, indent=1, ensure_ascii=False))
        print(f"-- dry run: {len(requests)} request(s), nothing sent", file=sys.stderr)
        return None
    try:
        # no retry: a write whose outcome is unknown is reported, not repeated
        resp = docs.documents().batchUpdate(documentId=doc_id, body=body).execute()
    except Exception as e:
        status, detail = _google.http_error(e)
        if str(status) == "400" and "revision" in (detail or "").lower():
            sys.exit(f"REVISION MISMATCH [{status}]: the Doc changed after it was read (someone "
                     f"is editing it). Nothing was written. Re-run to read it afresh.")
        if str(status) not in ("400", "401", "403", "404"):
            print("This write may or may not have been applied. Look at the Doc before re-running "
                  "(insert and replace refuse to apply twice).")
        _die_google(e, doc_id)
    print(done(resp) if callable(done) else done)
    return resp


def cmd_insert(argv):
    arg = _positional(argv, value_flags=("--before", "--after", "--md", "--from"))
    before, after = _opt(argv, "--before"), _opt(argv, "--after")
    if not arg or bool(before) == bool(after):
        sys.exit("insert needs <file|doc-url> and one of --before/--after <anchor text>")
    md, src = _opt(argv, "--md"), _opt(argv, "--from")
    if (md is None) == (src is None):
        sys.exit("insert needs --md <markdown> or --from <file.md>")
    if src:
        md = _resolve_local(src).read_text(encoding="utf-8")
    blocks = _md_blocks(md)
    if not blocks:
        sys.exit("nothing to insert")
    data, doc_id, label = _edit_target(arg)
    docs = _docs(data)
    d, tab, tab_id = _fetch_doc(docs, doc_id)
    reqs, p = _plan_insert(tab, tab_id, blocks, before or after, after=bool(after))
    suggest = "--suggest" in argv
    _send_edit(docs, doc_id, d["revisionId"], reqs, "--dry-run" in argv,
               f"{'suggested' if suggest else 'inserted'} {len(blocks)} paragraph(s) "
               f"{'after' if after else 'before'} {p['text'].strip()[:50]!r} in {label}",
               suggest=suggest)


def cmd_replace(argv):
    pos = [a for a in argv if a not in ("--dry-run", "--suggest")]
    if len(pos) != 3 or not pos[1]:
        sys.exit("replace needs <file|doc-url> <find> <with>")
    arg, find, new = pos
    data, doc_id, label = _edit_target(arg)
    docs = _docs(data)
    d, tab, tab_id = _fetch_doc(docs, doc_id)
    # Count against the Doc as it would read with pending suggestions accepted, so a replace
    # already suggested (its old text marked for deletion) counts as done, not as still to do.
    texts = _all_texts(tab, accepted=True)
    n = sum(t.count(find) for t in texts)
    if n == 0 and new and any(new in t for t in texts):
        sys.exit(f"{new!r} is already in the Doc and {find!r} is not. Nothing was written "
                 f"(replaced before?).")
    if n != 1:
        near = [t for t in texts if _match_key(find) in _match_key(t)] if n == 0 else []
        sys.exit(f"{find!r} occurs {n} times in the Doc; replace needs exactly one. Nothing was "
                 f"written." + (f"\nThe Doc has it with different characters (quotes, dashes or "
                                f"spaces): {near[0].strip()[:120]!r}" if near else
                                "" if n == 0 else " Lengthen it until it is unique."))
    if find in new and any(new in t for t in texts):
        sys.exit(f"{new!r} is already in the Doc. Nothing was written (replaced before?).")
    req = {"containsText": {"text": find, "matchCase": True}, "replaceText": new}
    if tab_id:
        req["tabsCriteria"] = {"tabIds": [tab_id]}
    suggest = "--suggest" in argv

    def done(resp):
        got = ((resp.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged")
        return (f"{'suggested a replacement of' if suggest else 'replaced'} "
                f"{1 if got is None else got} occurrence(s) in {label}")
    _send_edit(docs, doc_id, d["revisionId"], [{"replaceAllText": req}], "--dry-run" in argv,
               done, suggest=suggest)


def _plan_delete(tab, tab_id, anchor, through=None):
    """(requests, first paragraph, element count) to remove the one body paragraph containing
    `anchor`, or with `through` everything from it to the paragraph containing `through`
    (tables between included)."""
    content = tab.get("body", {}).get("content", [])
    paras = _body_paras(tab)
    p = _find_anchor(paras, anchor)
    q = _find_anchor(paras, through) if through else p
    if q["start"] < p["start"]:
        sys.exit(f"--through {through!r} comes before {anchor!r}; name the passage's last paragraph.")
    if q is p and all(r.get("suggestedDeletionIds") for r in p["runs"] if r.get("content", "").strip()):
        sys.exit(f"{p['text'].strip()[:60]!r} is already marked for deletion. Nothing was written.")
    i = next(k for k, el in enumerate(content)
             if "paragraph" in el and el.get("startIndex") == q["start"])
    nxt = content[i + 1] if i + 1 < len(content) else None
    if nxt is None:
        sys.exit("can't remove the Doc's last paragraph; replace its text instead.")
    if "paragraph" not in nxt:
        sys.exit("can't remove the paragraph just before a table (Google keeps that line); "
                 "replace its text instead.")
    rng = {"startIndex": p["start"], "endIndex": q["end"]}
    if tab_id:
        rng["tabId"] = tab_id
    n = sum(1 for el in content if p["start"] <= el.get("startIndex", -1) < q["end"])
    return [{"deleteContentRange": {"range": rng}}], p, n


def _marked_text(p, ids=False):
    """A paragraph's text with pending suggestions marked [+inserted+] and [-deleted-]
    (ids=True appends each one's suggestion id, for `accept` / `reject`)."""
    out = []
    for e in p.get("elements", []):
        tr = e.get("textRun") or {}
        c = tr.get("content", "").replace("\n", "")
        if not c:
            continue
        sid = tr.get("suggestedDeletionIds") or tr.get("suggestedInsertionIds")
        tag = "{" + ",".join(sid) + "}" if ids and sid else ""
        out.append(f"[-{c}-]{tag}" if tr.get("suggestedDeletionIds") else
                   f"[+{c}+]{tag}" if tr.get("suggestedInsertionIds") else c)
    return "".join(out)


def _suggestion_ids(p):
    ids = set(p.get("suggestedParagraphStyleChanges") or {}) | set(p.get("suggestedBulletChanges") or {})
    for e in p.get("elements", []):
        tr = e.get("textRun") or {}
        ids |= set(tr.get("suggestedInsertionIds") or []) | set(tr.get("suggestedDeletionIds") or [])
        ids |= set(tr.get("suggestedTextStyleChanges") or {})
    return ids


def _read_lines(tab, show_ids=False):
    """(lines, pending suggestion ids): the Doc's body as text to quote from, one line per
    paragraph: '#' headings, '* ' bullets, '| a | b |' table rows."""
    lines, ids = [], set()
    for el in tab.get("body", {}).get("content", []):
        if "paragraph" in el:
            p = el["paragraph"]
            ids |= _suggestion_ids(p)
            t = _marked_text(p, show_ids)
            if not t.strip():
                continue
            named = p.get("paragraphStyle", {}).get("namedStyleType", "")
            if named.startswith("HEADING_"):
                t = "#" * int(named[-1]) + " " + t
            elif named in ("TITLE", "SUBTITLE"):
                t = f"({named.lower()}) {t}"
            elif p.get("bullet"):
                t = "  " * p["bullet"].get("nestingLevel", 0) + "* " + t
            lines.append(t)
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    paras = [c["paragraph"] for c in cell.get("content", []) if "paragraph" in c]
                    for q in paras:
                        ids |= _suggestion_ids(q)
                    cells.append(" / ".join(t for t in (_marked_text(q, show_ids) for q in paras)
                                            if t.strip()))
                lines.append("| " + " | ".join(cells) + " |")
    return lines, ids


def cmd_read(argv):
    arg = _positional(argv)
    if not arg:
        sys.exit("read needs <file|doc-url>")
    data, doc_id, label = _edit_target(arg)
    if "--md" in argv:
        try:
            print(_export_md(_drive(data), doc_id).decode("utf-8"))
        except Exception as e:
            _die_google(e, doc_id)
        return
    d, tab, _tab_id = _fetch_doc(_docs(data), doc_id)
    lines, _ids = _read_lines(tab, show_ids="--ids" in argv)
    pending = _all_suggestion_ids(d)
    print(f"{d.get('title', '')}  ({label})")
    if pending:
        print(f"{len(pending)} pending suggestion(s): text marked [+inserted+] or [-deleted-]"
              + (" {suggestion id}" if "--ids" in argv else "; --ids shows their ids"))
    print("\n" + "\n".join(lines))


def _all_suggestion_ids(node):
    """Every pending suggestion id anywhere in a documents.get response: text, paragraph and
    text styles, bullets, tables, lists, objects and document styles."""
    ids = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("suggested"):
                if isinstance(v, list) and k in ("suggestedInsertionIds", "suggestedDeletionIds"):
                    ids.update(v)
                elif isinstance(v, str) and k == "suggestedInsertionId":
                    ids.add(v)
                elif isinstance(v, dict):                   # suggested*Changes: keyed by id
                    ids.update(v)
            ids |= _all_suggestion_ids(v)
    elif isinstance(node, list):
        for x in node:
            ids |= _all_suggestion_ids(x)
    return ids


def _resolve_suggestions(argv, verb):
    pos = [a for a in argv if not a.startswith("--")]
    if not pos or (("--all" in argv) == (len(pos) > 1)):
        sys.exit(f"{verb} needs <file|doc-url> and either --all or suggestion id(s) "
                 f"(`gdoc read <doc> --ids` shows them)")
    data, doc_id, label = _edit_target(pos[0])
    docs = _docs(data)
    d, _tab, _tab_id = _fetch_doc(docs, doc_id)
    pending = _all_suggestion_ids(d)
    ids = sorted(pending) if "--all" in argv else pos[1:]
    unknown = [i for i in ids if i not in pending]
    if unknown:
        sys.exit(f"not pending in the Doc: {', '.join(unknown)}. Nothing was written.")
    if not ids:
        print(f"no pending suggestions in {label}")
        return
    kind = "acceptSuggestion" if verb == "accept" else "rejectSuggestion"
    _send_edit(docs, doc_id, d["revisionId"], [{kind: {"suggestionId": i}} for i in ids],
               "--dry-run" in argv, f"{verb}ed {len(ids)} suggestion(s) in {label}")


def cmd_accept(argv):
    _resolve_suggestions(argv, "accept")


def cmd_reject(argv):
    _resolve_suggestions(argv, "reject")


def _cell_text(cell):
    return " ".join(_para_text(c["paragraph"]).strip() for c in cell.get("content", []) if "paragraph" in c)


def _plan_rows(tab, tab_id, anchor, rows):
    """(requests, anchor row's cell texts) for one batchUpdate that adds `rows` (lists of
    Markdown cell texts) to a body table, directly below the one row with a cell containing
    `anchor`. New rows are inserted empty, then filled from the highest index down, so no
    insertion shifts a cell still to be filled. Row layout: row start R, cell k's paragraph
    at R + 2 + 2k, row width 1 + 2 * columns."""
    a = _match_key(anchor or "")
    if not a:
        sys.exit("the anchor text is empty")
    hits = []
    for el in tab.get("body", {}).get("content", []):
        for r, row in enumerate((el.get("table") or {}).get("tableRows", [])):
            if any(a in _match_key(_cell_text(c)) for c in row.get("tableCells", [])):
                hits.append((el, r, row))
    if len(hits) != 1:
        sys.exit(f"anchor {anchor!r} is in {len(hits)} table rows; it must pick out exactly one")
    el, r, row = hits[0]
    ncols = el["table"]["columns"]
    for cells in rows:
        if len(cells) != ncols:
            sys.exit(f"each --row needs {ncols} cells separated by ' | '; got {len(cells)}: {cells!r}")
    existing = {_match_key(_cell_text(rw["tableCells"][0])) for rw in el["table"]["tableRows"]}
    dup = [c[0] for c in rows if _match_key(_md_inline(c[0])[0]) in existing]
    if dup:
        sys.exit(f"already in the table: {dup[0]!r}. Nothing was written (added before?).")

    def where(d):
        return {**d, "tabId": tab_id} if tab_id else d

    start = where({"index": el["startIndex"]})
    reqs = [{"insertTableRow": {"tableCellLocation": {"tableStartLocation": start, "rowIndex": r,
                                                      "columnIndex": 0}, "insertBelow": True}}
            for _ in rows]
    # each column's text style, from the anchor row (bold only where that whole cell is bold)
    base = []
    for cell in row["tableCells"]:
        runs = [e["textRun"] for c in cell.get("content", []) if "paragraph" in c
                for e in c["paragraph"].get("elements", []) if e.get("textRun", {}).get("content", "").strip()]
        st = {k: v for k, v in (runs[0].get("textStyle", {}) if runs else {}).items() if k in _BASE_TEXT}
        if runs and all(x.get("textStyle", {}).get("bold") for x in runs):
            st["bold"] = True
        base.append(st)
    width, first = 1 + 2 * ncols, row["endIndex"]
    for j in reversed(range(len(rows))):
        for k in reversed(range(ncols)):
            text, spans = _md_inline(rows[j][k].strip())
            if not text:
                continue
            at = first + j * width + 2 + 2 * k
            reqs.append({"insertText": {"location": where({"index": at}), "text": text}})
            reqs.append({"updateTextStyle": {"range": where({"startIndex": at, "endIndex": at + _u16(text)}),
                                             "textStyle": base[k], "fields": ",".join(TEXT_FIELDS)}})
            for s, z, style in spans:
                reqs.append({"updateTextStyle": {"range": where({"startIndex": at + s, "endIndex": at + z}),
                                                 "textStyle": style, "fields": ",".join(style)}})
    return reqs, [_cell_text(c) for c in row["tableCells"]]


def cmd_rows(argv):
    values = {i + 1 for i, a in enumerate(argv) if a in ("--after", "--row")}
    pos = next((a for i, a in enumerate(argv) if i not in values and not a.startswith("--")), None)
    anchor = _opt(argv, "--after")
    rows = [argv[i + 1].split(" | ") for i, a in enumerate(argv[:-1]) if a == "--row"]
    if not pos or not anchor or not rows:
        sys.exit('rows needs <file|doc-url> --after "<text in a table row>" --row "A | B" [--row ...]')
    data, doc_id, label = _edit_target(pos)
    docs = _docs(data)
    d, tab, tab_id = _fetch_doc(docs, doc_id)
    reqs, anchor_cells = _plan_rows(tab, tab_id, anchor, rows)
    suggest = "--suggest" in argv
    _send_edit(docs, doc_id, d["revisionId"], reqs, "--dry-run" in argv,
               f"{'suggested' if suggest else 'added'} {len(rows)} table row(s) below "
               f"{anchor_cells[0][:50]!r} in {label}", suggest=suggest)


def _plan_unlink_look(tab, tab_id, anchor, through=None):
    """(requests, run count) clearing the blue underline from text that carries no link, in
    the paragraphs from `anchor` to `through`. Only underline and colour change; text, bold and
    real links are left alone."""
    paras = _body_paras(tab)
    p = _find_anchor(paras, anchor)
    q = _find_anchor(paras, through) if through else p
    if q["start"] < p["start"]:
        sys.exit(f"--through {through!r} comes before {anchor!r}; name the passage's last paragraph.")
    reqs = []
    for el in tab.get("body", {}).get("content", []):
        if "paragraph" not in el or not p["start"] <= el.get("startIndex", -1) <= q["start"]:
            continue
        for e in el["paragraph"].get("elements", []):
            ts = (e.get("textRun") or {}).get("textStyle", {})
            if "textRun" not in e or ts.get("link") or not (ts.get("underline") or ts.get("foregroundColor")):
                continue
            rng = {"startIndex": e["startIndex"], "endIndex": e["endIndex"]}
            if tab_id:
                rng["tabId"] = tab_id
            reqs.append({"updateTextStyle": {"range": rng, "textStyle": {},
                                             "fields": "underline,foregroundColor"}})
    return reqs, len(reqs)


def cmd_plain(argv):
    through = _opt(argv, "--through")
    pos = [a for a in argv if a not in ("--dry-run", "--through", through)]
    if len(pos) != 2 or not pos[1]:
        sys.exit("plain needs <file|doc-url> <anchor text> [--through <last anchor>]")
    arg, anchor = pos
    data, doc_id, label = _edit_target(arg)
    docs = _docs(data)
    d, tab, tab_id = _fetch_doc(docs, doc_id)
    reqs, n = _plan_unlink_look(tab, tab_id, anchor, through)
    if not reqs:
        print(f"nothing to clear: no unlinked blue or underlined text there in {label}")
        return
    _send_edit(docs, doc_id, d["revisionId"], reqs, "--dry-run" in argv,
               f"cleared link styling from {n} unlinked run(s) in {label}")


def cmd_delete(argv):
    through = _opt(argv, "--through")
    pos = [a for a in argv if a not in ("--dry-run", "--suggest", "--through", through)]
    if len(pos) != 2 or not pos[1]:
        sys.exit("delete needs <file|doc-url> <anchor text> [--through <last anchor>]")
    arg, anchor = pos
    data, doc_id, label = _edit_target(arg)
    docs = _docs(data)
    d, tab, tab_id = _fetch_doc(docs, doc_id)
    reqs, p, n = _plan_delete(tab, tab_id, anchor, through)
    suggest = "--suggest" in argv
    what = (f"{n} paragraph(s)/table(s) from {p['text'].strip()[:50]!r}" if through
            else f"paragraph {p['text'].strip()[:50]!r}")
    _send_edit(docs, doc_id, d["revisionId"], reqs, "--dry-run" in argv,
               f"{'suggested deleting' if suggest else 'deleted'} {what} in {label}", suggest=suggest)


def _refresh_live_word_doc(target: Path, content: bytes, save: bool) -> bool:
    """The target .docx is write-locked because it is OPEN in Word. Instead of
    failing, refresh the OPEN document's content in place via COM (Windows only):
    attach to the user's already-running Word, find the document, replace its body
    with the freshly exported content. Never opens a new Word, never closes or
    saves the user's document unless save=True — the user keeps Ctrl+Z / Ctrl+S.
    Returns True if the live document was refreshed."""
    if os.name != "nt":
        return False
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False
    import tempfile
    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.GetActiveObject("Word.Application")
        except Exception:
            return False                      # no running Word — a different lock
        tgt = str(target.resolve()).lower()
        doc = None
        for d in word.Documents:
            try:
                if str(d.FullName).lower() == tgt:
                    doc = d
                    break
            except Exception:
                continue
        if doc is None:
            return False
        tmp = Path(tempfile.gettempdir()) / f"bizconnect_docx_{os.getpid()}.docx"
        tmp.write_bytes(content)
        try:
            rng = doc.Content
            rng.Delete()
            rng.InsertFile(str(tmp))
            if save:
                doc.Save()
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return True
    finally:
        pythoncom.CoUninitialize()


def cmd_docx(argv):
    """Markdown -> .docx, using Drive as the converter (import md -> Doc, export
    Doc -> Word). If the file's bound Doc already holds exactly this content, export
    that; otherwise convert via a throwaway Doc that is deleted afterwards — no
    binding is written and nothing lingers in Drive. If the target .docx is open in
    Word, the OPEN document is refreshed in place via COM (--save to also save it)."""
    arg = _positional(argv, value_flags=("--out",))
    if not arg:
        sys.exit("docx needs <file.md>")
    local = _resolve_local(arg)
    if not local.exists():
        sys.exit(f"local file not found: {local}")
    out = _opt(argv, "--out")
    out_path = _resolve_local(out) if out else local.with_suffix(".docx")

    data, conn_path, root = _repo()
    key = _rel(local, root)
    md = local.read_bytes()
    drive = _drive(data)

    binding = _binding(data, key)
    doc_id = binding.get("doc_id") if binding else None
    st = _load_state(root).get("docs", {}).get(key, {})
    reuse = bool(
        doc_id and st.get("last_push_sha256") == _content_sha(md)
        and st.get("synced_modified") and _modified(drive, doc_id) == st["synced_modified"])

    temp_id = None
    try:
        if reuse:
            export_id = doc_id
        else:
            from googleapiclient.http import MediaInMemoryUpload
            media = MediaInMemoryUpload(md, mimetype=MD_MIME, resumable=False)
            f = drive.files().create(
                body={"name": f"__bizconnect_docx_tmp__ {local.stem}", "mimeType": DOC_MIME},
                media_body=media, fields="id", supportsAllDrives=True).execute()
            temp_id = export_id = f["id"]
        content = drive.files().export(fileId=export_id, mimeType=DOCX_MIME).execute()
    except SystemExit:
        raise
    except Exception as e:
        _die_google(e, doc_id)
    finally:
        if temp_id:
            try:
                drive.files().delete(fileId=temp_id, supportsAllDrives=True).execute()
            except Exception:
                pass

    if isinstance(content, str):
        content = content.encode("utf-8")
    src = "from the bound Doc (in sync)" if reuse else "via a temporary Doc (deleted)"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_bytes(content)
    except PermissionError:
        # The .docx is open in Word. Refresh the live document instead of failing.
        if _refresh_live_word_doc(out_path, content, save="--save" in argv):
            saved = "and saved" if "--save" in argv else                 "left UNSAVED — Ctrl+S in Word to persist, Ctrl+Z to revert"
            print(f"{_rel(out_path, root)} is open in Word — refreshed the LIVE document "
                  f"in place ({len(content)} bytes {src}), {saved}.")
            return
        sys.exit(f"{_rel(out_path, root)} is locked (open in another app?) and no open "
                 f"Word document matches it — close the file and re-run.")
    print(f"wrote {_rel(out_path, root)} ({len(content)} bytes) {src}")


VERBS = {
    "push": cmd_push, "pull": cmd_pull, "link": cmd_link, "unlink": cmd_unlink,
    "status": cmd_status, "list": cmd_list,
    "comments": cmd_comments, "annotate": cmd_annotate, "diff": cmd_diff, "resolve": cmd_resolve,
    "docx": cmd_docx, "read": cmd_read, "insert": cmd_insert, "replace": cmd_replace,
    "delete": cmd_delete, "plain": cmd_plain, "accept": cmd_accept, "reject": cmd_reject, "rows": cmd_rows,
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
