---
name: git-flow
description: Standardised, safe git flow for a project repo — commit (branching off a protected branch first), sync (rebase-pull then push), and open a PR. Use when the user asks to save/commit/sync/push work or open a pull request and you want consistent, safe handling across repos.
allowed-tools: Bash(python *), Bash(git *), Bash(gh *)
---

# Standardised git flow

House rules applied consistently across every repo:

- never commit straight to a protected branch (`main`/`master`) — branch first;
- attribute AI-assisted commits with a `Co-Authored-By` trailer;
- sync = rebase-pull with autostash, then push.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" git status
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" git save "short message" --co-author "Claude <noreply@anthropic.com>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" git sync
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" git pr --title "..." --body "..."
```

`save` stages everything, and if you're on a protected branch it creates `wip/<slug>`
first so `main` is never committed to directly. Add `--push` to push immediately.
Pass the appropriate `--co-author` trailer for whoever/whatever is co-authoring (or
set `git.co_author` in `connections.yaml` as a repo default).

This standardises the routine flow; for anything unusual (interactive rebase, history
surgery, force-push), use raw `git` deliberately and explain what you're doing.
