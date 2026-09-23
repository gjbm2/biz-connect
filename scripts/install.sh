#!/usr/bin/env bash
# biz-connect installer (macOS / Linux / Git Bash). Registers the marketplace, installs the
# plugin, and runs a setup check. Safe to re-run. Needs Python 3.9+ (python3, python or py).
# PYTHON=/path/to/python3 picks the interpreter explicitly.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -n "${PYTHON:-}" ]; then export BIZCONNECT_PYTHON="$PYTHON"; fi

echo "==> Installing the biz-connect plugin"
if command -v claude >/dev/null 2>&1; then
  claude plugin marketplace add gjbm2/biz-connect || true
  claude plugin install biz-connect@biz-connect || true
else
  echo "  claude CLI not found. In the Claude Code REPL, run:"
  echo "    /plugin marketplace add gjbm2/biz-connect"
  echo "    /plugin install biz-connect@biz-connect"
fi

echo "==> Checking setup (doctor)"
# bin/bizconnect is the command shim: it picks python3 / python / py -3 and runs the launcher.
sh "$ROOT/bin/bizconnect" doctor || true

cat <<EOF2

Next steps:
  1. In each repo you want to connect, run \`bizconnect init\` at the repo root.
     The first run also creates your per-user store ~/.config/biz-connect/secrets.env
     (credentials live there, never in any repo).
  2. Notion: create an internal integration at https://www.notion.so/profile/integrations
     (capabilities: Read content, Update content, Insert content), put its secret in
     NOTION_TOKEN= in secrets.env, and share your hub page with the integration
     (the page's ... menu -> Connections; sub-pages inherit).
     Google connectors only (gdoc / sheet): drop your service-account.json into
     ~/.config/biz-connect/.
  3. Re-run \`bizconnect doctor\` until it prints OK.

Inside Claude Code the plugin puts the \`bizconnect\` command on PATH. From a plain
terminal, run "$ROOT/bin/bizconnect" (or python3 "$ROOT/scripts/bizconnect.py").
EOF2
