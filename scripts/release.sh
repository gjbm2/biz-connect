#!/usr/bin/env bash
# Cut a biz-connect release: bump .claude-plugin/plugin.json version, commit, tag, push.
# Users then get it via `/plugin update biz-connect` (the version bump drives the nudge).
# Usage: scripts/release.sh 0.2.0
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
V="${1:?usage: scripts/release.sh <version>  e.g. 0.2.0}"
PY="${PYTHON:-python3}"; command -v "$PY" >/dev/null 2>&1 || PY=python

"$PY" - "$ROOT" "$V" <<'PYEOF'
import json, sys, pathlib
root, ver = sys.argv[1], sys.argv[2]
f = pathlib.Path(root) / ".claude-plugin" / "plugin.json"
d = json.loads(f.read_text(encoding="utf-8"))
d["version"] = ver
f.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
print("set plugin.json version =", ver)
PYEOF

cd "$ROOT"
git add .claude-plugin/plugin.json
git commit -m "release v$V"
git tag "v$V"
git push origin HEAD --tags
echo "released v$V — users update with: /plugin update biz-connect"
