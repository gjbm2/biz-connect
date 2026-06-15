"""secrets — pull this repo's shared, scoped credentials from a secret manager into
the per-user central store, so onboarding and rotation are programmatic.

The Notion token and the Google service-account key are ONE scoped set, shared by the
team. Instead of passing files around, they live in a secret manager; access is granted
by an IAM group, and each person pulls them with THEIR OWN identity — so there is no key
to hand out just to bootstrap the key. Offboard = drop the group; rotate = add a new
secret version and everyone re-pulls.

Today this speaks Google Secret Manager and authenticates with Application Default
Credentials — run `gcloud auth application-default login` (as yourself) first. It does
NOT use the service account it is fetching, so there is no chicken-and-egg.

Config (connections.yaml; IDs only, no secrets):

  secrets:
    provider: gcp
    project: my-gcp-project-id          # or set $GOOGLE_CLOUD_PROJECT
    pull:
      - name: nous-reg-notion-token     # the Secret Manager secret id
        env: NOTION_TOKEN               # -> upsert `KEY=value` in secrets.env
      - name: nous-reg-google-sa-key
        file: service-account.json      # -> write into the central store
        # version: latest               # optional (default: latest)

Verbs
-----
  pull [--no-login]  fetch every configured secret into the central store (signs you in to
                     Google via the browser on first run unless --no-login)
  status [--check]   show what's configured (and, with --check, whether you can read it)
"""
from __future__ import annotations

import base64
import os
import re
import sys

from .. import config

SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _cfg(data):
    sec = config.get_path(data, "secrets") or {}
    if not isinstance(sec, dict) or not sec:
        sys.exit("no `secrets:` block in connections.yaml — nothing to pull.\n"
                 "Add one (see `bizconnect secrets help`) to enable programmatic credential pulls.")
    provider = str(sec.get("provider") or "gcp").lower()
    if provider != "gcp":
        sys.exit("secrets.provider %r not supported yet (only 'gcp')." % provider)
    project = sec.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit("secrets.project not set in connections.yaml (and $GOOGLE_CLOUD_PROJECT is empty).")
    items = sec.get("pull") or []
    if not items:
        sys.exit("secrets.pull is empty — list the secrets to fetch.")
    return project, items


def _adc(auto_login=True):
    """Application Default Credentials — the user's OWN identity (gcloud ADC), NOT the SA.

    If ADC is missing and auto_login is set, transparently drive the gcloud browser
    sign-in, so the caller never has to run a CLI by hand — completing the browser
    consent is the only human action. Pass auto_login=False for non-interactive callers."""
    try:
        import google.auth
    except ImportError:
        sys.exit("google-auth missing — run via the launcher (it bootstraps the venv).")
    try:
        creds, _ = google.auth.default(scopes=[SCOPE])
        return creds
    except Exception as e:                       # DefaultCredentialsError and friends
        if not auto_login:
            sys.exit("not signed in to Google. Run `bizconnect secrets pull` (it signs you in),\n"
                     "or `gcloud auth application-default login`.\n(%s)" % e)
    import shutil
    import subprocess
    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        sys.exit("Google Cloud SDK (gcloud) is needed for the one-time sign-in but was not found.\n"
                 "Install it (https://cloud.google.com/sdk/docs/install), then re-run.")
    print("Signing you in to Google — a browser window will open…")
    try:
        subprocess.run([gcloud, "auth", "application-default", "login"], check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Google sign-in didn't complete (%s). Re-run to try again." % e)
    creds, _ = google.auth.default(scopes=[SCOPE])
    return creds


def _service(creds):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("google-api-python-client missing — run via the launcher.")
    return build("secretmanager", "v1", credentials=creds, cache_discovery=False)


def _access(service, project, name, version="latest"):
    from googleapiclient.errors import HttpError
    res_name = "projects/%s/secrets/%s/versions/%s" % (project, name, version or "latest")
    try:
        res = service.projects().secrets().versions().access(name=res_name).execute()
    except HttpError as e:
        status = str(getattr(getattr(e, "resp", None), "status", "?"))
        if status == "403":
            sys.exit("403 on %s — your account lacks secretAccessor on it. Ask to be added to "
                     "the access group (roles/secretmanager.secretAccessor)." % name)
        if status == "404":
            sys.exit("404 — secret %r (version %s) not found in project %s." % (name, version or "latest", project))
        sys.exit("error accessing %s: %s" % (name, e))
    return base64.b64decode(res["payload"]["data"])


def _store():
    s = config.home()
    s.mkdir(parents=True, exist_ok=True)
    return s


def _chmod600(p):
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _upsert_env(store, key, value):
    """Set KEY=value in secrets.env, replacing any existing line, preserving the rest."""
    p = store / "secrets.env"
    val = value.decode("utf-8").replace("\r", "").strip().replace("\n", " ")
    line = "%s=%s" % (key, val)
    lines = (p.read_text(encoding="utf-8").splitlines() if p.exists()
             else ["# biz-connect central secret store (per-user, NEVER commit)."])
    pat = re.compile(r"^\s*(export\s+)?" + re.escape(key) + r"\s*=")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = line
            break
    else:
        lines.append(line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod600(p)


def _write_file(store, rel, data):
    p = store / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    _chmod600(p)
    return p


# --- verbs --------------------------------------------------------------------
def cmd_pull(argv):
    data, _ = config.require_connections()
    project, items = _cfg(data)
    service = _service(_adc(auto_login="--no-login" not in argv))
    store = _store()
    n = 0
    for it in items:
        name = it.get("name")
        if not name:
            sys.exit("each secrets.pull entry needs a `name`.")
        blob = _access(service, project, name, it.get("version", "latest"))
        if it.get("env"):
            _upsert_env(store, it["env"], blob)
            print("pulled %s -> secrets.env:%s" % (name, it["env"]))
        elif it.get("file"):
            dest = _write_file(store, it["file"], blob)
            print("pulled %s -> %s" % (name, dest))
        else:
            sys.exit("secrets.pull entry %r needs `env:` or `file:`." % name)
        n += 1
    print("secrets pull: wrote %d secret(s) into %s" % (n, store))
    print("verify with `bizconnect doctor`.")


def cmd_status(argv):
    data, _ = config.require_connections()
    project, items = _cfg(data)
    print("provider: gcp")
    print("project:  %s" % project)
    check = "--check" in argv
    service = _service(_adc(auto_login=False)) if check else None
    for it in items:
        name = it.get("name", "(unnamed)")
        target = ("secrets.env:%s" % it["env"]) if it.get("env") else (it.get("file") or "(no target)")
        suffix = ""
        if check and it.get("name"):
            try:
                _access(service, project, it["name"], it.get("version", "latest"))
                suffix = "  [ok]"
            except SystemExit as e:
                suffix = "  [%s]" % ((str(e).splitlines() or ["error"])[0])
        print("  %-28s -> %-24s%s" % (name, target, suffix))


VERBS = {"pull": cmd_pull, "status": cmd_status}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit("unknown secrets verb %r. One of: %s" % (verb, ", ".join(VERBS)))
    fn(rest)
    return 0
