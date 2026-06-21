"""Mechanical verifier for workbook-diff narratives (anti-hallucination gate).

Runs AFTER an agent has written a narrative from a diff.json fact graph. It is
deterministic and authoritative: it does not "review" prose for quality, it
proves that every FINANCIAL figure in the narrative is grounded in the JSON and
that citations resolve and agree. Fail-closed: anything it cannot parse fails.

Checks:
  1. PROVENANCE — the narrative front-matter `diff_run_id` must equal the JSON's.
     A mismatch means the narrative was written against a different diff (other
     files / other run) -> REFUSE outright.
  2. CITATIONS — every {F..}/{H..}/{C..} token must resolve to a real id. A number
     sitting next to a citation must match one of that entity's values.
  3. GROUNDING — every financial-looking number ($, %, pp, m/bn, or thousands)
     must appear somewhere in the JSON's citable number set (facts, headlines,
     totals) within tolerance. A number that appears nowhere is a fabrication.
  4. CAUSATION — 'caused'/'because'/'drove'/'due to' adjacent to a {C..} citation
     is only allowed if that causal link is path_proven with high confidence.

Public API:
    verify(narrative_text, diff_json) -> dict(status=PASS|FAIL, issues=[...], stats)
    verify_files(narrative_path, json_path) -> same
CLI (via the connector): bizconnect xlsx verify NARRATIVE.md diff.json
"""
from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

_CITE = re.compile(r"\{([FHC]\d+)\}")
_RUNID = re.compile(r"diff_run_id\s*[:=]\s*[\"']?(wbdiff2-[0-9a-f]+)", re.I)
# financial-looking number: optional sign/paren, optional currency, digits with
# thousands separators or a decimal, optional %/pp/m/bn/k suffix.
_NUM = re.compile(
    r"\(?\s*[-+]?\s*[$£€¥]?\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*[%]?\)?"   # thousands-grouped
    r"|\(?\s*[-+]?\s*[$£€¥]\s*\d+(?:\.\d+)?\s*(?:[mbn]{1,2}|k)?\)?"      # currency
    r"|[-+]?\s*\d+(?:\.\d+)?\s*(?:pp|%)"                                 # percent / pp
    r"|[-+]?\s*\d+(?:\.\d+)?\s*(?:bn|m|k)\b",                            # scaled
    re.I)

_CAUSAL_WORDS = re.compile(r"\b(caused|because|drove|driven by|due to|led to|results? from)\b", re.I)


def _to_magnitude(tok):
    """Parse a financial token to a signed magnitude (suffix-scaled)."""
    t = tok.strip()
    neg = t.startswith("(") and t.endswith(")") or "-" in t[:2]
    t = t.strip("()").replace(",", "").strip()
    scale = 1.0
    low = t.lower()
    if low.endswith("pp") or low.endswith("%"):
        t = re.sub(r"(pp|%)$", "", low, flags=re.I)
    elif low.endswith("bn"):
        t = low[:-2]; scale = 1e9
    elif low.endswith("k"):
        t = low[:-1]; scale = 1e3
    elif low.endswith("m"):
        t = low[:-1]; scale = 1e6
    t = re.sub(r"[$£€¥+\s]", "", t)
    try:
        v = float(t) * scale
    except ValueError:
        return None
    return -v if (neg and v > 0) else v


def _collect_allowed(diff):
    """All citable magnitudes (from displays + raw + totals), sorted for bisect.
    Display strings are parsed too, so a quoted display matches numerically."""
    mags = set()

    def add_num(x):
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            mags.add(round(float(x), 4))

    def add_display(s):
        if isinstance(s, str):
            for m in _NUM.finditer(s):
                v = _to_magnitude(m.group(0))
                if v is not None:
                    mags.add(round(v, 4))

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith(("_raw", "_abs", "_pct", "_pp")) or k in ("old_raw", "new_raw"):
                    add_num(v)
                if k.endswith("_display") or k == "delta_display":
                    add_display(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)):
            add_num(node)

    walk(diff.get("headline_metrics", []))
    walk(diff.get("sheets", []))
    walk(diff.get("named_ranges", {}))
    for v in (diff.get("totals") or {}).values():
        add_num(v)
    # also raw percent magnitudes scaled to display (0.0855 -> 8.55)
    extra = set()
    for m in list(mags):
        if abs(m) < 1:
            extra.add(round(m * 100, 4))
    mags |= extra
    return sorted(mags)


def _match(value, allowed_sorted):
    if not allowed_sorted:
        return False
    av = abs(value)
    i = bisect.bisect_left(allowed_sorted, value)
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(allowed_sorted):
            a = allowed_sorted[j]
            if abs(value - a) <= max(0.05, 0.01 * max(av, abs(a))):
                return True
    return False


def _index_entities(diff):
    idx = {}
    for h in diff.get("headline_metrics", []):
        if h.get("id"):
            idx[h["id"]] = h
    for c in diff.get("causal_links", []):
        if c.get("id"):
            idx[c["id"]] = c
    for s in diff.get("sheets", []):
        for f in s.get("facts", []):
            if f.get("id"):
                idx[f["id"]] = f
    return idx


def _entity_magnitudes(ent):
    mags = []
    for k, v in ent.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and \
                (k.endswith(("_raw", "_abs", "_pct", "_pp"))):
            mags.append(round(float(v), 4))
            if abs(v) < 1:
                mags.append(round(float(v) * 100, 4))
        if (k.endswith("_display") or k == "delta_display") and isinstance(v, str):
            for m in _NUM.finditer(v):
                x = _to_magnitude(m.group(0))
                if x is not None:
                    mags.append(round(x, 4))
    return sorted(set(mags))


def verify(narrative_text, diff):
    issues = []
    stats = {"citations": 0, "numbers_checked": 0}

    # 1) provenance
    json_run = diff.get("diff_run_id")
    m = _RUNID.search(narrative_text)
    nar_run = m.group(1) if m else None
    if not json_run:
        issues.append({"severity": "fail", "reason": "diff.json has no diff_run_id (cannot bind)."})
    elif nar_run is None:
        issues.append({"severity": "fail", "reason": "narrative front-matter is missing diff_run_id."})
    elif nar_run != json_run:
        issues.append({"severity": "fail",
                       "reason": f"diff_run_id mismatch: narrative {nar_run} != json {json_run} "
                                 "(narrative written against a different diff)."})

    entities = _index_entities(diff)
    allowed = _collect_allowed(diff)
    lines = narrative_text.splitlines()

    # 2/3) per-line number + citation checks
    for lineno, line in enumerate(lines, 1):
        if line.lstrip().startswith(("diff_run_id", "engine_version", "schema_version")):
            continue
        cites = _CITE.findall(line)
        stats["citations"] += len(cites)
        for cid in cites:
            if cid not in entities:
                issues.append({"severity": "fail", "line": lineno,
                               "reason": f"dangling citation {{{cid}}} (no such id in diff.json)."})
        line_cite_mags = []
        for cid in cites:
            if cid in entities:
                line_cite_mags.append((cid, _entity_magnitudes(entities[cid])))

        for nm in _NUM.finditer(line):
            tok = nm.group(0).strip()
            val = _to_magnitude(tok)
            if val is None:
                continue
            stats["numbers_checked"] += 1
            if line_cite_mags:
                # cited line: the figure MUST agree with a citation on this line
                if not any(_match(val, mags) for _cid, mags in line_cite_mags):
                    issues.append({"severity": "fail", "line": lineno,
                                   "reason": f"figure {tok!r} contradicts its line citation "
                                             f"({', '.join(c for c, _ in line_cite_mags)}) — "
                                             "cite the fact this number comes from."})
            elif not _match(val, allowed):
                # uncited line: the figure must at least exist somewhere in the JSON
                issues.append({"severity": "fail", "line": lineno,
                               "reason": f"fabricated/ungrounded figure {tok!r}: not present in diff.json."})

        # 4) causation phrasing
        if _CAUSAL_WORDS.search(line):
            for cid in cites:
                ent = entities.get(cid, {})
                if cid.startswith("C") and not (ent.get("path_proven") and ent.get("confidence") == "high"):
                    issues.append({"severity": "fail", "line": lineno,
                                   "reason": f"causal phrasing cites {{{cid}}} which is not "
                                             "path-proven high-confidence (hedge instead)."})

    fails = [i for i in issues if i["severity"] == "fail"]
    return {"status": "PASS" if not fails else "FAIL",
            "issues": issues, "stats": stats,
            "diff_run_id": json_run}


def verify_files(narrative_path, json_path):
    try:
        diff = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "FAIL", "issues": [{"severity": "fail",
                "reason": f"could not read/parse diff.json: {e}"}], "stats": {}}
    try:
        text = Path(narrative_path).read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "FAIL", "issues": [{"severity": "fail",
                "reason": f"could not read narrative: {e}"}], "stats": {}}
    return verify(text, diff)


def format_report(result):
    L = [f"verifier: {result['status']}"]
    if result.get("stats"):
        L.append(f"  citations={result['stats'].get('citations', 0)} "
                 f"numbers_checked={result['stats'].get('numbers_checked', 0)}")
    for i in result.get("issues", []):
        loc = f"L{i['line']}: " if i.get("line") else ""
        L.append(f"  [{i['severity'].upper()}] {loc}{i['reason']}")
    return "\n".join(L)
