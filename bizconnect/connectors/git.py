"""git — standardised, safe git flow for a project repo.

House rules, applied consistently across every repo that uses biz-connect:
  * never commit straight to a protected branch (main/master) or a detached HEAD —
    branch first (into a unique wip/<slug> branch);
  * refuse to commit obvious secret files;
  * optional Co-Authored-By trailer (--co-author, or git.co_author in connections.yaml);
  * sync = rebase-pull with autostash, then push (handles first-push/no-upstream).

Verbs
-----
  status                      branch + ahead/behind + short status
  save "<msg>" [--co-author "Name <email>"] [--allow-main] [--allow-secrets] [--push]
                              stage all, commit (branching off a protected/detached HEAD first)
  sync                        pull --rebase --autostash (if upstream), then push
  pr [--title T] [--body B]   push the branch and open a PR via the gh CLI

Config (connections.yaml, optional):
  git:
    protected: [main, master]
    co_author: "Claude <noreply@anthropic.com>"
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys

from .. import config

SECRET_GLOBS = ["service-account*.json", "*-service-account.json", "secrets.env",
                ".env", "*.pem", "id_rsa", "id_rsa.*", "*.key"]


def _git(args, check=True, capture=True):
    r = subprocess.run(["git", *args], capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.exit((r.stderr or r.stdout or f"git {' '.join(args)} failed").strip())
    return (r.stdout or "").strip()


def _current_branch():
    """Branch name, or None if in detached-HEAD state."""
    r = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _protected():
    data, _ = config.load_connections()
    p = config.get_path(data, "git.protected")
    return set(p) if isinstance(p, list) else {"main", "master"}


def _co_author(argv):
    ca = _opt(argv, "--co-author")
    if ca:
        return ca
    data, _ = config.load_connections()
    return config.get_path(data, "git.co_author")


def _slug(msg, maxlen=40):
    s = re.sub(r"[^\w]+", "-", msg, flags=re.UNICODE).strip("-").lower()
    return s[:maxlen].strip("-") or "work"


def _branch_exists(name):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                          capture_output=True, text=True).returncode == 0


def _unique_branch(base):
    if not _branch_exists(base):
        return base
    sha = _git(["rev-parse", "--short", "HEAD"], check=False) or ""
    cand = f"{base}-{sha}" if sha else f"{base}-1"
    n = 1
    while _branch_exists(cand):
        n += 1
        cand = f"{base}-{sha}-{n}" if sha else f"{base}-{n}"
    return cand


def _staged_secret_files():
    out = _git(["diff", "--cached", "--name-only"], check=False)
    hits = []
    for f in out.splitlines():
        f = f.strip()
        if f and any(fnmatch.fnmatch(f.rsplit("/", 1)[-1], g) for g in SECRET_GLOBS):
            hits.append(f)
    return hits


def _message(argv):
    """First positional, skipping the value that belongs to --co-author."""
    skip = set()
    for vf in ("--co-author",):
        if vf in argv:
            skip.add(argv.index(vf) + 1)
    for i, a in enumerate(argv):
        if i in skip or a.startswith("-"):
            continue
        return a
    return None


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def cmd_status(_argv):
    print(_git(["status", "-sb"]))


def cmd_save(argv):
    msg = _message(argv)
    if not msg:
        sys.exit('save needs a message, e.g. bizconnect git save "fix parser"')
    _git(["add", "-A"])
    if not _git(["status", "--porcelain"]):
        print("nothing to commit")
        return
    hits = _staged_secret_files()
    if hits and "--allow-secrets" not in argv:
        sys.exit("refusing to commit — these staged files look like secrets:\n  "
                 + "\n  ".join(hits)
                 + "\nThey belong in the central store (~/.config/biz-connect), not a repo. "
                   "Remove/gitignore them\n(run `bizconnect init` to add guards), or pass --allow-secrets to override.")
    branch = _current_branch()
    if (branch is None or branch in _protected()) and "--allow-main" not in argv:
        target = _unique_branch(f"wip/{_slug(msg)}")
        _git(["switch", "-c", target])
        where = "detached HEAD" if branch is None else f"protected '{branch}'"
        print(f"(was on {where} — created and switched to '{target}')")
        branch = target
    full = msg
    ca = _co_author(argv)
    if ca:
        full = f"{msg}\n\nCo-Authored-By: {ca}"
    _git(["commit", "-m", full])
    print(f"committed to {branch}: {msg}")
    if "--push" in argv:
        _git(["push", "-u", "origin", branch])
        print(f"pushed {branch}")


def cmd_sync(_argv):
    branch = _current_branch()
    if branch is None:
        sys.exit("detached HEAD — checkout a branch before sync.")
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False)
    if upstream:
        _git(["pull", "--rebase", "--autostash"])
        _git(["push"])
        print(f"synced {branch} (rebased + pushed)")
    else:
        _git(["push", "-u", "origin", branch])
        print(f"synced {branch} (set upstream + pushed)")


def cmd_pr(argv):
    if subprocess.run(["gh", "--version"], capture_output=True, text=True).returncode != 0:
        sys.exit("the GitHub CLI (gh) is required for `git pr`. Install it or open the PR manually.")
    branch = _current_branch()
    if branch is None:
        sys.exit("detached HEAD — checkout a branch before opening a PR.")
    _git(["push", "-u", "origin", branch])      # surface push failures before invoking gh
    args = ["pr", "create"]
    title, body = _opt(argv, "--title"), _opt(argv, "--body")
    if title:
        args += ["--title", title]
    if body:
        args += ["--body", body]
    if not title and not body:
        args += ["--fill"]
    sys.exit(subprocess.run(["gh", *args], text=True).returncode)


VERBS = {"status": cmd_status, "save": cmd_save, "sync": cmd_sync, "pr": cmd_pr}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit(f"unknown git verb {verb!r}. One of: {', '.join(VERBS)}")
    fn(rest)
    return 0
