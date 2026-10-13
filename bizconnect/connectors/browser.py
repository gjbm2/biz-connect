"""browser — the biz-connect browser bridge: select text on any page, ask Claude Code about it.

A browser extension (installed unpacked, in developer mode) sends the selection and its page to
Claude Code running headless in your workspace, with that repo's CLAUDE.md, skills, plugins and
MCP servers. Edits go through biz-connect for Notion and Google Docs, and straight into the page
for any other site. Full guide: the browser-bridge skill.

  install [--dev] [--browsers LIST] [--no-workspace]
        Set up the bridge for this user and register the current repo as a workspace:
        makes the bridge venv (~/.config/biz-connect/browser/.venv, ~250 MB, once), writes the
        native-messaging host, registers it with Chrome, Brave, Edge and Chromium, and puts the
        extension where the browser can load it. Re-run after a plugin update. --dev runs the
        host and the extension from this clone (for working on biz-connect itself).
  status                 what is installed, registered and configured
  add [PATH] [--name N] [--default]   register another workspace (default: this repo)
  remove NAME            unregister a workspace
  default NAME           the workspace the panel opens on
  menu [PATH]            check a workspace's `browser:` settings and right-click menu
  uninstall              remove the host registrations (the extension you remove in the browser)

Per-workspace settings live in that repo's connections.yaml:

  browser:
    permission_mode: auto        # auto (default) | default | acceptEdits | plan
    model:                       # blank = your Claude Code default
    deny: []                     # extra tools browser runs may never use (fnmatch patterns)
    menu:                        # right-click items; each runs a skill (or a prompt) on the selection
      - label: Tighten this paragraph
        skill: my-style-skill
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config
from ..browser_host import (EXTENSION_ID, HOST_NAME, load_bridge_config, save_bridge_config,
                            store_dir, workspace_settings)

ROOT = Path(__file__).resolve().parents[2]              # the plugin folder
BROWSERS = ("chrome", "brave", "edge", "chromium")
WIN_KEYS = {"chrome": r"Software\Google\Chrome\NativeMessagingHosts",
            "brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
            "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
            "chromium": r"Software\Chromium\NativeMessagingHosts"}
MAC_DIRS = {"chrome": "Google/Chrome", "brave": "BraveSoftware/Brave-Browser",
            "edge": "Microsoft Edge", "chromium": "Chromium"}
LINUX_DIRS = {"chrome": "google-chrome", "brave": "BraveSoftware/Brave-Browser",
              "edge": "microsoft-edge", "chromium": "chromium"}
EXT_PAGES = {"chrome": "chrome://extensions", "brave": "brave://extensions",
             "edge": "edge://extensions", "chromium": "chrome://extensions"}


# --------------------------------------------------------------------------- paths
def _venv_python(store: Path) -> Path:
    v = store / ".venv"
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _host_script(store: Path) -> Path:
    return store / ("host.cmd" if os.name == "nt" else "host.sh")


def _manifest_path(store: Path) -> Path:
    return store / f"{HOST_NAME}.json"


def host_manifest(script: Path) -> dict:
    return {"name": HOST_NAME,
            "description": "biz-connect browser bridge (Claude Code in your workspace)",
            "path": str(script), "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}


def _posix_manifest_dir(browser: str) -> Path | None:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / MAC_DIRS[browser]
    else:
        base = Path.home() / ".config" / LINUX_DIRS[browser]
    return base / "NativeMessagingHosts"


# ---------------------------------------------------------------------- registry
def _register(browser: str, manifest: Path) -> str:
    if os.name == "nt":
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_KEYS[browser] + "\\" + HOST_NAME)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
        winreg.CloseKey(key)
        return "HKCU\\" + WIN_KEYS[browser] + "\\" + HOST_NAME
    d = _posix_manifest_dir(browser)
    if not d.parent.exists():
        return ""                                       # browser not installed: skip
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{HOST_NAME}.json"
    shutil.copyfile(manifest, dst)
    return str(dst)


def _registered(browser: str) -> str | None:
    """Where the browser would find the host manifest, or None."""
    if os.name == "nt":
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_KEYS[browser] + "\\" + HOST_NAME)
            val, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            return val
        except OSError:
            return None
    p = _posix_manifest_dir(browser) / f"{HOST_NAME}.json"
    return str(p) if p.exists() else None


def _unregister(browser: str):
    if os.name == "nt":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, WIN_KEYS[browser] + "\\" + HOST_NAME)
        except OSError:
            pass
        return
    p = _posix_manifest_dir(browser) / f"{HOST_NAME}.json"
    if p.exists():
        p.unlink()


# --------------------------------------------------------------------- workspaces
def _repo_root(start=None) -> Path | None:
    conn = config.find_connections(start)
    if conn:
        return conn.parent.resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=15, cwd=str(start) if start else None)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def add_workspace(cfg: dict, path: Path, name: str | None = None, make_default=False) -> str:
    ws = cfg.setdefault("workspaces", {})
    for k, v in ws.items():                             # already registered: keep its name
        if Path(v).resolve() == path.resolve():
            name = k
            break
    else:
        base = name or path.name
        name, n = base, 2
        while name in ws:
            name, n = f"{base}-{n}", n + 1
        ws[name] = str(path.resolve())
    if make_default or not cfg.get("default_workspace") or cfg["default_workspace"] not in ws:
        cfg["default_workspace"] = name
    return name


# ------------------------------------------------------------------------- venv
def _ensure_venv(store: Path) -> Path:
    py = _venv_python(store)
    req = ROOT / "browser" / "requirements.txt"
    want = hashlib.sha256(req.read_bytes()).hexdigest()
    marker = store / ".venv" / ".requirements.sha256"
    if not py.exists():
        print(f"creating the bridge venv at {store / '.venv'} ...")
        base = getattr(sys, "_base_executable", None) or sys.executable
        if subprocess.run([base, "-m", "venv", str(store / ".venv")]).returncode != 0 or not py.exists():
            sys.exit("could not create the bridge venv (is Python's venv module installed?)")
    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    if have != want:
        print("installing the bridge's packages (claude-agent-sdk is ~250 MB; this happens once) ...")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        if subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)]).returncode != 0:
            sys.exit("installing the bridge's packages failed (see pip's output above)")
        marker.write_text(want, encoding="utf-8")
    return py


def _write_host_script(store: Path, py: Path) -> Path:
    script = _host_script(store)
    launch = store / "launch.py"
    body = (f'@echo off\r\n"{py}" -u "{launch}" %*\r\n' if os.name == "nt"
            else f'#!/bin/sh\nexec "{py}" -u "{launch}" "$@"\n')
    with open(script, "w", encoding="utf-8", newline="") as fh:     # no newline translation
        fh.write(body)
    if os.name != "nt":
        script.chmod(0o755)
    return script


# ------------------------------------------------------------------------- verbs
def cmd_install(args):
    p = argparse.ArgumentParser(prog="bizconnect browser install")
    p.add_argument("--dev", action="store_true",
                   help="run the host and the extension from this clone (biz-connect development)")
    p.add_argument("--browsers", default=",".join(BROWSERS),
                   help="comma-separated: " + ", ".join(BROWSERS))
    p.add_argument("--no-workspace", action="store_true", help="don't register the current repo")
    ns = p.parse_args(args)
    browsers = [b.strip().lower() for b in ns.browsers.split(",") if b.strip()]
    bad = [b for b in browsers if b not in BROWSERS]
    if bad:
        sys.exit(f"unknown browser(s): {', '.join(bad)} (one of {', '.join(BROWSERS)})")

    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    py = _ensure_venv(store)
    shutil.copyfile(ROOT / "browser" / "launch.py", store / "launch.py")
    script = _write_host_script(store, py)
    manifest = _manifest_path(store)
    manifest.write_text(json.dumps(host_manifest(script), indent=2) + "\n", encoding="utf-8")

    print("native-messaging host registered for:")
    for b in browsers:
        where = _register(b, manifest)
        print(f"  {b.ljust(9)} {where or '(not installed here; skipped)'}")

    src = ROOT / "browser" / "extension"
    if ns.dev:
        ext_dir = src
    else:
        ext_dir = store / "extension"
        if ext_dir.exists():
            shutil.rmtree(ext_dir)
        shutil.copytree(src, ext_dir)

    cfg = load_bridge_config()
    cfg.update({"plugin_root": str(ROOT), "pin_plugin_root": bool(ns.dev),
                "extension_dir": str(ext_dir), "claude_path": shutil.which("claude") or ""})
    if not ns.no_workspace:
        root = _repo_root()
        if root:
            name = add_workspace(cfg, root)
            print(f"workspace: {name} -> {root}")
        elif not cfg.get("workspaces"):
            print("note: not inside a repo, so no workspace was registered. In your repo, run "
                  "`bizconnect browser add`.")
    save_bridge_config(cfg)
    if not cfg["claude_path"]:
        print("WARNING: `claude` is not on PATH; the bridge will use the copy bundled with the SDK, "
              "which has its own login. Install Claude Code, then re-run this.")

    pages = sorted({EXT_PAGES[b] for b in browsers})
    print(f"""
Next, once per browser profile, load the extension:
  1. open {' or '.join(pages)}
  2. turn on Developer mode
  3. Load unpacked -> {ext_dir}
     (extension id {EXTENSION_ID}; after a biz-connect update, re-run this command, then press
     the extension's reload button)
Then select text on any page and right-click -> "Ask Claude", or press Alt+Shift+A.""")
    return 0


def cmd_status(args):
    store = store_dir()
    cfg = load_bridge_config()
    print(f"bridge store: {store}")
    py = _venv_python(store)
    if py.exists():
        r = subprocess.run([str(py), "-c", "import claude_agent_sdk as s; print(s.__version__)"],
                           capture_output=True, text=True)
        print(f"  venv:          {'claude-agent-sdk ' + r.stdout.strip() if r.returncode == 0 else 'BROKEN: ' + r.stderr.strip()[-200:]}")
    else:
        print("  venv:          missing (run `bizconnect browser install`)")
    print(f"  host script:   {_host_script(store) if _host_script(store).exists() else 'missing'}")
    print(f"  plugin root:   {cfg.get('plugin_root') or '?'}{'  (pinned: dev clone)' if cfg.get('pin_plugin_root') else ''}")
    print(f"  claude:        {cfg.get('claude_path') or shutil.which('claude') or 'not on PATH (SDK bundled copy)'}")
    print(f"  extension:     {cfg.get('extension_dir') or '?'}  (id {EXTENSION_ID})")
    print("registered with:")
    want = str(_manifest_path(store))
    for b in BROWSERS:
        where = _registered(b)
        state = "no" if not where else ("yes" if os.name != "nt" or Path(where) == Path(want)
                                        else f"points elsewhere: {where}")
        print(f"  {b.ljust(9)} {state}")
    ws = cfg.get("workspaces") or {}
    print("workspaces:" + ("" if ws else " none (run `bizconnect browser add` in a repo)"))
    for k, v in ws.items():
        mark = "*" if k == cfg.get("default_workspace") else " "
        ok = "" if Path(v).is_dir() else "  (MISSING)"
        st = workspace_settings(v) if Path(v).is_dir() else None
        extra = (f"  mode={st['permission_mode']} menu={len(st['menu'])}"
                 + (f"  {len(st['problems'])} problem(s): run `bizconnect browser menu {v}`" if st["problems"] else "")
                 ) if st else ""
        print(f" {mark} {k}: {v}{ok}{extra}")
    from ..browser_host import _plugin_enabled
    if not _plugin_enabled():
        print("note: biz-connect is not enabled in ~/.claude/settings.json; browser runs load it "
              "anyway (browser.load_plugin), but your other sessions won't have it.")
    return 0


def cmd_add(args):
    p = argparse.ArgumentParser(prog="bizconnect browser add")
    p.add_argument("path", nargs="?")
    p.add_argument("--name")
    p.add_argument("--default", action="store_true")
    ns = p.parse_args(args)
    root = Path(ns.path).resolve() if ns.path else _repo_root()
    if not root or not root.is_dir():
        sys.exit("not a folder / not inside a repo; pass the workspace path")
    cfg = load_bridge_config()
    name = add_workspace(cfg, root, ns.name, ns.default)
    save_bridge_config(cfg)
    print(f"workspace {name} -> {root}" + ("  (default)" if cfg["default_workspace"] == name else ""))
    return 0


def cmd_remove(args):
    if not args:
        sys.exit("usage: bizconnect browser remove NAME")
    cfg = load_bridge_config()
    if (cfg.get("workspaces") or {}).pop(args[0], None) is None:
        sys.exit(f"no workspace {args[0]!r}")
    if cfg.get("default_workspace") == args[0]:
        cfg["default_workspace"] = next(iter(cfg["workspaces"]), None)
    save_bridge_config(cfg)
    print(f"removed {args[0]}")
    return 0


def cmd_default(args):
    cfg = load_bridge_config()
    if not args or args[0] not in (cfg.get("workspaces") or {}):
        sys.exit("usage: bizconnect browser default NAME (one of: %s)"
                 % ", ".join(cfg.get("workspaces") or {}))
    cfg["default_workspace"] = args[0]
    save_bridge_config(cfg)
    print(f"default workspace: {args[0]}")
    return 0


def cmd_menu(args):
    root = Path(args[0]).resolve() if args else _repo_root()
    if not root:
        sys.exit("not inside a repo; pass the workspace path")
    st = workspace_settings(root)
    print(f"workspace: {root}")
    print(f"  permission_mode: {st['permission_mode']}   model: {st['model'] or '(default)'}   "
          f"max_turns: {st['max_turns']}")
    if st["deny"]:
        print(f"  deny: {', '.join(st['deny'])}")
    print("  right-click menu: Ask Claude..." + ("" if st["menu"] else "  (no items in connections.yaml browser.menu)"))
    for m in st["menu"]:
        what = f"/{m['skill']}" if m["skill"] else f"prompt: {m['prompt'][:60]}"
        print(f"    - {m['label']}  ->  {what}" + (f"  [sites: {', '.join(m['sites'])}]" if m["sites"] else ""))
    for prob in st["problems"]:
        print(f"  PROBLEM: {prob}")
    return 1 if st["problems"] else 0


def cmd_uninstall(args):
    store = store_dir()
    for b in BROWSERS:
        _unregister(b)
    for f in (_manifest_path(store), _host_script(store), store / "launch.py"):
        if f.exists():
            f.unlink()
    print("host registrations removed. Remove the extension in your browser's extensions page. "
          f"The venv and settings stay in {store}; delete that folder to remove everything.")
    return 0


VERBS = {"install": cmd_install, "status": cmd_status, "add": cmd_add, "remove": cmd_remove,
         "default": cmd_default, "menu": cmd_menu, "uninstall": cmd_uninstall}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown browser verb {verb!r}. One of: {', '.join(sorted(VERBS))}")
    return fn(rest) or 0
