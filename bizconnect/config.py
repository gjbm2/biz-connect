"""bizconnect.config — the two-location model that makes connectors repo-agnostic
and shareable across users.

  1. CENTRAL STORE  (per-user, machine-level, NEVER committed to any repo)
     Located via $BIZCONNECT_HOME, default ~/.config/biz-connect.
     Holds:  secrets.env  (NOTION_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE, ...)
             service-account.json  (the Google key)
             .venv/       (dependencies, created by the launcher)
     Rotate a credential here once and every repo picks it up.

  2. REPO CONNECTIONS  (per-repo, committed, contains NO secrets)
     connections.yaml in the repo root, found by walking up from the cwd.
     Declares this repo's "attachpoints": which Google Doc / Drive folder /
     Notion page this repo binds to. IDs and URLs are not secrets, so this file
     is safe to commit. Connectors read it, and may write IDs back into it
     (comment-preserving, via ruamel.yaml).

Nothing here imports heavy deps at module load, so `doctor` and error messages
work even before the venv is populated.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CONN_NAME = "connections.yaml"
_loaded = False


# --------------------------------------------------------------- central store
def home() -> Path:
    """The central secret store directory."""
    env = os.environ.get("BIZCONNECT_HOME")
    base = Path(env) if env else Path.home() / ".config" / "biz-connect"
    return base.expanduser()


def load_secrets() -> dict:
    """Load <store>/secrets.env into os.environ and return it as a dict.

    Dotenv-ish: tolerates `export FOO=bar`, strips one pair of surrounding quotes,
    and drops an inline ` # comment` on unquoted values. secrets.env is AUTHORITATIVE
    (it overrides any pre-existing env var) so that rotating a credential in the store
    actually takes effect — a stale exported var must not silently win.
    """
    global _loaded
    data: dict[str, str] = {}
    env_path = home() / "secrets.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]                       # strip matched surrounding quotes; keep inner verbatim
            else:
                h = v.find(" #")                  # dotenv-style inline comment (unquoted only)
                if h != -1:
                    v = v[:h].rstrip()
            data[k] = v
            os.environ[k] = v
    _loaded = True
    return data


def secret(key: str, default=None, required: bool = False):
    if not _loaded:
        load_secrets()
    val = os.environ.get(key, default)
    if required and not val:
        sys.exit(
            f"missing required secret {key!r}.\n"
            f"Add it to {home() / 'secrets.env'} then re-run. "
            f"(`bizconnect doctor` checks your setup.)"
        )
    return val


def service_account_file() -> Path:
    """Resolve GOOGLE_SERVICE_ACCOUNT_FILE; relative paths resolve against the store."""
    if not _loaded:
        load_secrets()
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = home() / p
    return p


# ------------------------------------------------------------ repo connections
def find_connections(start=None) -> Path | None:
    """Walk up from `start` (default cwd) to find connections.yaml."""
    d = Path(start or os.getcwd()).resolve()
    for cand in [d, *d.parents]:
        f = cand / CONN_NAME
        if f.exists():
            return f
    return None


def repo_root(start=None) -> Path | None:
    f = find_connections(start)
    return f.parent if f else None


def _yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("ruamel.yaml missing — run via the launcher "
                 "(scripts/bizconnect.py bootstraps the central-store venv).")
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096                       # don't wrap long URLs/ids
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_connections(start=None):
    """Return (data, path). `data` is a ruamel mapping; ({}, None) if no file found."""
    f = find_connections(start)
    if not f:
        return {}, None
    with open(f, encoding="utf-8") as fh:
        data = _yaml().load(fh)
    return (data or {}), f


def require_connections(start=None):
    """Like load_connections but exits with guidance if no file is found."""
    data, path = load_connections(start)
    if path is None:
        sys.exit(
            f"no {CONN_NAME} found in this repo (searched up from {Path(start or os.getcwd()).resolve()}).\n"
            f"Run `bizconnect init` in the repo root to create one."
        )
    return data, path


def save_connections(data, path):
    """Write connections.yaml back, preserving comments and key order."""
    with open(path, "w", encoding="utf-8") as fh:
        _yaml().dump(data, fh)


def get_path(data, dotted, default=None):
    """Read a nested value by 'a.b.c' dotted path; tolerant of missing keys."""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ------------------------------------------------------- multi-deliverable scope
# A repo can host several deliverables (e.g. one consultation response each) under
# deliverables/<slug>/, each with its own pipeline.yaml. connections.yaml stays a single
# umbrella file at the repo root; per-deliverable attachpoints live under a
# `deliverables.<slug>:` block. The "active deliverable" is named by the nearest
# pipeline.yaml's top-level `deliverable:` key, so running a command from inside a
# deliverable directory scopes every binding to it. Repos with no `deliverable:` key and
# no `deliverables:` block behave exactly as before (scoped() == get_path()).
PIPELINE_NAME = "pipeline.yaml"


def active_deliverable(start=None):
    """The `deliverable:` slug from the nearest pipeline.yaml walking up from `start`
    (default cwd), or None. None means 'not inside a layered deliverable' — callers then
    fall back to top-level config, preserving single-deliverable behaviour."""
    d = Path(start or os.getcwd()).resolve()
    for cand in [d, *d.parents]:
        f = cand / PIPELINE_NAME
        if f.exists():
            try:
                with open(f, encoding="utf-8") as fh:
                    data = _yaml().load(fh) or {}
            except Exception:
                return None
            return data.get("deliverable")
    return None


def scoped(data, dotted, deliverable=None, start=None):
    """Resolve `dotted` preferring the active deliverable's scope, then the top level:
    `deliverables.<deliverable>.<dotted>` if present, else `get_path(data, dotted)`.
    `deliverable` defaults to active_deliverable(start). Backward-compatible — with no
    active deliverable or no matching scoped key, this is exactly get_path()."""
    if deliverable is None:
        deliverable = active_deliverable(start)
    if deliverable:
        val = get_path(data, "deliverables.%s.%s" % (deliverable, dotted))
        if val is not None:
            return val
    return get_path(data, dotted)


def scoped_parent(data, deliverable=None, start=None, create=False):
    """The mapping a top-level key (e.g. 'notion', 'google') should be read/written UNDER for the
    active deliverable: data['deliverables'][slug] when one is active, else `data` itself. With
    create=True, builds the deliverables.<slug> maps as needed — this is how `register init` /
    `docreg init` write their binding into the right scope when run inside a deliverable."""
    if deliverable is None:
        deliverable = active_deliverable(start)
    if not deliverable:
        return data
    from ruamel.yaml.comments import CommentedMap
    dl = data.get("deliverables")
    if not isinstance(dl, dict):
        if not create:
            return data
        dl = CommentedMap(); data["deliverables"] = dl
    slot = dl.get(deliverable)
    if not isinstance(slot, dict):
        if not create:
            return data
        slot = CommentedMap(); dl[deliverable] = slot
    return slot


def list_deliverables(start=None):
    """Enumerate an umbrella repo's deliverables: scan <repo>/deliverables/*/pipeline.yaml and
    read each one's `deliverable:` slug + `title:`. Returns [{slug, title, dir}] sorted by slug.
    Empty for a single-deliverable repo (no deliverables/ dir) — callers then just use cwd."""
    root = repo_root(start)
    if not root:
        return []
    base = root / "deliverables"
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        pf = d / PIPELINE_NAME
        if not pf.exists():
            continue
        try:
            with open(pf, encoding="utf-8") as fh:
                pd = _yaml().load(fh) or {}
        except Exception:
            pd = {}
        out.append({"slug": pd.get("deliverable") or d.name,
                    "title": pd.get("title") or "", "dir": str(d)})
    return out
