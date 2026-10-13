"""biz-connect browser bridge: native-messaging host launcher.

`bizconnect browser install` copies this file to ~/.config/biz-connect/browser/ and registers
host.cmd / host.sh (which run it under the browser venv) with the browser. It lives in the
central store, not in the plugin, because the plugin moves to a new versioned folder on every
update. At each start it finds the current plugin folder, tops up the browser venv if
browser/requirements.txt changed, and runs bizconnect.browser_host.

stdout is the native-messaging channel: nothing here may print to it.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent              # <store>/browser
os.environ.setdefault("BIZCONNECT_HOME", str(HERE.parent))


def _err(msg):
    try:
        sys.stderr.write("[biz-connect browser] %s\n" % msg)
    except Exception:
        pass


def _load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def plugin_root():
    """BIZCONNECT_PLUGIN_ROOT, else a pinned dev clone, else the installed plugin, else the folder
    recorded at install."""
    env = os.environ.get("BIZCONNECT_PLUGIN_ROOT")
    if env:
        return Path(env)
    cfg = _load(HERE / "config.json")
    if cfg.get("pin_plugin_root") and cfg.get("plugin_root"):
        return Path(cfg["plugin_root"])
    data = _load(Path.home() / ".claude" / "plugins" / "installed_plugins.json")
    entries = (data.get("plugins") or data).get("biz-connect@biz-connect") or []
    found = [e for e in entries if isinstance(e, dict) and e.get("installPath")
             and (Path(e["installPath"]) / "bizconnect" / "browser_host.py").exists()]
    if found:
        found.sort(key=lambda e: e.get("lastUpdated") or "")
        return Path(found[-1]["installPath"])
    if cfg.get("plugin_root"):
        return Path(cfg["plugin_root"])
    return None


def ensure_requirements(root):
    req = root / "browser" / "requirements.txt"
    if not req.exists():
        return
    want = hashlib.sha256(req.read_bytes()).hexdigest()
    marker = Path(sys.prefix) / ".requirements.sha256"
    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    if have == want:
        return
    _err("updating the browser venv ...")
    sink = sys.stderr if sys.stderr is not None else subprocess.DEVNULL
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
                       stdin=subprocess.DEVNULL, stdout=sink, stderr=sink)
    if r.returncode == 0:
        marker.write_text(want, encoding="utf-8")


def main():
    root = plugin_root()
    if not root or not (root / "bizconnect" / "browser_host.py").exists():
        _err("biz-connect plugin not found; run `bizconnect browser install` again")
        sys.exit(1)
    ensure_requirements(root)
    sys.path.insert(0, str(root))
    from bizconnect.browser_host import main as host_main
    host_main()


if __name__ == "__main__":
    main()
