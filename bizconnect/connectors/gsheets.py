"""gsheets — read/write Google Sheets via the central service account.

The read-only Drive connector can't write; this can. Share a sheet with the
service-account email (Editor to write, Viewer to read), then:

  whoami                                          service-account email + project
  check  <sheet|url>                              access pre-flight; lists tabs
  read   <sheet|url> [--tab N|--gid N] [--range A1:Z] [--format csv|tsv|json]
  write  <sheet|url> --range A1 [--tab N] [--input raw|user] (--csv F|--json F|--stdin)
  append <sheet|url> [--tab N] [--input raw|user] (--csv F|--stdin)
  clear  <sheet|url> --range A1:Z [--tab N]
  create --title NAME [--tabs A,B,C] [--share EMAIL[:role]]
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

from .. import _google


def _services():
    creds_scopes = [_google.SHEETS, _google.DRIVE]
    sheets = _google.build("sheets", "v4", creds_scopes)
    drive = _google.build("drive", "v3", creds_scopes)
    return sheets, drive


def sheet_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    if m:
        return m.group(1)
    if "/" in s or " " in s:
        sys.exit(f"could not find a spreadsheet id in: {s!r}")
    return s


def url_gid(s):
    m = re.search(r"[?&#]gid=(\d+)", s)
    return int(m.group(1)) if m else None


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def flag(args, name):
    return name in args


def _meta(sheets, sid):
    return sheets.spreadsheets().get(spreadsheetId=sid,
                                     fields="properties.title,sheets.properties").execute()


def _first_tab(meta):
    return meta["sheets"][0]["properties"]["title"]


def _tab_for_gid(meta, gid):
    for sh in meta.get("sheets", []):
        if sh["properties"].get("sheetId") == gid:
            return sh["properties"]["title"]
    sys.exit(f"no tab with gid={gid}")


def _range(tab, rng):
    if tab:
        q = "'" + tab.replace("'", "''") + "'"     # escape apostrophes per A1 notation
        return f"{q}!{rng}" if rng else q
    return rng


def _die(e, sid):
    status, detail = _google.http_error(e)
    if str(status) in ("403", "404"):
        email = _google.client_email() or "<service-account email>"
        print(f"NO ACCESS [{status}]: {detail}")
        print(f"Fix: open the sheet -> Share -> add  {email}  as Editor (or Viewer), then re-run.")
    else:
        # not an access problem (bad range, rate limit, network, ...) — don't mislead
        print(f"sheets API error [{status}]: {detail}")
    sys.exit(1)


def _rows_from_input(args):
    if flag(args, "--stdin"):
        return list(csv.reader(io.StringIO(sys.stdin.read())))
    cf = opt(args, "--csv")
    if cf:
        with open(Path(cf), newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))
    jf = opt(args, "--json")
    if jf:
        data = json.loads(Path(jf).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            sys.exit("--json must be a list of rows")
        return [r if isinstance(r, list) else [r] for r in data]
    sys.exit("need input: --csv FILE, --json FILE, or --stdin")


def _input_option(args):
    v = (opt(args, "--input", "raw") or "raw").lower()
    if v in ("user", "user_entered"):
        return "USER_ENTERED"
    if v == "raw":
        return "RAW"
    sys.exit("--input must be 'raw' or 'user'")


def cmd_whoami(_a):
    print(f"service account: {_google.client_email()}")
    print("Share any target sheet with the email above (Editor to write, Viewer to read).")


def cmd_check(args):
    if not args:
        sys.exit("check needs <sheet|url>")
    sid = sheet_id(args[0])
    sheets, _ = _services()
    try:
        meta = _meta(sheets, sid)
    except Exception as e:
        _die(e, sid)
    print(f"OK — can access {sid}")
    print(f"title: {meta.get('properties', {}).get('title','')!r}  tabs: {len(meta.get('sheets', []))}")
    for sh in meta.get("sheets", []):
        p = sh["properties"]; gp = p.get("gridProperties", {})
        print(f"  - {p['title']!r}  gid={p.get('sheetId')}  {gp.get('rowCount','?')}x{gp.get('columnCount','?')}")


def cmd_read(args):
    if not args:
        sys.exit("read needs <sheet|url>")
    sid = sheet_id(args[0])
    if "--gid" not in args and "--tab" not in args:
        g = url_gid(args[0])
        if g is not None:
            args = args + ["--gid", str(g)]
    sheets, _ = _services()
    tab, gid, rng = opt(args, "--tab"), opt(args, "--gid"), opt(args, "--range")
    if gid is not None and not str(gid).lstrip("-").isdigit():
        sys.exit(f"--gid must be a number, got {gid!r}")
    try:
        if gid is not None and tab is None:
            tab = _tab_for_gid(_meta(sheets, sid), int(gid))
        elif tab is None and rng is None:
            tab = _first_tab(_meta(sheets, sid))
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sid, range=_range(tab, rng),
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING").execute()
    except Exception as e:
        _die(e, sid)
    rows = resp.get("values", [])
    fmt = (opt(args, "--format", "csv") or "csv").lower()
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        w = csv.writer(sys.stdout, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n")
        for r in rows:
            w.writerow(r)


def cmd_write(args):
    if not args:
        sys.exit("write needs <sheet|url>")
    sid = sheet_id(args[0])
    rng = opt(args, "--range")
    if not rng:
        sys.exit("write needs --range (e.g. --range A1, plus optional --tab NAME)")
    rows = _rows_from_input(args)
    sheets, _ = _services()
    try:
        resp = sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=_range(opt(args, "--tab"), rng),
            valueInputOption=_input_option(args), body={"values": rows}).execute()
    except Exception as e:
        _die(e, sid)
    print(f"wrote {resp.get('updatedCells')} cells to {resp.get('updatedRange')}")


def cmd_append(args):
    if not args:
        sys.exit("append needs <sheet|url>")
    sid = sheet_id(args[0])
    sheets, _ = _services()
    tab = opt(args, "--tab") or _first_tab(_meta(sheets, sid))
    rows = _rows_from_input(args)
    try:
        resp = sheets.spreadsheets().values().append(
            spreadsheetId=sid, range=_range(tab, None), valueInputOption=_input_option(args),
            insertDataOption="INSERT_ROWS", body={"values": rows}).execute()
    except Exception as e:
        _die(e, sid)
    print(f"appended {resp.get('updates', {}).get('updatedRows')} rows to "
          f"{resp.get('updates', {}).get('updatedRange')}")


def cmd_clear(args):
    if not args:
        sys.exit("clear needs <sheet|url>")
    sid = sheet_id(args[0])
    rng = opt(args, "--range")
    if not rng:
        sys.exit("clear needs --range")
    sheets, _ = _services()
    try:
        sheets.spreadsheets().values().clear(
            spreadsheetId=sid, range=_range(opt(args, "--tab"), rng), body={}).execute()
    except Exception as e:
        _die(e, sid)
    print(f"cleared {_range(opt(args, '--tab'), rng)}")


def cmd_create(args):
    title = opt(args, "--title")
    if not title:
        sys.exit("create needs --title NAME")
    tabs = [t.strip() for t in (opt(args, "--tabs", "") or "").split(",") if t.strip()]
    sheets, drive = _services()
    body = {"properties": {"title": title}}
    if tabs:
        body["sheets"] = [{"properties": {"title": t}} for t in tabs]
    try:
        ss = sheets.spreadsheets().create(body=body, fields="spreadsheetId,spreadsheetUrl").execute()
    except Exception as e:
        status, detail = _google.http_error(e)
        sys.exit(f"create failed [{status}]: {detail}")
    sid, url = ss["spreadsheetId"], ss["spreadsheetUrl"]
    print(f"created {title!r}\n  id:  {sid}\n  url: {url}")
    share = opt(args, "--share")
    if share:
        email, _, role = share.partition(":")
        try:
            drive.permissions().create(
                fileId=sid, body={"type": "user", "role": role or "writer", "emailAddress": email},
                sendNotificationEmail=True, fields="id").execute()
            print(f"  shared with {email} as {role or 'writer'}")
        except Exception as e:
            status, detail = _google.http_error(e)
            print(f"  WARN could not share [{status}]: {detail}")
    else:
        print(f"  note: owned by the service account; pass --share you@example.com for edit access.")


VERBS = {"whoami": cmd_whoami, "check": cmd_check, "tabs": cmd_check, "read": cmd_read,
         "write": cmd_write, "append": cmd_append, "clear": cmd_clear, "create": cmd_create}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown sheet verb {verb!r}. One of: {', '.join(sorted(set(VERBS)))}")
    fn(rest)
    return 0
