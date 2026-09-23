"""biz-connect — business-service connectors driven by a per-repo connections.yaml
and a per-user central secret store. See README.md."""


def _plugin_version() -> str:
    """The installed plugin version, from .claude-plugin/plugin.json (the one version that
    releases bump). Never raises: a missing/unreadable manifest gives '0+unknown'."""
    try:
        import json
        from pathlib import Path
        pj = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
        return str(json.loads(pj.read_text(encoding="utf-8-sig")).get("version") or "0+unknown")
    except Exception:
        return "0+unknown"


__version__ = _plugin_version()
