#!/usr/bin/env python3
"""biz-connect launcher / bootstrap.

This is the single entrypoint the plugin skills (and humans) call. Normally via the
`bizconnect` command shim in bin/ (Claude Code puts a plugin's bin/ on PATH):

    bizconnect <service> <verb> [args]

or directly, when the shim isn't on PATH:

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> [args]
    (on Windows: python "..." or py -3 "...")

It is dependency-free and self-bootstrapping so the plugin works for any user with
just a Python 3.9+ on PATH:

  1. ensures a venv exists in the central store (~/.config/biz-connect/.venv),
     creating it and pip-installing requirements.txt the first time (and whenever
     requirements change). A half-created venv (python present but no pip, e.g.
     Debian/Ubuntu without the python3-venv package) is rebuilt once, then reported
     with a one-line fix instead of a traceback; so is a venv whose base Python was
     removed or upgraded away (its python no longer starts);
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
MIN_PY = (3, 9)


def _home() -> Path:
    env = os.environ.get("BIZCONNECT_HOME")
    return (Path(env) if env else Path.home() / ".config" / "biz-connect").expanduser()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _say(msg):
    print("[biz-connect] " + msg, file=sys.stderr)


def _run(cmd, quiet=False) -> int:
    """Run a command; return its exit code (127 if it can't be started at all)."""
    try:
        out = subprocess.DEVNULL if quiet else None
        return subprocess.run(cmd, stdout=out, stderr=out).returncode
    except OSError:
        return 127


def _make_venv(venv: Path, clear=False) -> int:
    venv.parent.mkdir(parents=True, exist_ok=True)
    return _run([sys.executable, "-m", "venv", *(["--clear"] if clear else []), str(venv)])


def _pip_ok(py: Path) -> bool:
    return py.exists() and _run([str(py), "-m", "pip", "--version"], quiet=True) == 0


def _py_runs(py: Path) -> bool:
    """False when the venv's Python can't start: its base Python was removed or upgraded away
    (Windows: "No Python at ..." from pyvenv.cfg's home; POSIX: a dangling bin/python link)."""
    return py.exists() and _run([str(py), "-c", "import sys"], quiet=True) == 0


def _venv_fix_hint() -> str:
    if sys.platform.startswith("linux"):
        v = "%d.%d" % sys.version_info[:2]
        return ("install python3-venv (sudo apt install python3-venv, or python%s-venv) and re-run" % v)
    if sys.platform == "darwin":
        return "install a full Python 3 (python.org or `brew install python`) and re-run"
    return "install a full Python 3 from https://www.python.org/downloads/ and re-run"


def _die(msg):
    _say(msg)
    sys.exit(1)


def _ensure_venv(probe=True) -> Path:
    """`probe=False` when already running on the venv's Python (it evidently starts)."""
    venv = _home() / ".venv"
    py = _venv_python(venv)
    req = PLUGIN_ROOT / "requirements.txt"
    want = hashlib.sha256(req.read_bytes()).hexdigest() if req.exists() else ""
    marker = venv / ".requirements.sha256"

    fresh = False
    if not os.path.lexists(str(py)):    # a dangling bin/python symlink is handled by the probe
        _say("creating venv at %s ..." % venv)
        _make_venv(venv)      # exit code checked via _pip_ok below (a failed create can leave bin/python)
        fresh = True
        if not py.exists():
            _die("could not create a venv at %s: %s" % (venv, _venv_fix_hint()))

    if probe and not _py_runs(py):
        if not fresh:
            _say("the venv's base Python is gone — rebuilding %s ..." % venv)
            _make_venv(venv, clear=True)
            if marker.exists():
                marker.unlink()           # a cleared venv has no packages: reinstall them
        if not _py_runs(py):
            _die("the venv at %s can't start its Python: %s" % (venv, _venv_fix_hint()))

    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    if have != want and req.exists():
        # The marker is written only after a successful install, so a missing/stale marker is
        # also where a half-created venv (python but no pip) shows up. Verify pip before use.
        if not _pip_ok(py):
            if not fresh:
                _say("the venv at %s has no working pip — rebuilding it ..." % venv)
                _make_venv(venv, clear=True)
            if not _pip_ok(py):
                _die("the venv at %s has no pip (Python's venv/ensurepip is missing): %s"
                     % (venv, _venv_fix_hint()))
        _say("installing dependencies ...")
        _run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"])   # best effort
        if _run([str(py), "-m", "pip", "install", "-q", "-r", str(req)]) != 0:
            _die("installing dependencies failed (see pip's output above) — check your network/proxy "
                 "and re-run; to start over, delete %s" % venv)
        marker.write_text(want, encoding="utf-8")
    return py


def main():
    if sys.version_info < MIN_PY:
        _die("needs Python %d.%d+ (this is %s) — install a newer Python 3 and re-run."
             % (MIN_PY + (sys.version.split()[0],)))
    argv = sys.argv[1:]
    venv = _home() / ".venv"
    # Are we already running INSIDE the target venv? Compare sys.prefix (the venv dir
    # when active), NOT the python binary — on POSIX the venv python is a SYMLINK to the
    # base interpreter, so resolving the binary makes both sides equal and we'd never
    # actually enter the venv (deps would appear missing).
    in_venv = Path(sys.prefix).resolve() == venv.resolve()
    inside = in_venv or os.environ.get("BIZCONNECT_BOOTSTRAPPED") == "1"
    py = _ensure_venv(probe=not inside)
    if not inside:
        env = dict(os.environ, BIZCONNECT_BOOTSTRAPPED="1")
        proc = subprocess.run([str(py), str(Path(__file__).resolve()), *argv], env=env)
        sys.exit(proc.returncode)
    sys.path.insert(0, str(PLUGIN_ROOT))
    from bizconnect.cli import main as cli_main
    sys.exit(cli_main(argv))


if __name__ == "__main__":
    main()
