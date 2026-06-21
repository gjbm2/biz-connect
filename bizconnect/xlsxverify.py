"""Mechanical verifier for workbook-diff narratives (anti-hallucination gate).

Runs AFTER an agent has written a narrative from a diff.json fact graph. It is
deterministic and authoritative: it proves that every FINANCIAL figure in the
narrative is grounded in the JSON and that citations resolve and agree. It does
NOT judge prose quality. Fail-closed: anything it cannot parse fails.

Checks:
  1. PROVENANCE — the narrative front-matter `diff_run_id` must equal the JSON's
     (case-insensitive). Mismatch -> REFUSE.
  2. CITATIONS — every {F..}/{H..}/{C..} token must resolve. A figure on a cited
     line must agree (near-exact, same unit dimension) with a citation on that line.
  3. GROUNDING — every financial-looking number (currency, %, pp, scaled, a bare
     >=4-digit / decimal run, or a number with a unit word) must appear in the
     JSON's citable set, in the SAME dimension (a percent cannot match a dollar).
  4. CAUSATION — causal/attribution phrasing next to a {C..} is allowed only if
     that link is path_proven with high confidence.

Public API: verify(text, diff) / verify_files(narrative, json) -> result dict.
"""
from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

_CITE = re.compile(r"\{([FHC]\d+)\}")
_RUNID = re.compile(r"diff_run_id\s*[:=]\s*[\"']?(wbdiff2-[0-9a-fA-F]+)", re.I)

# A financial-looking number. Order matters (currency/grouped before bare).
_NUM = re.compile(
    r"\(?[-+]?[$£€¥]\s?\d[\d,]*(?:\.\d+)?\s?(?:bn|billion|m|million|k|thousand)?\)?"   # currency
    r"|\(?[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?\s?(?:bn|billion|m|million|k|thousand)?"  # grouped
    r"|[-+]?\d+(?:\.\d+)?\s?(?:%|pp|percentage points?|percent|bps|basis points?)"      # ratio
    r"|[-+]?\d+(?:\.\d+)?\s?(?:bn|billion|m|million|k|thousand|dollars?|usd|gbp|eur|pounds?|euros?)\b"  # scaled/word
    r"|[-+]?\d{4,}(?:\.\d+)?"                                                            # bare >=4-digit
    r"|[-+]?\d+\.\d+",                                                                   # bare decimal
    re.I)

_RATIO_MARK = re.compile(r"%|\bpp\b|percentage points?|percent|bps|basis points?", re.I)
_CAUSAL_WORDS = re.compile(
    r"\b(caused|because|drove|driv(?:e|en|ing)|due to|led to|leads? to|results? from|"
    r"resulted from|stem(?:s|med)? from|arose from|attributable to|as a result of|"
    r"owing to|thanks to|on the back of|triggered|responsible for|contributed to|"
    r"reflecting|reflects)\b", re.I)


def _parse_token(tok):
    """(magnitude, dimension) for a financial token; dimension in {ratio, abs}.
    bps -> ratio scaled to percentage-points (50bps == 0.5)."""
    t = tok.strip()
    low = t.lower()
    neg = (t.startswith("(") and ")" in t) or bool(re.match(r"\s*-", t))
    is_ratio = bool(_RATIO_MARK.search(low))
    is_bps = "bps" in low or "basis point" in low
    num = re.sub(r"[(),$£€¥+\s]", "", low)
    scale = 1.0
    for suf, mul in (("billion", 1e9), ("bn", 1e9), ("million", 1e6),
                     ("thousand", 1e3), ("m", 1e6), ("k", 1e3)):
        if num.endswith(suf):
            num = num[:-len(suf)]; scale = mul; break
    num = re.sub(r"(percentagepoints?|percent|basispoints?|bps|pp|%|dollars?|usd|gbp|eur|pounds?|euros?)$",
                 "", num)
    try:
        v = float(num) * scale
    except ValueError:
        return None
    if is_bps:
        v /= 100.0
    if neg and v > 0:
        v = -v
    return (round(v, 4), "ratio" if is_ratio else "abs")


def _add_token(buckets, tok):
    pt = _parse_token(tok)
    if pt:
        buckets[pt[1]].add(pt[0])
        buckets[pt[1]].add(round(abs(pt[0]), 4))


def _collect_allowed(diff):
    """Citable magnitudes split by dimension, from display strings + raw numbers."""
    buckets = {"ratio": set(), "abs": set()}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if (k.endswith("_display") or k == "delta_display") and isinstance(v, str):
                    for m in _NUM.finditer(v):
                        _add_token(buckets, m.group(0))
                elif k.endswith(("_raw", "_abs")) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    buckets["abs"].add(round(float(v), 4))      # raw amounts are absolute
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(diff.get("headline_metrics", []))
    walk(diff.get("sheets", []))
    walk(diff.get("named_ranges", {}))
    for v in (diff.get("totals") or {}).values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            buckets["abs"].add(round(float(v), 4))
    return {k: sorted(s) for k, s in buckets.items()}


def _match(value, allowed_sorted):
    """Near-exact membership (absorbs only last-digit rounding of the SAME display)."""
    if not allowed_sorted:
        return False
    i = bisect.bisect_left(allowed_sorted, value)
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(allowed_sorted):
            a = allowed_sorted[j]
            if abs(value - a) <= max(0.05, 1e-6 * abs(a)):
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


def _entity_buckets(ent):
    b = {"ratio": [], "abs": []}
    for k, v in ent.items():
        if (k.endswith("_display") or k == "delta_display") and isinstance(v, str):
            for m in _NUM.finditer(v):
                pt = _parse_token(m.group(0))
                if pt:
                    b[pt[1]].append(pt[0]); b[pt[1]].append(round(abs(pt[0]), 4))
        elif k.endswith(("_raw", "_abs")) and isinstance(v, (int, float)) and not isinstance(v, bool):
            b["abs"].append(round(float(v), 4)); b["abs"].append(round(abs(float(v)), 4))
    return {k: sorted(set(x)) for k, x in b.items()}


def _frontmatter_end(lines):
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i
    return -1


def verify(narrative_text, diff):
    issues = []
    stats = {"citations": 0, "numbers_checked": 0}

    json_run = diff.get("diff_run_id")
    m = _RUNID.search(narrative_text)
    nar_run = m.group(1) if m else None
    if not json_run:
        issues.append({"severity": "fail", "reason": "diff.json has no diff_run_id (cannot bind)."})
    elif nar_run is None:
        issues.append({"severity": "fail", "reason": "narrative is missing diff_run_id in its front-matter."})
    elif nar_run.casefold() != json_run.casefold():
        issues.append({"severity": "fail",
                       "reason": f"diff_run_id mismatch: narrative {nar_run} != json {json_run}."})

    entities = _index_entities(diff)
    allowed = _collect_allowed(diff)
    lines = narrative_text.splitlines()
    fm_end = _frontmatter_end(lines)

    for lineno, line in enumerate(lines, 1):
        if lineno - 1 <= fm_end:               # inside the YAML front-matter block
            continue
        cites = _CITE.findall(line)
        stats["citations"] += len(cites)
        cite_buckets = []
        for cid in cites:
            if cid not in entities:
                issues.append({"severity": "fail", "line": lineno,
                               "reason": f"dangling citation {{{cid}}} (no such id in diff.json)."})
            else:
                cite_buckets.append((cid, _entity_buckets(entities[cid])))

        for nm in _NUM.finditer(_CITE.sub(" ", line)):   # don't read digits inside {F0001}
            raw_tok = nm.group(0).strip()
            if re.fullmatch(r"(19|20)\d\d", raw_tok):     # a bare year is not a financial figure
                continue
            pt = _parse_token(raw_tok)
            if pt is None:
                continue
            val, dim = pt
            stats["numbers_checked"] += 1
            if cite_buckets:
                if not any(_match(val, b.get(dim, [])) for _cid, b in cite_buckets):
                    issues.append({"severity": "fail", "line": lineno,
                                   "reason": f"figure {nm.group(0).strip()!r} contradicts its line "
                                             f"citation ({', '.join(c for c, _ in cite_buckets)}) "
                                             "or is the wrong unit — cite the fact it comes from."})
            elif not _match(val, allowed.get(dim, [])):
                issues.append({"severity": "fail", "line": lineno,
                               "reason": f"fabricated/ungrounded figure {nm.group(0).strip()!r} "
                                         "(not present in diff.json in that unit)."})

        if _CAUSAL_WORDS.search(line):
            for cid in cites:
                ent = entities.get(cid, {})
                if cid.startswith("C") and not (ent.get("path_proven") and ent.get("confidence") == "high"):
                    issues.append({"severity": "fail", "line": lineno,
                                   "reason": f"causal phrasing cites {{{cid}}} which is not "
                                             "path-proven high-confidence (hedge instead)."})

    fails = [i for i in issues if i["severity"] == "fail"]
    return {"status": "PASS" if not fails else "FAIL", "issues": issues,
            "stats": stats, "diff_run_id": json_run}


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
