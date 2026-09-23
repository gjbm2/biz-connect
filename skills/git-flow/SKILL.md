---
name: git-flow
description: Standardised, safe git flow for a project repo — commit (branching off a protected branch first), sync (rebase-pull then push), and open a PR. Use when the user asks to save/commit/sync/push work or open a pull request and you want consistent, safe handling across repos.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Bash(git *), Bash(gh *)
---

# Standardised git flow

House rules applied consistently across every repo:

- never commit straight to a protected branch (`main`/`master`) — branch first;
- attribute AI-assisted commits with a `Co-Authored-By` trailer;
- sync = rebase-pull with autostash, then push.

```bash
bizconnect git status
bizconnect git save "short message" --co-author "Claude <noreply@anthropic.com>"
bizconnect git sync
bizconnect git pr --title "..." --body "..."
```

If `bizconnect` isn't found (the plugin was installed in this session, or you're in a
clone), run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> ...` (`python` or `py`
on Windows).

`save` stages everything, and if you're on a protected branch it creates `wip/<slug>`
first so `main` is never committed to directly. Add `--push` to push immediately.
Pass the appropriate `--co-author` trailer for whoever/whatever is co-authoring (or
set `git.co_author` in `connections.yaml` as a repo default).

This standardises the routine flow; for anything unusual (interactive rebase, history
surgery, force-push), use raw `git` deliberately and explain what you're doing.
