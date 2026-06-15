#!/usr/bin/env python
"""biz-connect launcher / bootstrap.

This is the single entrypoint the plugin skills (and humans) call:

    python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> [args]

It is dependency-free and self-bootstrapping so the plugin works for any user with
just a Python on PATH:

  1. ensures a venv exists in the central store (~/.config/biz-connect/.venv),
     creating it and pip-installing requirements.txt the first time (and whenever
     requirements change);
  2. re-executes itself under that venv's Python;
  3. puts the plugin root on sys.path and dispatches to bizconnect.cli.

The venv lives in the CENTRAL STORE, not the plugin dir, so it survives plugin
updates (the plugin is re-copied to a versioned cache on every update).
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _home() -> Path:
    env = os.environ.get("BIZCONNECT_HOME")
    return (Path(env) if env else Path.home() / ".config" / "biz-connect").expanduser()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_venv() -> Path:
    venv = _home() / ".venv"
    py = _venv_python(venv)
    req = PLUGIN_ROOT / "requirements.txt"
    want = hashlib.sha256(req.read_bytes()).hexdigest() if req.exists() else ""
    marker = venv / ".requirements.sha256"

    if not py.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        print(f"[biz-connect] creating venv at {venv} ...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])

    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    if have != want and req.exists():
        print("[biz-connect] installing dependencies ...", file=sys.stderr)
        subprocess.check_call([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        subprocess.check_call([str(py), "-m", "pip", "install", "-q", "-r", str(req)])
        marker.write_text(want, encoding="utf-8")
    return py


def main():
    argv = sys.argv[1:]
    py = _ensure_venv()
    venv = _home() / ".venv"
    # Are we already running INSIDE the target venv? Compare sys.prefix (the venv dir
    # when active), NOT the python binary — on POSIX the venv python is a SYMLINK to the
    # base interpreter, so resolving the binary makes both sides equal and we'd never
    # actually enter the venv (deps would appear missing).
    in_venv = Path(sys.prefix).resolve() == venv.resolve()
    if not in_venv and os.environ.get("BIZCONNECT_BOOTSTRAPPED") != "1":
        env = dict(os.environ, BIZCONNECT_BOOTSTRAPPED="1")
        proc = subprocess.run([str(py), str(Path(__file__).resolve()), *argv], env=env)
        sys.exit(proc.returncode)
    sys.path.insert(0, str(PLUGIN_ROOT))
    from bizconnect.cli import main as cli_main
    sys.exit(cli_main(argv))


if __name__ == "__main__":
    main()
