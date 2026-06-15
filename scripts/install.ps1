# biz-connect installer (Windows / PowerShell). Registers the marketplace, installs
# the plugin, and runs a setup check. Safe to re-run.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$py = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' }
      elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { $null }

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
if ($py) { & $py "$root\scripts\bizconnect.py" doctor }
else { Write-Host '  No python/py found on PATH.' }

Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1. Edit ~/.config/biz-connect/secrets.env (NOTION_TOKEN=...) and drop'
Write-Host '     your Google service-account.json into ~/.config/biz-connect/'
Write-Host '  2. Per repo: run `bizconnect init` in the repo root, then edit connections.yaml'
Write-Host '  3. Re-run the doctor until it prints OK.'
