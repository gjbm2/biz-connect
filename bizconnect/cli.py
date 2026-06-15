"""bizconnect CLI — `bizconnect <service> <verb> [args]`.

Services:  gdoc | notion | sheet | git
Top-level: doctor | init | version | help

Normally invoked through the launcher (scripts/bizconnect.py), which guarantees
the dependency venv exists. The plugin skills shell out to that launcher.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import __version__, config

SERVICES = {"gdoc": "gdocs", "gdocs": "gdocs", "notion": "notion",
            "sheet": "gsheets", "sheets": "gsheets", "gsheet": "gsheets", "git": "git"}

USAGE = """biz-connect — business-service connectors for this repo.

  bizconnect doctor                 check your setup (central store, creds, deps, connections.yaml)
  bizconnect init                   scaffold connections.yaml here + ensure the central store exists
  bizconnect update                 check for a newer plugin version (and how to apply it)
  bizconnect version

  bizconnect gdoc   push|pull|status|link|unlink|list   sync local Markdown <-> Google Docs
  bizconnect notion whoami|check|read|upload|fill        read pages, upload local media
  bizconnect sheet  whoami|check|read|write|append|clear|create
  bizconnect git    status|save|sync|pr                  standardised git flow

Bindings (which Doc/page this repo uses) live in ./connections.yaml.
Credentials live in the central store (%s).
""" % (config.home(),)


def _load_connector(name):
    import importlib
    return importlib.import_module(f".connectors.{name}", package="bizconnect")


def cmd_version():
    print(f"biz-connect {__version__}")
    return 0


def cmd_doctor():
    store = config.home()
    ok = True
    print(f"central store: {store}")
    sec = store / "secrets.env"
    print(f"  secrets.env:        {'present' if sec.exists() else 'MISSING'}")
    ok &= sec.exists()
    config.load_secrets()

    tok = config.secret("NOTION_TOKEN")
    print(f"  NOTION_TOKEN:       {'set' if tok else 'not set'}")

    sa = config.service_account_file()
    if sa.exists():
        try:
            import json
            email = json.loads(sa.read_text(encoding='utf-8')).get("client_email")
            print(f"  service account:    {email}")
        except Exception as e:
            print(f"  service account:    present but unreadable ({e})"); ok = False
    else:
        print(f"  service account:    MISSING ({sa})"); ok = False

    print("dependencies:")
    for mod, label in [("googleapiclient", "google-api-python-client"),
                       ("google.auth", "google-auth"), ("ruamel.yaml", "ruamel.yaml")]:
        try:
            __import__(mod)
            print(f"  {label}: ok")
        except ImportError:
            print(f"  {label}: MISSING (run via the launcher to bootstrap)"); ok = False

    data, path = config.load_connections()
    if path:
        print(f"connections.yaml: {path}")
        docs = config.get_path(data, "google.docs") or {}
        print(f"  google.share_with: {config.get_path(data, 'google.share_with') or '(unset)'}")
        print(f"  google.drive_folder: {config.get_path(data, 'google.drive_folder') or '(unset)'}")
        print(f"  notion.notes_page: {config.get_path(data, 'notion.notes_page') or '(unset)'}")
        print(f"  bound docs: {len(docs)}")
    else:
        print("connections.yaml: not found in this directory tree (run `bizconnect init`).")

    try:
        from . import update as _upd
        fc = _upd.check(force=True)
        tail = ("  — UPDATE AVAILABLE (run `bizconnect update`)" if fc.get("behind")
                else "" if fc.get("last_error") else "  — up to date")
        print(f"version: installed {fc.get('installed')}, latest {fc.get('latest')}{tail}")
    except Exception:
        pass

    print("\n" + ("OK" if ok else "Some checks failed — see above."))
    return 0 if ok else 1


GITIGNORE_GUARDS = ["service-account*.json", "*-service-account.json",
                    "secrets.env", ".env", ".bizconnect/"]


def _ensure_repo_gitignore(repo: Path):
    """Append secret/state guards to the consuming repo's .gitignore (idempotent).
    This is where `git save` runs, so the guards must exist HERE, not only in the plugin."""
    gi = repo / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [g for g in GITIGNORE_GUARDS if g not in existing]
    if missing:
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write("\n# biz-connect: never commit secrets / tool-owned sync state\n"
                     + "\n".join(missing) + "\n")
        print(f"updated {gi.name} with biz-connect secret guards")


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
        sec.write_text(
            "# biz-connect central secret store (per-user, NEVER commit).\n"
            "NOTION_TOKEN=\n"
            "# NOTION_VERSION=2022-06-28\n"
            "GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json\n"
            "# GOOGLE_IMPERSONATE_SUBJECT=you@domain   # needs domain-wide delegation (drive+documents)\n",
            encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(sec, 0o600)
            except OSError:
                pass
        print(f"created {sec} (fill in NOTION_TOKEN; drop service-account.json in {store})")
    else:
        print(f"central store already set up: {store}")

    existing = config.find_connections()       # walk up; don't shadow an ancestor file
    if existing:
        print(f"connections.yaml already present at {existing} — leaving it as-is.")
        return 0
    conn = Path.cwd() / config.CONN_NAME
    example = Path(__file__).resolve().parents[1] / "examples" / "connections.example.yaml"
    conn.write_text(example.read_text(encoding="utf-8") if example.exists() else _MIN_CONN, encoding="utf-8")
    _ensure_repo_gitignore(Path.cwd())
    print(f"created {conn} — edit it to set this repo's attachpoints.")
    return 0


_MIN_CONN = """# biz-connect attachpoints for this repo (committed; NO secrets).
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
    if cmd == "version":
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
