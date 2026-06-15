#!/usr/bin/env bash
# biz-connect installer (POSIX). Registers the marketplace, installs the plugin,
# and runs a setup check. Safe to re-run.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"; command -v "$PY" >/dev/null 2>&1 || PY=python

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
"$PY" "$ROOT/scripts/bizconnect.py" doctor || true

cat <<'EOF'

Next steps:
  1. Credentials (once per user) — never in any repo:
       edit ~/.config/biz-connect/secrets.env   (set NOTION_TOKEN=...)
       drop your Google service-account.json into ~/.config/biz-connect/
  2. Per repo:  run `bizconnect init` in the repo root, then edit connections.yaml
  3. Re-run the doctor until it prints OK.
EOF
