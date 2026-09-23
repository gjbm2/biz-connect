"""Tests of the top-level CLI (doctor / init / version), the secrets loader, the launcher's
venv bootstrap and the `bizconnect` command shims. Nothing here calls the live Notion API:
bizconnect.connectors.notion.api is replaced by a stub."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import bizconnect
from bizconnect import cli, config, update
from bizconnect.connectors import notion as N

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_VERSION = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
ENV_KEYS = ("NOTION_TOKEN", "NOTION_VERSION", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_IMPERSONATE_SUBJECT")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway central store + repo; env vars the loader may set are restored afterwards."""
    home = tmp_path / "store"
    home.mkdir()
    monkeypatch.setenv("BIZCONNECT_HOME", str(home))
    for k in ENV_KEYS:
        monkeypatch.setenv(k, "")       # recorded first, so teardown undoes load_secrets' writes
        monkeypatch.delenv(k)
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(update, "check", lambda force=False: {"installed": PLUGIN_VERSION,
                                                              "latest": PLUGIN_VERSION})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "connections.yaml").write_text("notion: {}\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    return home, repo


def stub_api(monkeypatch, status, body):
    calls = []

    def api(method, path, *args, **kwargs):
        calls.append((method, path))
        return status, body
    monkeypatch.setattr(N, "api", api)
    return calls


# ---------------------------------------------------------------------- doctor
def test_doctor_notion_only_user_is_ok(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    calls = stub_api(monkeypatch, 200, {"object": "user", "type": "bot", "name": "Test Bot",
                                        "bot": {"workspace_name": "Acme"}})
    rc = cli.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert calls == [("GET", "/users/me")]
    assert "'Test Bot'" in out and "'Acme'" in out
    assert "Google connectors unavailable" in out          # a warning, not a failure
    assert "FAIL" not in out
    assert out.rstrip().splitlines()[-1].startswith("OK")


def test_doctor_rejected_token_fails(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    stub_api(monkeypatch, 401, {"object": "error", "status": 401, "code": "unauthorized",
                                "message": "API token is invalid."})
    rc = cli.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "FAIL" in out and "API token is invalid." in out


def test_doctor_forbidden_token_fails(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    stub_api(monkeypatch, 403, {"message": "restricted"})
    assert cli.cmd_doctor() == 1


def test_doctor_unreachable_notion_is_a_warning(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    stub_api(monkeypatch, 599, {"message": "network error: offline"})
    rc = cli.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "could not verify" in out
    last = out.rstrip().splitlines()[-1]                     # the summary names the real problem
    assert "(Notion could not be verified — check your network and re-run; " \
           "Google needs a service account)." in last
    assert "needs NOTION_TOKEN" not in out


def test_doctor_unreachable_notion_with_google_missing_only_mentions_what_is_missing(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    (home / "service-account.json").write_text('{"client_email": "sa@x.iam"}', encoding="utf-8")
    stub_api(monkeypatch, 599, {"message": "network error: offline"})
    assert cli.cmd_doctor() == 0
    last = capsys.readouterr().out.rstrip().splitlines()[-1]
    assert last.startswith("OK")                             # Google is ready: nothing to explain


def test_doctor_nothing_configured_is_not_broken(store, monkeypatch, capsys):
    calls = stub_api(monkeypatch, 500, {})
    rc = cli.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert calls == []                                      # no token -> no API call
    assert "Notion connectors unavailable" in out
    assert "Google connectors unavailable" in out
    assert "FAIL" not in out
    assert out.rstrip().splitlines()[-1].endswith(
        "(Notion needs NOTION_TOKEN; Google needs a service account).")


def test_doctor_unreadable_service_account_fails(store, monkeypatch, capsys):
    home, _repo = store
    (home / "service-account.json").write_text("{not json", encoding="utf-8")
    assert cli.cmd_doctor() == 1
    assert "unreadable" in capsys.readouterr().out


def test_doctor_lists_notion_manifests(store, monkeypatch, capsys):
    home, repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\n", encoding="utf-8")
    stub_api(monkeypatch, 200, {"name": "Test Bot"})
    (repo / "projects" / "x").mkdir(parents=True)
    (repo / "projects" / "x" / "notion.yaml").write_text("hub: null\nmap: {}\n", encoding="utf-8")
    assert cli.cmd_doctor() == 0
    assert "projects/x/notion.yaml" in capsys.readouterr().out


def test_doctor_warns_on_notion_version_override(store, monkeypatch, capsys):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=secret_dummy\nNOTION_VERSION=2025-09-03\n",
                                      encoding="utf-8")
    stub_api(monkeypatch, 200, {"name": "Test Bot"})
    assert cli.cmd_doctor() == 0
    assert "NOTION_VERSION" in capsys.readouterr().out


# --------------------------------------------------------------------- secrets
def test_secrets_env_with_bom(store):
    home, _repo = store
    (home / "secrets.env").write_bytes(b"\xef\xbb\xbfNOTION_TOKEN=secret_bom\r\nOTHER=1\r\n")
    assert config.secret("NOTION_TOKEN") == "secret_bom"


def test_blank_secret_does_not_clobber_environment(store, monkeypatch):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=\n", encoding="utf-8")
    monkeypatch.setenv("NOTION_TOKEN", "from_env")
    assert config.secret("NOTION_TOKEN") == "from_env"


def test_secrets_env_still_overrides_a_stale_export(store, monkeypatch):
    home, _repo = store
    (home / "secrets.env").write_text("NOTION_TOKEN=rotated\n", encoding="utf-8")
    monkeypatch.setenv("NOTION_TOKEN", "stale")
    assert config.secret("NOTION_TOKEN") == "rotated"


# ------------------------------------------------------------------------ init
def test_init_writes_notion_friendly_template(store, tmp_path, monkeypatch, capsys):
    home, _repo = store
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    assert cli.cmd_init() == 0
    text = (home / "secrets.env").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "https://www.notion.so/profile/integrations" in text
    assert not any(line.strip().startswith("NOTION_VERSION") for line in lines)   # never set by default
    assert any(line.startswith("#") and "NOTION_VERSION" in line for line in lines)
    assert "NOTION_TOKEN=" in lines
    assert (fresh / "connections.yaml").exists()
    assert ".bizconnect/" in (fresh / ".gitignore").read_text(encoding="utf-8")
    assert b"\r\n" not in (fresh / "connections.yaml").read_bytes()


def test_init_refuses_connections_yaml_in_home_folder(store, tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(fake_home)
    assert cli.cmd_init() == 0
    assert not (fake_home / "connections.yaml").exists()
    assert "home folder" in capsys.readouterr().out


def _fake_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


@pytest.mark.parametrize("in_git", [True, False])
def test_init_sidesteps_a_stray_connections_yaml_in_home(store, tmp_path, monkeypatch, capsys, in_git):
    """A connections.yaml left in the home folder would make home the repo root: init warns,
    leaves home's .gitignore alone and sets up this repo (its git top-level, else cwd)."""
    fake_home = _fake_home(tmp_path, monkeypatch)
    stray = fake_home / "connections.yaml"
    stray.write_text("notion: {}\n", encoding="utf-8")
    repo = fake_home / "dev" / "proj"
    sub = repo / "notes"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub if in_git else repo)
    monkeypatch.setattr(cli, "_git_toplevel", lambda: repo.resolve() if in_git else None)
    assert cli.cmd_init() == 0
    out = capsys.readouterr().out
    assert "a stray connections.yaml at %s makes it the repo root for every folder below; " \
           "move or delete it" % stray in out
    assert (repo / "connections.yaml").exists() and not (sub / "connections.yaml").exists()
    assert ".bizconnect/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert not (fake_home / ".gitignore").exists()
    assert stray.read_text(encoding="utf-8") == "notion: {}\n"


def test_init_sidesteps_a_connections_yaml_above_the_git_repo(store, tmp_path, monkeypatch, capsys):
    _fake_home(tmp_path, monkeypatch)
    outer = tmp_path / "work"
    repo = outer / "repo"
    repo.mkdir(parents=True)
    (outer / "connections.yaml").write_text("notion: {}\n", encoding="utf-8")
    (outer / ".gitignore").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_git_toplevel", lambda: repo.resolve())
    assert cli.cmd_init() == 0
    assert "above this git repo" in capsys.readouterr().out
    assert (repo / "connections.yaml").exists()
    assert (outer / ".gitignore").read_text(encoding="utf-8") == "x\n"


def test_init_keeps_the_repos_own_connections_yaml(store, tmp_path, monkeypatch, capsys):
    """A connections.yaml at the git top-level, run from a subfolder: left as-is, guards added."""
    _fake_home(tmp_path, monkeypatch)
    _home, repo = store
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    monkeypatch.setattr(cli, "_git_toplevel", lambda: repo.resolve())
    assert cli.cmd_init() == 0
    assert "already present" in capsys.readouterr().out
    assert not (repo / "sub" / "connections.yaml").exists()
    assert ".bizconnect/" in (repo / ".gitignore").read_text(encoding="utf-8")


# --------------------------------------------------------------------- version
def test_version_matches_plugin_json(capsys):
    assert bizconnect.__version__ == PLUGIN_VERSION
    assert update.installed_version() == PLUGIN_VERSION
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "biz-connect %s" % PLUGIN_VERSION


# -------------------------------------------------------------------- launcher
def _load_launcher():
    spec = importlib.util.spec_from_file_location("bizconnect_launcher", ROOT / "scripts" / "bizconnect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _half_venv(home):
    venv = home / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")                    # python present, pip missing, no marker
    return venv, py


def test_launcher_rebuilds_half_created_venv(tmp_path, monkeypatch):
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    venv, _py = _half_venv(tmp_path)
    made, pip_state, ran = [], iter([False, True]), []
    monkeypatch.setattr(L, "_make_venv", lambda v, clear=False: made.append(clear) or 0)
    monkeypatch.setattr(L, "_pip_ok", lambda py: next(pip_state))
    monkeypatch.setattr(L, "_run", lambda cmd, quiet=False: ran.append(cmd) or 0)
    L._ensure_venv()
    assert made == [True]                                    # rebuilt once with --clear
    assert any("-r" in cmd for cmd in ran)                   # then installed requirements
    want = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    assert (venv / ".requirements.sha256").read_text(encoding="utf-8") == want


def test_launcher_gives_a_hint_when_pip_cannot_be_made(tmp_path, monkeypatch, capsys):
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    _half_venv(tmp_path)
    monkeypatch.setattr(L, "_make_venv", lambda v, clear=False: 1)
    monkeypatch.setattr(L, "_py_runs", lambda py: True)
    monkeypatch.setattr(L, "_pip_ok", lambda py: False)
    monkeypatch.setattr(L, "_run", lambda cmd, quiet=False: pytest.fail("must not pip install"))
    with pytest.raises(SystemExit) as ei:
        L._ensure_venv()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "re-run" in err and "Traceback" not in err
    if sys.platform.startswith("linux"):
        assert "sudo apt install python3-venv" in err


def _ready_venv(home):
    """A venv whose requirements marker is current: only the probe can find it broken."""
    venv, py = _half_venv(home)
    want = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    (venv / ".requirements.sha256").write_text(want, encoding="utf-8")
    return venv, py, want


def test_launcher_rebuilds_a_venv_whose_base_python_is_gone(tmp_path, monkeypatch, capsys):
    """Windows: the venv's python.exe answers "No Python at ..." (rc 103) once its base
    Python is uninstalled/upgraded. Rebuild with --clear and reinstall the requirements."""
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    venv, _py, want = _ready_venv(tmp_path)
    made, runs, ran = [], iter([False, True]), []
    monkeypatch.setattr(L, "_make_venv", lambda v, clear=False: made.append(clear) or 0)
    monkeypatch.setattr(L, "_py_runs", lambda py: next(runs))
    monkeypatch.setattr(L, "_pip_ok", lambda py: True)
    monkeypatch.setattr(L, "_run", lambda cmd, quiet=False: ran.append(cmd) or 0)
    L._ensure_venv()
    assert made == [True]
    assert "base Python is gone" in capsys.readouterr().err
    assert any("-r" in cmd for cmd in ran)                   # the marker was dropped: deps reinstalled
    assert (venv / ".requirements.sha256").read_text(encoding="utf-8") == want


def test_launcher_dies_with_a_hint_if_the_rebuilt_venv_still_cannot_start(tmp_path, monkeypatch, capsys):
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    _ready_venv(tmp_path)
    made = []
    monkeypatch.setattr(L, "_make_venv", lambda v, clear=False: made.append(clear) or 0)
    monkeypatch.setattr(L, "_py_runs", lambda py: False)
    monkeypatch.setattr(L, "_run", lambda cmd, quiet=False: pytest.fail("must not pip install"))
    with pytest.raises(SystemExit) as ei:
        L._ensure_venv()
    assert ei.value.code == 1 and made == [True]
    err = capsys.readouterr().err
    assert "can't start its Python" in err and "re-run" in err and "Traceback" not in err


def test_launcher_probe_catches_a_real_broken_python(tmp_path):
    """The real probe: an empty python file can't start; a dangling symlink doesn't exist."""
    L = _load_launcher()
    _venv, py, _want = _ready_venv(tmp_path)
    assert not L._py_runs(py)
    assert L._py_runs(Path(sys.executable))
    link = tmp_path / "dangling-python"
    try:
        os.symlink(str(tmp_path / "gone" / "python3"), str(link))
    except (OSError, NotImplementedError):
        return                                               # Windows without symlink rights
    assert os.path.lexists(str(link)) and not L._py_runs(link)


def test_launcher_rebuilds_over_a_dangling_python_symlink(tmp_path, monkeypatch, capsys):
    """POSIX: bin/python -> a removed base Python. It must be --clear'ed (a plain `venv`
    run keeps the dangling link), not reported as "could not create a venv"."""
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    venv = tmp_path / ".venv"
    py = L._venv_python(venv)
    py.parent.mkdir(parents=True)
    try:
        os.symlink(str(tmp_path / "gone" / "python3"), str(py))
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights")
    made = []

    def make(v, clear=False):
        made.append(clear)
        if clear:
            py.unlink()
            py.write_bytes(b"")
        return 0
    monkeypatch.setattr(L, "_make_venv", make)
    monkeypatch.setattr(L, "_run", lambda cmd, quiet=False: 0)
    L._ensure_venv()
    assert made == [True]
    assert "base Python is gone" in capsys.readouterr().err


def test_launcher_skips_the_probe_when_already_inside(tmp_path, monkeypatch):
    L = _load_launcher()
    monkeypatch.setenv("BIZCONNECT_HOME", str(tmp_path))
    _ready_venv(tmp_path)
    monkeypatch.setattr(L, "_py_runs", lambda py: pytest.fail("probed from inside the venv"))
    L._ensure_venv(probe=False)


# ----------------------------------------------------------------------- shims
def _shim_env(tmp_path):
    """Env where the launcher finds a ready (fake) venv and runs the CLI in-process."""
    home = tmp_path / "shimstore"
    venv = home / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    want = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    (venv / ".requirements.sha256").write_text(want, encoding="utf-8")
    return dict(os.environ, BIZCONNECT_HOME=str(home), BIZCONNECT_BOOTSTRAPPED="1",
                BIZCONNECT_UPDATE_CHECK="off")


def test_posix_shim_runs_version(tmp_path):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no sh on PATH")
    r = subprocess.run([sh, (ROOT / "bin" / "bizconnect").as_posix(), "version"], cwd=str(tmp_path),
                       env=_shim_env(tmp_path), capture_output=True, text=True, timeout=120)
    if r.returncode == 127 and "no Python 3.9+ found" in r.stderr:
        pytest.skip("no python3/python/py on PATH for the shim")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "biz-connect %s" % PLUGIN_VERSION


def test_posix_shim_honours_bizconnect_python(tmp_path):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no sh on PATH")
    env = dict(_shim_env(tmp_path), BIZCONNECT_PYTHON=sys.executable)
    r = subprocess.run([sh, (ROOT / "bin" / "bizconnect").as_posix(), "bogus-command"],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stderr                       # exit code passes through
    assert "unknown command" in r.stderr


def test_posix_shim_is_committed_executable():
    """Claude Code runs bin/bizconnect directly on macOS/Linux: without the git exec bit a
    fresh plugin install gets `Permission denied`. (release.sh refuses to release without it.)"""
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run([git, "ls-files", "-s", "bin/bizconnect"], cwd=str(ROOT), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("bin/bizconnect is not tracked yet (git add --chmod=+x bin/bizconnect)")
    assert r.stdout.startswith("100755"), r.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd shim")
def test_cmd_shim_runs_version(tmp_path):
    r = subprocess.run(["cmd", "/c", str(ROOT / "bin" / "bizconnect.cmd"), "version"], cwd=str(tmp_path),
                       env=_shim_env(tmp_path), capture_output=True, text=True, timeout=120)
    if r.returncode == 9009:
        pytest.skip("no py/python on PATH for the shim")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "biz-connect %s" % PLUGIN_VERSION
