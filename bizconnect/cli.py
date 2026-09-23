"""bizconnect CLI — `bizconnect <service> <verb> [args]`.

Services:  gdoc | notion | sheet | xlsx | git | compose | deliverable | register | docreg | secrets
Top-level: doctor | init | update | version | help

Normally invoked through the `bizconnect` command shim (bin/, on PATH inside Claude Code)
or the launcher it runs (scripts/bizconnect.py), which guarantees the dependency venv
exists. The plugin skills shell out to that shim/launcher.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import __version__, config

SERVICES = {"gdoc": "gdocs", "gdocs": "gdocs", "notion": "notion",
            "sheet": "gsheets", "sheets": "gsheets", "gsheet": "gsheets", "git": "git",
            "xlsx": "xlsx", "workbook": "xlsx",
            "compose": "compose", "register": "register", "docreg": "docreg",
            "secrets": "secrets", "secret": "secrets",
            "deliverable": "deliverable", "deliverables": "deliverable"}

NOTION_INTEGRATIONS_URL = "https://www.notion.so/profile/integrations"

USAGE = """biz-connect — business-service connectors for this repo.

  bizconnect doctor                 check your setup (central store, Notion token, Google creds, deps, repo)
  bizconnect init                   scaffold connections.yaml here + ensure the central store exists
  bizconnect update                 check for a newer plugin version (and how to apply it)
  bizconnect version

  bizconnect notion link|map|outline|locate|diff|status|push|pull   two-way sync of project files <-> Notion (notion.yaml)
  bizconnect notion whoami|check|read|upload|fill|sync   token check, read pages, upload local media, mirror a hub
  bizconnect gdoc   push|pull|status|link|unlink|list|comments|diff|resolve|docx   Markdown <-> Google Docs (+ feedback capture, .docx export)
  bizconnect sheet  whoami|check|read|write|append|clear|create
  bizconnect xlsx   diff OLD.xlsx NEW.xlsx [-o OUT.md] [--formulas] [--values]  structural workbook diff
  bizconnect git    status|save|sync|pr                  standardised git flow
  bizconnect compose status|run|accept|scaffold|graph    config-driven doc-composition pipeline
  bizconnect deliverable list|new <slug>  stand up / list deliverables in an umbrella repo
  bizconnect register init|pull|upsert|open|status|resolve|journal   Notion open-points register (review feedback)
  bizconnect docreg  init|log|list|pull   Notion catalogue of produced doc instances + versions
  bizconnect secrets pull|status          pull this repo's shared scoped creds (GCP Secret Manager) into the central store

Notion sync is configured per folder in notion.yaml; other bindings (which Doc/page this
repo uses) live in ./connections.yaml. The doc pipeline is configured by ./pipeline.yaml
(see examples/pipeline.example.yaml). Credentials live in the central store (%s).
Without the `bizconnect` command on PATH, run: python3 <plugin>/scripts/bizconnect.py ...
(`python` or `py -3` on Windows).
""" % (config.home(),)


def _load_connector(name):
    import importlib
    return importlib.import_module(f".connectors.{name}", package="bizconnect")


def cmd_version():
    print(f"biz-connect {__version__}")
    return 0


# ---------------------------------------------------------------------- doctor
class _Report:
    """Collects doctor lines; a FAIL means something configured is broken, a WARNING
    means something optional is unavailable."""

    def __init__(self):
        self.fails = 0
        self.warns = 0

    @staticmethod
    def line(label, text):
        print(f"  {(label + ':').ljust(19)} {text}")

    def warn(self, label, text):
        self.warns += 1
        self.line(label, "WARNING: " + text)

    def fail(self, label, text):
        self.fails += 1
        self.line(label, "FAIL: " + text)


def _git_toplevel():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _too_wide(folder):
    """The home folder or a drive/filesystem root: never a sensible repo root."""
    folder = Path(folder).resolve()
    return folder == Path.home().resolve() or folder.parent == folder


def _stray_msg(conn):
    return (f"a stray {config.CONN_NAME} at {conn} makes it the repo root for every folder "
            f"below; move or delete it")


def _doctor_notion(rep, tok, from_file):
    """Validate NOTION_TOKEN with GET /users/me. True when the token works."""
    src = "" if from_file else " (from the environment)"
    try:
        from .connectors import notion as N
    except Exception as e:                              # pragma: no cover - broken install
        rep.fail("NOTION_TOKEN", f"set{src}, but the Notion connector failed to load: {e}")
        return False
    try:
        status, body = N.api("GET", "/users/me", retries=1)
    except SystemExit as e:                             # NotionError and friends
        status, body = 0, {"message": str(e)}
    except Exception as e:
        status, body = 599, {"message": str(e)}
    body = body if isinstance(body, dict) else {"message": str(body)}
    msg = body.get("message") or body.get("code") or ""
    if 200 <= status < 300:
        ws = (body.get("bot") or {}).get("workspace_name")
        rep.line("NOTION_TOKEN", f"ok{src} — integration {body.get('name')!r}"
                 + (f" in workspace {ws!r}" if ws else ""))
        return True
    if status in (401, 403):
        rep.fail("NOTION_TOKEN", f"rejected by Notion [{status}]{': ' + msg if msg else ''} — put the "
                 f"integration's secret (from {NOTION_INTEGRATIONS_URL}) in {config.home() / 'secrets.env'}")
        return False
    if 400 <= status < 500:
        rep.fail("NOTION_TOKEN", f"check failed [{status}]{': ' + msg if msg else ''}")
        return False
    rep.warn("NOTION_TOKEN", f"set{src}, but could not verify it (Notion unreachable?)"
             f"{' [%s]' % status if status else ''}{': ' + msg if msg else ''}")
    return False


def _doctor_manifests(root):
    """notion.yaml files under the repo root, relative to it ([] if none; None if the scan failed)."""
    try:
        from .connectors import notiontree
        found = notiontree.find_manifests(root)
    except (Exception, SystemExit):                     # tolerate a broken/older notiontree
        return None
    out = []
    for m in found:
        try:
            out.append(Path(m).resolve().relative_to(root).as_posix())
        except Exception:
            out.append(str(m))
    return out


def cmd_doctor():
    rep = _Report()
    print(f"biz-connect {__version__}  ({Path(__file__).resolve().parents[1]})")
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")

    store = config.home()
    print(f"central store: {store}")
    sec = store / "secrets.env"
    if sec.exists():
        rep.line("secrets.env", "present")
    else:
        rep.warn("secrets.env", "not found — run `bizconnect init` in your repo root to create it")
    try:
        file_vals = config.load_secrets()
    except Exception as e:
        rep.fail("secrets.env", f"unreadable: {e}")
        file_vals = {}

    # repo first: the Notion/Google lines mention what this repo would use.
    root, root_kind, conn_data = None, None, {}
    try:
        conn_data, conn_path = config.load_connections()
    except (Exception, SystemExit) as e:                # bad YAML, or ruamel.yaml missing (sys.exit)
        conn_path = config.find_connections()
        rep_conn_error = f"unreadable: {e}"
    else:
        rep_conn_error = None
    if conn_path:
        root, root_kind = conn_path.parent.resolve(), "connections.yaml"
    else:
        top = _git_toplevel()
        if top:
            root, root_kind = top, "git top-level"
    too_wide = bool(root) and _too_wide(root)
    manifests = _doctor_manifests(root) if root and not too_wide else []

    # -- Notion
    print("Notion:")
    notion_ok = False
    tok = config.secret("NOTION_TOKEN")
    if tok:
        notion_ok = _doctor_notion(rep, tok, bool(file_vals.get("NOTION_TOKEN")))
    else:
        need = f" (this repo has {len(manifests)} notion.yaml that need it)" if manifests else ""
        rep.warn("NOTION_TOKEN", f"not set — Notion connectors unavailable{need}. Create an internal "
                 f"integration at {NOTION_INTEGRATIONS_URL} and put its secret in {sec}")
    nv = os.environ.get("NOTION_VERSION")
    if nv and nv != "2022-06-28":
        rep.warn("NOTION_VERSION", f"set to {nv!r} — remove it; the Notion sync pins the API version it needs")

    # -- Google
    print("Google (gdoc / sheet connectors):")
    google_ok = False
    sa = config.service_account_file()
    if sa.exists():
        try:
            import json
            email = json.loads(sa.read_text(encoding="utf-8-sig")).get("client_email")
            if email:
                rep.line("service account", email)
                google_ok = True
            else:
                rep.fail("service account", f"{sa} has no client_email — is it a service-account key?")
        except Exception as e:
            rep.fail("service account", f"{sa} is unreadable ({e})")
        subj = config.secret("GOOGLE_IMPERSONATE_SUBJECT")
        if subj:
            rep.line("impersonating", subj)
    else:
        docs = config.get_path(conn_data, "google.docs") or {}
        need = f"; this repo binds {len(docs)} Google Doc(s)" if docs else ""
        rep.warn("service account", f"not found ({sa}) — Google connectors unavailable{need}. "
                 f"Not needed for Notion.")

    # -- dependencies (the launcher installs them into the central-store venv)
    print("dependencies:")
    for mod, label, needed in [("ruamel.yaml", "ruamel.yaml", True),
                               ("googleapiclient", "google-api-python-client", google_ok),
                               ("google.auth", "google-auth", google_ok)]:
        try:
            __import__(mod)
            rep.line(label, "ok")
        except ImportError:
            hint = "missing (run via `bizconnect` / scripts/bizconnect.py to bootstrap)"
            (rep.fail if needed else rep.warn)(label, hint)

    # -- this repo
    if root:
        print(f"repo: {root}  (root from {root_kind})")
        if too_wide:
            rep.warn("repo root", f"{root} is your home folder / a drive root — " + (
                _stray_msg(conn_path) if conn_path else "a git repo there makes it the repo root for every folder below"))
    else:
        print("repo: none found (no connections.yaml and not inside a git repository)")
    if rep_conn_error:
        rep.fail("connections.yaml", rep_conn_error)
    elif conn_path:
        rep.line("connections.yaml", str(conn_path))
        shown = [(k, config.get_path(conn_data, k)) for k in
                 ("google.share_with", "google.drive_folder", "notion.notes_page")]
        rdb = config.scoped(conn_data, "notion.register_db") or {}
        ddb = config.scoped(conn_data, "notion.docs_registry") or {}
        shown += [("notion.register_db", rdb.get("database_id")),
                  ("notion.docs_registry", ddb.get("database_id"))]
        for k, v in shown:
            if v:
                rep.line("  " + k, v)
        deliv = config.active_deliverable()
        if deliv:
            rep.line("  active deliverable", deliv)
        secs = config.get_path(conn_data, "secrets") or {}
        if secs:
            rep.line("  secrets", f"provider={secs.get('provider', 'gcp')} "
                     f"project={secs.get('project') or '(unset)'} pull={len(secs.get('pull') or [])}")
        docs = config.get_path(conn_data, "google.docs") or {}
        if docs:
            rep.line("  bound Google Docs", str(len(docs)))
    else:
        rep.line("connections.yaml", "not found (optional for Notion sync; `bizconnect init` recommended)")
    if root and not too_wide:
        if manifests is None:
            rep.line("notion.yaml", "(could not scan this repo)")
        elif manifests:
            rep.line("notion.yaml", f"{len(manifests)} found")
            for m in manifests:
                print(f"    {m}")
        else:
            rep.line("notion.yaml", "none (set up sync with `bizconnect notion link <dir> <hub-url>`)")

    try:
        from . import update as _upd
        fc = _upd.check(force=True)
        tail = ("  — UPDATE AVAILABLE (run `bizconnect update`)" if fc.get("behind")
                else "" if fc.get("last_error") else "  — up to date")
        print(f"version: installed {fc.get('installed')}, latest {fc.get('latest')}{tail}")
    except Exception:
        pass

    print()
    if rep.fails:
        print("Some checks failed — see FAIL above.")
        return 1
    if not (notion_ok or google_ok):
        why = ["Notion could not be verified — check your network and re-run" if tok
               else "Notion needs NOTION_TOKEN"]
        if not sa.exists():
            why.append("Google needs a service account")
        print("Nothing is broken, but no connector is ready yet — see the WARNINGs above "
              "(%s)." % "; ".join(why))
        return 0
    print("OK" + (f" ({rep.warns} warning(s) above)" if rep.warns else ""))
    return 0


# ------------------------------------------------------------------------ init
GITIGNORE_GUARDS = ["service-account*.json", "*-service-account.json",
                    "secrets.env", ".env", ".bizconnect/"]


def _ensure_repo_gitignore(repo: Path):
    """Append secret/state guards to the consuming repo's .gitignore (idempotent).
    This is where `git save` runs, so the guards must exist HERE, not only in the plugin."""
    gi = repo / ".gitignore"
    existing = gi.read_text(encoding="utf-8-sig") if gi.exists() else ""
    missing = [g for g in GITIGNORE_GUARDS if g not in existing]
    if missing:
        with open(gi, "a", encoding="utf-8", newline="\n") as fh:
            lead = "" if not existing or existing.endswith("\n") else "\n"
            fh.write(lead + "\n# biz-connect: never commit secrets / tool-owned sync state\n"
                     + "\n".join(missing) + "\n")
        print(f"updated {gi} with biz-connect secret/state guards")


SECRETS_TEMPLATE = """\
# biz-connect central secret store (per-user, NEVER commit).
#
# Notion: create an internal integration at https://www.notion.so/profile/integrations
# (capabilities: Read content, Update content, Insert content), paste its secret below,
# then share your hub page with it (the page's ... menu -> Connections; sub-pages inherit).
NOTION_TOKEN=
# Do not set NOTION_VERSION: the Notion sync pins the API version it needs.
#
# Google connectors only (gdoc / sheet): the service-account key file, relative to this folder.
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
# GOOGLE_IMPERSONATE_SUBJECT=you@domain   # needs domain-wide delegation (drive+documents)
"""


def cmd_init():
    store = config.home()
    store.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(store, 0o700)
        except OSError:
            pass
    sec = store / "secrets.env"
    if not sec.exists():
        with open(sec, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(SECRETS_TEMPLATE)
        if os.name != "nt":
            try:
                os.chmod(sec, 0o600)
            except OSError:
                pass
        print(f"created {sec}\n  Notion: fill in NOTION_TOKEN (see the comments in the file).\n"
              f"  Google (gdoc/sheet only): drop service-account.json in {store}.")
    else:
        print(f"central store already set up: {store}")

    here = Path.cwd().resolve()
    top = _git_toplevel()
    existing = config.find_connections()       # walk up; don't shadow an ancestor file ...
    if existing:
        parent = existing.parent.resolve()
        above = bool(top) and parent in top.parents
        if not (_too_wide(parent) or above):
            print(f"connections.yaml already present at {existing} — leaving it as-is.")
            _ensure_repo_gitignore(existing.parent)
            return 0
        # ... unless it's a stray (home / drive root / outside this git repo): leave that
        # folder alone and give this repo its own
        print(f"WARNING: {_stray_msg(existing)}" + (f" (it is above this git repo, {top})" if above else ""))
        here = top or here
    if _too_wide(here):
        print(f"not creating {config.CONN_NAME} in your home folder / a drive root (it would make every "
              f"folder below it one repo) — cd to a repo root and run `bizconnect init` there.")
        return 0
    if top and top != here:
        print(f"note: this is a subfolder of the git repo {top}; the repo root is usually the right "
              f"place for {config.CONN_NAME}.")
    conn = here / config.CONN_NAME
    if conn.exists():                          # never overwrite one
        print(f"connections.yaml already present at {conn} — leaving it as-is.")
        return 0
    example = Path(__file__).resolve().parents[1] / "examples" / "connections.example.yaml"
    with open(conn, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(example.read_text(encoding="utf-8-sig") if example.exists() else _MIN_CONN)
    _ensure_repo_gitignore(here)
    print(f"created {conn} — edit it to set this repo's attachpoints.")
    return 0


_MIN_CONN = """# biz-connect attachpoints for this repo (committed; NO secrets).
# Notion file sync is configured per folder in notion.yaml (`bizconnect notion link`).
google:
  share_with:            # your email; new Docs are shared back to you
  drive_folder:          # optional Drive folder/shared-drive id for new Docs
  docs: {}               # local-markdown -> Google Doc bindings (filled by `gdoc push`)
notion:
  notes_page:            # default page id/url for `notion ... .`
"""


def main(argv=None):
    try:                                            # Windows consoles default to cp1252;
        sys.stdout.reconfigure(encoding="utf-8")    # connector output (em-dashes, IDs) is UTF-8.
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd in ("version", "--version", "-V"):
        return cmd_version()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "init":
        return cmd_init()
    if cmd == "update":
        from . import update as _upd
        return _upd.cmd_update(argv[1:])
    try:                                            # throttled freshness nudge (fail-open)
        from . import update as _upd
        _upd.maybe_nudge()
    except Exception:
        pass
    mod = SERVICES.get(cmd)
    if not mod:
        sys.stderr.write(f"unknown command {cmd!r}; try `bizconnect help`\n")
        return 2
    return _load_connector(mod).run(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
