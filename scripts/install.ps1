# biz-connect installer (Windows / PowerShell). Registers the marketplace, installs
# the plugin, and runs a setup check. Safe to re-run. Needs Python 3.9+ (py or python).
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

Write-Host '==> Installing the biz-connect plugin'
if (Get-Command claude -ErrorAction SilentlyContinue) {
  claude plugin marketplace add gjbm2/biz-connect
  claude plugin install biz-connect@biz-connect
} else {
  Write-Host '  claude CLI not found. In the Claude Code REPL, run:'
  Write-Host '    /plugin marketplace add gjbm2/biz-connect'
  Write-Host '    /plugin install biz-connect@biz-connect'
}

Write-Host '==> Checking setup (doctor)'
# bin\bizconnect.cmd is the command shim: it picks py -3 / python and runs the launcher
# (and says so if no Python 3.9+ is installed).
& "$root\bin\bizconnect.cmd" doctor

Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1. In each repo you want to connect, run `bizconnect init` at the repo root.'
Write-Host '     The first run also creates your per-user store ~/.config/biz-connect/secrets.env'
Write-Host '     (credentials live there, never in any repo).'
Write-Host '  2. Notion: create an internal integration at https://www.notion.so/profile/integrations'
Write-Host '     (capabilities: Read content, Update content, Insert content), put its secret in'
Write-Host '     NOTION_TOKEN= in secrets.env, and share your hub page with the integration'
Write-Host "     (the page's ... menu -> Connections; sub-pages inherit)."
Write-Host '     Google connectors only (gdoc / sheet): drop your service-account.json into'
Write-Host '     ~/.config/biz-connect/.'
Write-Host '  3. Re-run `bizconnect doctor` until it prints OK.'
Write-Host ''
Write-Host 'Inside Claude Code the plugin puts the `bizconnect` command on PATH. From a plain'
Write-Host "terminal, run `"$root\bin\bizconnect.cmd`" (or py -3 `"$root\scripts\bizconnect.py`")."
