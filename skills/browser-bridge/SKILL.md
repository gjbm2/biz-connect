---
name: browser-bridge
description: Set up, configure and troubleshoot the biz-connect browser extension ("Ask Claude"). You select text on any web page, right-click, and Claude Code acts on it in your workspace with that repo's own skills, tools and instructions. Use when the user wants the browser extension or plugin, wants to act on a web-page selection from the browser, asks to add or change a right-click menu item, or the panel says the bridge isn't installed.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read, Edit
---

# Browser bridge: select text on a page, hand it to Claude Code

**The extension sends the selection, its page and a stable anchor to Claude Code running in the
user's workspace (a repo), so a point edit needs no copy-paste and no context-setting.** The run
loads the repo's CLAUDE.md, skills, plugins and MCP servers, exactly as a terminal or VS Code
session does. Bigger jobs continue in VS Code with one click.

Commands are `bizconnect browser <verb>`. If `bizconnect` isn't on PATH, run the launcher with the
same arguments: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" browser ...` (`py -3` on
Windows).

## Set up (once per user, then once per browser profile)

1. In the repo the agent should work in, run `bizconnect browser install`. It:
   - makes the bridge's own venv (`~/.config/biz-connect/browser/.venv`, about 250 MB, once);
   - registers the native-messaging host with Chrome, Brave, Edge and Chromium (Windows: HKCU
     registry; macOS/Linux: each browser's `NativeMessagingHosts` folder);
   - copies the extension to `~/.config/biz-connect/browser/extension`;
   - registers the current repo as the default workspace.
2. In the browser, open `chrome://extensions` (`brave://extensions`, `edge://extensions`), turn on
   **Developer mode**, choose **Load unpacked** and pick the folder the install printed. The
   extension id is always `lapfcbolncefglmmgofpggdigjchekbe`; the host only talks to that id.
3. Check with `bizconnect browser status`.

No store listing and no network port are involved. The extension reaches the host only through
the browser's native messaging, and the host accepts only this extension.

**After a biz-connect update**, re-run `bizconnect browser install`, then press the reload button
on the extension's card. The panel shows a banner when the two are out of step.

## Use

- Select text, then right-click → **Ask Claude…**, press **Alt+Shift+A**, or click the toolbar
  icon. The side panel opens with the selection and the site it came from.
- Type the instruction and press Enter. Claude's steps stream into the panel. Type again to
  follow up in the same session.
- **New session** starts fresh. A new selection also starts a new session on the next Run.
- **Continue in VS Code** opens the same session in the Claude Code panel of VS Code (needs the
  workspace folder open there). **Copy resume command** gives `claude --resume <id>` for a terminal.
- **Google Docs** draws its text on a canvas, so the page can't report the selection. Copy the
  passage there (Ctrl+C), trigger the panel, and paste it into the box it shows.

## Where edits go

| Site | Claude reads | Claude writes |
|---|---|---|
| Notion | the selection and its block link | the mapped file, then `bizconnect notion push` (the notion-sync skill). Never the page itself. |
| Google Docs | the Doc id and the pasted passage | `bizconnect gdoc insert` / `replace`, in place. Never a copy. |
| Any other site | the selection, then the page on request | straight into the selection, typed as if by the user (`replace_selection`, `insert_after_selection`) |

The host decides the route from the URL and only offers the page-writing tools on other sites.
Page tools write text only. They cannot click, submit or navigate, so the user always presses
Send, Save or Submit. Each page edit lands on the page's own undo stack (Ctrl+Z).

## Configure (per workspace, in its connections.yaml)

```yaml
browser:
  permission_mode: auto        # auto (default) | default | acceptEdits | plan
  model:                       # blank = the user's Claude Code default (e.g. set `sonnet` for speed)
  deny: []                     # extra tools browser runs may never use (fnmatch patterns on tool names)
  confirm_page_writes: false   # true = approve every page edit in the panel
  load_plugin: true            # load biz-connect into runs even if the plugin is disabled in settings
  menu:                        # right-click items; each runs a skill (or a prompt) on the selection
    - label: Tighten this paragraph
      skill: tighten           # any skill the workspace has; plugin skills as plugin:skill
    - label: Explain in plain English
      prompt: Explain the selection in plain English for a non-specialist.
      sites: [web]             # optional: notion | google-doc | web, or URL match patterns
```

- **Menu items run skills.** Write a point-edit skill as an ordinary skill in the repo
  (`.claude/skills/<name>/SKILL.md`), then list it here. It then works from the browser and from
  VS Code alike. The selection arrives as the browser context of the skill's first turn.
- `bizconnect browser menu` checks the section and lists problems (exit 1 if any).
- The menu refreshes when the panel opens or the browser starts. Items also appear as chips in
  the panel.

**Permissions, in layers.**
- The workspace's permission mode applies. In auto mode, routine work proceeds without prompts.
- Anything that still needs a decision appears in the panel as Allow / Deny. Unanswered cards
  are denied after 15 minutes.
- A built-in deny list blocks, in every mode: sending email or messages (drafts only), and
  the Workflow tool (no multi-agent fan-out from a right-click). `browser.deny` adds to the list.
- Page content is passed to Claude marked as untrusted data, never as instructions.

## More workspaces

`bizconnect browser add [PATH] [--name N] [--default]` registers another repo; the panel's
workspace menu switches between them. `bizconnect browser default NAME` and
`bizconnect browser remove NAME` manage the list.

## Troubleshooting

- **"The biz-connect bridge isn't installed for this browser"**: run `bizconnect browser
  install`, then reopen the panel. `bizconnect browser status` shows which browsers are registered.
- **The run fails or stops**: read `~/.config/biz-connect/browser/host.log`.
- **"claude is not on PATH"** at install: the bridge then uses the copy of Claude Code bundled
  with the SDK, which has its own login. Install Claude Code and re-run the install.
- **A page edit is refused**: the selection is not in an editable area, the page reloaded, or
  the text changed since it was selected. Claude then gives the text in its reply.
- Closing the panel ends the run. The session is still saved, so **Continue in VS Code** or
  `claude --resume <id>` picks it up.
- The shortcut can be changed at `chrome://extensions/shortcuts` (`brave://...`, `edge://...`).

`bizconnect browser uninstall` removes the host registrations. Remove the extension in the
browser, and delete `~/.config/biz-connect/browser` to remove everything.
