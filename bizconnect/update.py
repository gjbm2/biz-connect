"""Self-maintaining freshness check.

Compares the INSTALLED plugin version against the repo's `main` and nudges (once a
day, to stderr) when behind. Network is optional and fail-open — it never blocks a
command, and a daily on-disk cache means at most one tiny HTTP call per day. An
offline-staleness fallback nudges if freshness hasn't been verified in a while.

Config (env / central secrets.env):
  BIZCONNECT_UPDATE_CHECK=off       disable entirely (default on)
  BIZCONNECT_UPDATE_MAX_AGE_DAYS=14 offline-staleness threshold
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from . import config

DEFAULT_REPO = "gjbm2/biz-connect"
CHECK_INTERVAL_H = 24
NUDGE_INTERVAL_H = 24
DEFAULT_MAX_AGE_DAYS = 14


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _plugin_json():
    try:
        return json.loads((plugin_root() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _repo_slug(pj):
    r = pj.get("repository") or ""
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", r)
    if m:
        return m.group(1)
    if "/" in r and " " not in r and "://" not in r:
        return r.strip()
    return DEFAULT_REPO


def _ver_tuple(v):
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums[:3]) or (0,)


def _cache_path() -> Path:
    return config.home() / ".update-check.json"


def _load_cache() -> dict:
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(c):
    try:
        config.home().mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _enabled() -> bool:
    return (config.secret("BIZCONNECT_UPDATE_CHECK", "on") or "on").lower() not in ("off", "0", "false", "no")


def check(force=False) -> dict:
    """Return the freshness verdict, using a <=24h on-disk cache unless force=True."""
    pj = _plugin_json()
    installed = pj.get("version", "0")
    repo = _repo_slug(pj)
    old = _load_cache()
    now = time.time()
    age_h = (now - old.get("checked_at", 0)) / 3600
    if not force and old.get("installed") == installed and old.get("checked_at") and age_h < CHECK_INTERVAL_H:
        return old

    latest, err = installed, None
    try:
        url = f"https://raw.githubusercontent.com/{repo}/main/.claude-plugin/plugin.json"
        with urllib.request.urlopen(url, timeout=4) as r:
            latest = json.loads(r.read().decode("utf-8")).get("version", installed)
    except Exception as e:
        err = str(e)

    new = {
        "installed": installed,
        "latest": latest,
        "behind": _ver_tuple(latest) > _ver_tuple(installed),
        "repo": repo,
        "checked_at": now if not err else old.get("checked_at", 0),
        "last_attempt": now,
        "last_error": err,
        "nudged_at": old.get("nudged_at", 0),
    }
    _save_cache(new)
    return new


def maybe_nudge():
    """Print a one-line stderr nudge if behind (or long-unverified), throttled daily."""
    try:
        if not _enabled():
            return
        c = check(force=False)
        now = time.time()
        if (now - c.get("nudged_at", 0)) / 3600 < NUDGE_INTERVAL_H:
            return
        msg = None
        if c.get("behind"):
            msg = (f"[biz-connect] update available: {c['installed']} -> {c['latest']}. "
                   f"Run `/plugin update biz-connect` (or `bizconnect update`).")
        else:
            max_age = int(config.secret("BIZCONNECT_UPDATE_MAX_AGE_DAYS", str(DEFAULT_MAX_AGE_DAYS)) or DEFAULT_MAX_AGE_DAYS)
            last_ok = c.get("checked_at", 0)
            if last_ok and (now - last_ok) / 86400 > max_age:
                msg = (f"[biz-connect] freshness not verified in >{max_age}d (offline?). "
                       f"Consider `/plugin update biz-connect`.")
        if msg:
            sys.stderr.write(msg + "\n")
            c["nudged_at"] = now
            _save_cache(c)
    except Exception:
        pass


def cmd_update(argv):
    c = check(force=True)
    print(f"installed: {c['installed']}")
    print(f"latest:    {c['latest']}  (from {c.get('repo')})")
    if c.get("last_error"):
        print(f"  (could not reach GitHub: {c['last_error']})")
    if c.get("behind"):
        print("\nAn update is available. Update with:")
        print("  /plugin update biz-connect       # in the Claude Code REPL (installed plugin)")
        print("  git -C <repo> pull               # if you cloned the repo directly")
    elif not c.get("last_error"):
        print("up to date.")
    return 0
