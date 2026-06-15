"""compose — incremental, config-driven document-composition pipeline.

Build a structured document (per-item answers -> front matter -> rendered doc) from a
corpus, with deterministic glue in code and judgement steps run by an agent against your
own prompt templates. **Content-free**: every project specific (the items, the prompts, the
guides, the evidence) lives in the consuming repo and is named by `pipeline.yaml`.

The submission is a compiled artifact. Each TARGET in the graph tracks a content-hash of
its declared inputs, so you can (re)build any SUBSET — change one item's inputs and only
that item's chain goes stale, never the whole graph.

Targets (per-item targets are parameterised over the items in your `pipeline.yaml`):
    inputs          (code) sync external source docs    -> files named by connections.yaml `inputs:`
    assemble:<id>   (code) evidence pack for an item   -> <build>/<id>.context.md
    spec:<id>       (llm)  the item's argument guide    -> <local_dir>/<id>.md   (human-owned)
    draft:<id>      (llm)  the item's answer            -> <answers_dir>/<id>.md (human-owned)
    critique:<id>   (llm)  adversarial review           -> <build>/<id>.critique.gen.md
    ladder          (llm)  front matter: template+intro over answers -> <submission> (human-owned)
    lint            (code) completeness / provenance    -> <build>/lint-report.md
    render          (code) assembled document           -> <final>

code targets run here (deterministic). llm targets are run by an AGENT: compose assembles
the exact prompt (your template + resolved context) into <build>/, you run it, promote the
result into the human-owned canonical file, then record it with `accept`. compose NEVER
overwrites a human-owned file (clobber-safe).

Verbs
-----
  status [target ...]        FRESH / STALE / MISSING for all (or named) targets
  run <stage> [id|all]       build target(s): code runs; llm assembles a prompt for an agent
  accept <stage> [id|all]    record inputs as built (after you promote an llm output)
  scaffold [id ...]          create missing per-item local guides from config+index (clobber-safe)
  graph                      print the dependency model

Driven by `pipeline.yaml` (found by walking up from the cwd). See
examples/pipeline.example.yaml for the schema.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

PIPELINE_NAME = "pipeline.yaml"

STAGES = {
    "inputs":   {"owner": "code", "per_item": False},
    "assemble": {"owner": "code", "per_item": True},
    "spec":     {"owner": "llm",  "per_item": True},
    "draft":    {"owner": "llm",  "per_item": True},
    "critique": {"owner": "llm",  "per_item": True},
    "ladder":   {"owner": "llm",  "per_item": False},
    "lint":     {"owner": "code", "per_item": False},
    "render":   {"owner": "code", "per_item": False},
    "assimilate": {"owner": "llm", "per_item": False},
    "digest":   {"owner": "llm",  "per_item": False},
}


# --------------------------------------------------------------- config (pipeline.yaml)
def _yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("ruamel.yaml missing — run via the launcher (scripts/bizconnect.py "
                 "bootstraps the central-store venv).")
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    return y


class Cfg:
    def __init__(self, start=None):
        d = Path(start or Path.cwd()).resolve()
        f = next((c / PIPELINE_NAME for c in [d, *d.parents] if (c / PIPELINE_NAME).exists()), None)
        if not f:
            sys.exit("no %s found (searched up from %s).\n"
                     "Create one — see examples/pipeline.example.yaml." % (PIPELINE_NAME, d))
        with open(f, encoding="utf-8") as fh:
            self.d = _yaml().load(fh) or {}
        self.root = f.parent
        self.file = f

    def g(self, dotted, default=None):
        cur = self.d
        for p in dotted.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    # path helpers -----------------------------------------------------------
    def ap(self, rel):
        return (self.root / rel)

    def relkey(self, p):
        return str(Path(p).resolve().relative_to(self.root)).replace("\\", "/")

    def read(self, rel):
        p = self.ap(rel)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def sha(self, rel):
        p = self.ap(rel)
        return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.exists() else None

    def globrel(self, reldir, pat="*.md"):
        base = self.ap(reldir)
        if not base.exists():
            return []
        return sorted(self.relkey(p) for p in base.glob(pat))

    # config-named locations -------------------------------------------------
    def loc(self, key, default=None):
        return self.g("paths." + key, default)

    @property
    def build_dir(self):
        return self.loc("build_dir", "build")


# --------------------------------------------------------------- items
def pad_id(rawid, pad):
    m = re.search(r"(\d+)$", str(rawid))
    if pad and m:
        return "%s%0*d" % (rawid[:m.start()], pad, int(m.group(1)))
    return str(rawid)


def load_items(cfg):
    src = cfg.g("items.source")
    if not src:
        sys.exit("pipeline.yaml: items.source is required")
    data = json.loads(cfg.read(src) or "{}")
    lk = cfg.g("items.list_key", "items")
    lst = data.get(lk, data if isinstance(data, list) else [])
    idk = cfg.g("items.id_key", "id")
    tk = cfg.g("items.text_key", "text")
    pad = cfg.g("items.pad", 2)
    out = []
    for it in lst:
        rid = str(it[idk])
        out.append({"raw": rid, "pad": pad_id(rid, pad), "text": it.get(tk, "")})
    return out


def items_map(cfg):  # raw id -> item
    return {it["raw"]: it for it in load_items(cfg)}


def pad_to_raw(cfg):
    return {it["pad"]: it["raw"] for it in load_items(cfg)}


# --------------------------------------------------------------- DAG definition
def inputs_for(cfg, stage, pid=None):
    L = cfg.loc("local_dir", "context/items")
    P = cfg.loc("prompts_dir", "prompts")
    A = cfg.loc("answers_dir", "answers")
    G = cfg.loc("global_context", "context/global.md")
    B = cfg.build_dir
    idx = cfg.g("index.source")
    items_src = cfg.g("items.source")
    R = cfg.loc("register")
    reg = [R] if R else []
    FB = "%s/feedback.bundle.md" % cfg.loc("feedback_dir", "%s/feedback" % B)
    if stage == "inputs":
        c = "connections.yaml"
        return [c] if (cfg.root / c).exists() else []
    if stage == "assemble":
        return [G, "%s/%s.md" % (L, pid)] + ([idx] if idx else []) + [items_src]
    if stage == "spec":
        return ["%s/spec.md" % P, G, items_src] + ([idx] if idx else []) + ["%s/%s.context.md" % (B, pid)] + reg
    if stage == "draft":
        return ["%s/draft.md" % P, G, "%s/%s.md" % (L, pid), "%s/%s.context.md" % (B, pid)] + reg
    if stage == "critique":
        return ["%s/critique.md" % P, G, "%s/%s.md" % (L, pid), "%s/%s.md" % (A, pid)] + reg
    if stage == "ladder":
        intro = cfg.loc("intro")
        fmt = cfg.loc("front_matter_template")
        extra = ([intro] if intro else []) + ([fmt] if fmt else [])
        return ["%s/ladder.md" % P, G] + extra + cfg.globrel(A) + reg
    if stage == "lint":
        return cfg.globrel(A) + cfg.globrel(L) + ([idx] if idx else []) + [items_src, G] + reg
    if stage == "render":
        sub = cfg.loc("submission")
        return cfg.globrel(A) + ([sub] if sub else [])
    if stage == "assimilate":
        return (["%s/assimilate.md" % P, G, FB] + cfg.globrel(A) + cfg.globrel(L)
                + ["%s/draft.md" % P, "%s/spec.md" % P, items_src] + reg)
    if stage == "digest":
        return ["%s/digest.md" % P, G] + reg
    return []


def output_for(cfg, stage, pid=None):
    L = cfg.loc("local_dir", "context/items")
    A = cfg.loc("answers_dir", "answers")
    B = cfg.build_dir
    FB = cfg.loc("feedback_dir", "%s/feedback" % B)
    return {
        "inputs":   "%s/inputs.lock.json" % B,
        "assemble": "%s/%s.context.md" % (B, pid),
        "spec":     "%s/%s.md" % (L, pid),
        "draft":    "%s/%s.md" % (A, pid),
        "critique": "%s/%s.critique.gen.md" % (B, pid),
        "ladder":   cfg.loc("submission", "%s/submission.md" % B),
        "lint":     "%s/lint-report.md" % B,
        "render":   cfg.loc("final", "final/document.md"),
        "assimilate": "%s/cycle.gen.md" % FB,
        "digest":   cfg.loc("brief", "%s/brief.gen.md" % FB),
    }[stage]


def canonical_for(cfg, stage, pid=None):
    L = cfg.loc("local_dir", "context/items")
    A = cfg.loc("answers_dir", "answers")
    return {"spec": "%s/%s.md" % (L, pid), "draft": "%s/%s.md" % (A, pid),
            "ladder": cfg.loc("submission"), "digest": cfg.loc("brief")}.get(stage)


def gen_for(cfg, stage, pid=None):
    B = cfg.build_dir
    FB = cfg.loc("feedback_dir", "%s/feedback" % B)
    return {"spec": "%s/%s.spec.gen.md" % (B, pid), "draft": "%s/%s.draft.gen.md" % (B, pid),
            "critique": "%s/%s.critique.gen.md" % (B, pid),
            "ladder": "%s/submission.gen.md" % B,
            "assimilate": "%s/cycle.gen.md" % FB,
            "digest": "%s/brief.gen.md" % FB}.get(stage)


def tid(stage, pid=None):
    return "%s:%s" % (stage, pid) if pid else stage


def all_targets(cfg):
    out = []
    pids = [it["pad"] for it in load_items(cfg)]
    for stage, c in STAGES.items():
        out += [(stage, p) for p in pids] if c["per_item"] else [(stage, None)]
    return out


# --------------------------------------------------------------- manifest / staleness
def manifest_path(cfg):
    return cfg.ap("%s/manifest.json" % cfg.build_dir)


def load_manifest(cfg):
    p = manifest_path(cfg)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_manifest(cfg, m):
    cfg.ap(cfg.build_dir).mkdir(parents=True, exist_ok=True)
    manifest_path(cfg).write_text(json.dumps(m, indent=2), encoding="utf-8")


def current_inputs(cfg, stage, pid):
    return {i: cfg.sha(i) for i in inputs_for(cfg, stage, pid)}


def state(cfg, stage, pid, man):
    out = output_for(cfg, stage, pid)
    if cfg.sha(out) is None:
        return "MISSING", "output %s not built" % out
    rec = man.get(tid(stage, pid), {}).get("inputs")
    cur = current_inputs(cfg, stage, pid)
    if rec != cur:
        if rec is None:
            return "STALE", "no recorded build"
        changed = [k for k in set(rec) | set(cur) if rec.get(k) != cur.get(k)]
        return "STALE", "inputs changed: " + ", ".join(changed[:4]) + ("…" if len(changed) > 4 else "")
    return "FRESH", ""


# --------------------------------------------------------------- evidence pack (code)
def _pretty(k):
    s = re.sub(r"(?<!^)(?=[A-Z])", " ", str(k)).replace("_", " ")
    return s[:1].upper() + s[1:]


def _evidence_ids(mapval):
    if isinstance(mapval, list):
        return mapval
    if isinstance(mapval, dict):
        if isinstance(mapval.get("all"), list):
            return mapval["all"]
        seen, out = set(), []
        for v in mapval.values():
            if isinstance(v, list):
                for x in v:
                    if x not in seen:
                        seen.add(x); out.append(x)
        return out
    return []


def evidence_pack(cfg, raw, pid):
    idx_src = cfg.g("index.source")
    item = items_map(cfg).get(raw, {"text": ""})
    L = ["# Evidence pack — %s" % raw, "", "> %s" % item.get("text", ""), ""]
    if not idx_src:
        L += ["_(no index configured)_", ""]
        return "\n".join(L)
    idx = json.loads(cfg.read(idx_src) or "{}")
    idk = cfg.g("index.entry_id_key", "id")
    entries = {e.get(idk): e for e in idx.get(cfg.g("index.entries_key", "entries"), [])}
    imap = idx.get(cfg.g("index.item_map_key", "item_map"), {})
    ids = _evidence_ids(imap.get(raw))
    L += ["_Deterministically assembled from %s. Read with the global guide and this item's "
          "local guide._" % idx_src, ""]
    if not ids:
        L += ["_(no evidence mapped to this item)_", ""]
    for eid in ids:
        e = entries.get(eid)
        if not e:
            L += ["### `%s` _(not in index)_" % eid, ""]
            continue
        L += ["### %s" % (e.get("title") or eid), "`%s`" % eid, ""]
        for k, v in e.items():
            if k in (idk, "title"):
                continue
            if isinstance(v, str) and v.strip():
                L += ["**%s.** %s" % (_pretty(k), v.strip()), ""]
            elif isinstance(v, list) and v:
                L.append("**%s**" % _pretty(k))
                L += ["- %s" % x for x in v]
                L.append("")
    return "\n".join(L)


# --------------------------------------------------------------- prompt assembly (llm)
def _fill(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{{%s}}" % k, v)
    return text


def _open_points(cfg, match, label):
    """OPEN points (not resolved/closed) whose `questions:` line satisfies `match`,
    sliced from the register projection — what the next turn must address."""
    reg = cfg.loc("register")
    txt = cfg.read(reg) if reg else None
    if not txt:
        return "(no open points — none captured yet)"
    if "<!-- BEGIN REGISTER" in txt and "<!-- END REGISTER -->" in txt:
        txt = txt.split("<!-- BEGIN REGISTER", 1)[1].split("<!-- END REGISTER -->", 1)[0]
    OPEN = ("open", "triaged", "in-discussion", "agreed", "actioned")
    out = []
    for part in re.split(r"(?m)^### ", txt)[1:]:
        block = "### " + part.strip()
        mq = re.search(r"questions:.*", block)
        if mq and not match(mq.group(0)):
            continue
        ms = re.search(r"status:\s*([\w-]+)", block)
        if ms and ms.group(1) not in OPEN:
            continue
        out.append(block)
    return "\n\n".join(out) if out else "(no open points for %s)" % label


def open_points_for(cfg, raw, pid):
    """Per-item open points — what the next spec/draft/critique turn must address."""
    toks = [raw] + ([pid] if pid else [])
    return _open_points(cfg, lambda q: any(t in q for t in toks), raw)


def assemble_prompt(cfg, stage, raw, pid):
    tmpl = cfg.read("%s/%s.md" % (cfg.loc("prompts_dir", "prompts"), stage)) or ""
    L = cfg.loc("local_dir", "context/items")
    A = cfg.loc("answers_dir", "answers")
    P = cfg.loc("prompts_dir", "prompts")
    B = cfg.build_dir
    m = {"GLOBAL_CONTEXT": cfg.read(cfg.loc("global_context", "")) or ""}
    if pid:
        item = items_map(cfg).get(raw, {"text": ""})
        m["ITEM_ID"] = raw
        m["ITEM_TEXT"] = item.get("text", "")
        m["EVIDENCE"] = cfg.read("%s/%s.context.md" % (B, pid)) or ""
        m["LOCAL_CONTEXT"] = cfg.read("%s/%s.md" % (L, pid)) or ""
        m["OUTPUT"] = cfg.read("%s/%s.md" % (A, pid)) or "(no answer drafted yet)"
    if stage == "ladder":
        answers = cfg.globrel(A)
        m["ALL_OUTPUTS"] = "\n\n---\n\n".join(cfg.read(a) or "" for a in answers) or "(no answers yet)"
        intro = cfg.loc("intro")
        m["INTRO"] = (cfg.read(intro) if intro else None) or "(no intro material configured)"
        fmt = cfg.loc("front_matter_template")
        m["TEMPLATE"] = (cfg.read(fmt) if fmt else None) or "(no front-matter template configured)"
        m["OPEN_POINTS"] = _open_points(cfg, lambda q: "front-matter" in q, "front-matter")
    if stage in ("spec", "draft", "critique"):
        m["OPEN_POINTS"] = open_points_for(cfg, raw, pid)
    if stage == "assimilate":
        FB = cfg.loc("feedback_dir", "%s/feedback" % B)
        m["FEEDBACK"] = (cfg.read("%s/feedback.bundle.md" % FB)
                         or "(no feedback bundle — capture it with `gdoc comments`/`gdoc diff` first)")
        m["ANSWERS"] = "\n\n---\n\n".join(cfg.read(a) or "" for a in cfg.globrel(A)) or "(no answers yet)"
        m["SPECS"] = "\n\n---\n\n".join(cfg.read(s) or "" for s in cfg.globrel(L)) or "(no specs yet)"
        m["PROMPTS"] = "### draft.md\n%s\n\n### spec.md\n%s" % (
            cfg.read("%s/draft.md" % P) or "", cfg.read("%s/spec.md" % P) or "")
        m["REGISTER"] = cfg.read(cfg.loc("register", "")) or "(register empty)"
    if stage == "digest":
        m["REGISTER"] = cfg.read(cfg.loc("register", "")) or "(register empty)"
    return _fill(tmpl, m)


# --------------------------------------------------------------- code stages
def run_inputs(cfg):
    """Sync external inputs declared in connections.yaml `inputs:` to their local
    Markdown copies, READ-ONLY (we never write back to the source). Idempotent:
    only rewrites a file whose content changed. Currently supports type: gdoc."""
    from .. import config as _conn, _google
    from .gdocs import _doc_id_from
    data, _p = _conn.load_connections(start=cfg.root)
    inputs = (data or {}).get("inputs") or {}
    cfg.ap(cfg.build_dir).mkdir(parents=True, exist_ok=True)
    if not inputs:
        cfg.ap("%s/inputs.lock.json" % cfg.build_dir).write_text("{}", encoding="utf-8")
        return "no inputs declared in connections.yaml"
    subject = _google.impersonation_subject(data)
    drive, synced, refreshed, lock = None, 0, 0, {}
    for handle, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        typ, dest, ref = spec.get("type", "gdoc"), spec.get("extract_to"), spec.get("doc_id") or spec.get("url")
        if not dest or not ref:
            lock[handle] = {"skipped": "missing extract_to or url"}; continue
        if typ != "gdoc":
            lock[handle] = {"skipped": "type %r not supported yet" % typ}; continue
        if drive is None:
            drive = _google.build("drive", "v3", [_google.DRIVE], subject=subject)
        content = drive.files().export(fileId=spec.get("doc_id") or _doc_id_from(ref),
                                       mimeType="text/markdown").execute()
        if isinstance(content, str):
            content = content.encode("utf-8")
        out = cfg.ap(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        changed = (not out.exists()) or out.read_bytes() != content
        if changed:
            out.write_bytes(content); refreshed += 1
        synced += 1
        lock[handle] = {"extract_to": dest, "sha": hashlib.sha1(content).hexdigest()[:12], "refreshed": changed}
    cfg.ap("%s/inputs.lock.json" % cfg.build_dir).write_text(json.dumps(lock, indent=2), encoding="utf-8")
    return "inputs: %d synced, %d refreshed (read-only)" % (synced, refreshed)


def run_assemble(cfg, raw, pid):
    cfg.ap(cfg.build_dir).mkdir(parents=True, exist_ok=True)
    cfg.ap("%s/%s.context.md" % (cfg.build_dir, pid)).write_text(evidence_pack(cfg, raw, pid), encoding="utf-8")
    return "wrote %s/%s.context.md" % (cfg.build_dir, pid)


def run_lint(cfg):
    cfg.ap(cfg.build_dir).mkdir(parents=True, exist_ok=True)
    A = cfg.loc("answers_dir", "answers")
    L = cfg.loc("local_dir", "context/items")
    markers = cfg.g("markers", ["[VERIFY", "[DECISION", "[RECONCILE"])
    prov = cfg.g("provenance")
    body, problems = [], 0
    for it in load_items(cfg):
        pid, raw = it["pad"], it["raw"]
        ans = cfg.read("%s/%s.md" % (A, pid))
        spec = cfg.read("%s/%s.md" % (L, pid)) or ""
        line = "## %s" % raw
        if ans is None:
            line += " — answer NOT drafted"; problems += 1
        body.append(line)
        st = next((l.split(":", 1)[1].split("#")[0].strip()
                   for l in spec.splitlines() if l.startswith("status:")), "?")
        body.append("- guide status: %s" % st)
        if ans:
            for mk in markers:
                n = ans.count(mk)
                if n:
                    body.append("- %d unresolved `%s…`" % (n, mk)); problems += 1
            if prov and prov not in ans:
                body.append("- ⚠ no `%s…` provenance citations" % prov); problems += 1
            body.append("- %d words" % len(ans.split()))
        body.append("")
    reg = cfg.loc("register")
    regtxt = cfg.read(reg) if reg else None
    if regtxt:
        gen = regtxt
        if "<!-- BEGIN REGISTER" in regtxt and "<!-- END REGISTER -->" in regtxt:
            gen = regtxt.split("<!-- BEGIN REGISTER", 1)[1].split("<!-- END REGISTER -->", 1)[0]
        reg_ids = set(re.findall(r"\bISS-\d+\b", gen))
        intext = set()
        for it in load_items(cfg):
            for f in ("%s/%s.md" % (A, it["pad"]), "%s/%s.md" % (L, it["pad"])):
                intext |= set(re.findall(r"\bISS-\d+\b", cfg.read(f) or ""))
        body.append("## register <-> markers")
        for i in sorted(reg_ids - intext):
            body.append("- %s open in register but no in-text marker" % i); problems += 1
        for i in sorted(intext - reg_ids):
            body.append("- marker %s in text but not in register" % i); problems += 1
        if reg_ids and reg_ids == intext:
            body.append("- in sync (%d id(s))" % len(reg_ids))
        body.append("")
    sub = cfg.loc("submission")
    if sub:
        body.append("## %s: %s" % (sub, "present" if cfg.read(sub) else "NOT written"))
    out = ["# Lint report", "", "**%d issue(s) found.**" % problems, ""] + body
    cfg.ap("%s/lint-report.md" % cfg.build_dir).write_text("\n".join(out), encoding="utf-8")
    return "wrote %s/lint-report.md (%d issue(s))" % (cfg.build_dir, problems)


def run_render(cfg):
    final = cfg.loc("final", "final/document.md")
    cfg.ap(final).parent.mkdir(parents=True, exist_ok=True)
    A = cfg.loc("answers_dir", "answers")
    parts = [cfg.g("title", "# Document"), ""]
    sub = cfg.loc("submission")
    if sub and cfg.read(sub):
        parts += [cfg.read(sub), "", "---", ""]
    for it in load_items(cfg):
        a = cfg.read("%s/%s.md" % (A, it["pad"]))
        if a:
            parts += [a, "", "---", ""]
    cfg.ap(final).write_text("\n".join(parts), encoding="utf-8")
    return "wrote %s" % final


def run_scaffold(cfg, only):
    L = cfg.loc("local_dir", "context/items")
    cfg.ap(L).mkdir(parents=True, exist_ok=True)
    idx_src = cfg.g("index.source")
    entries, imap = {}, {}
    if idx_src:
        idx = json.loads(cfg.read(idx_src) or "{}")
        idk = cfg.g("index.entry_id_key", "id")
        entries = {e.get(idk): e for e in idx.get(cfg.g("index.entries_key", "entries"), [])}
        imap = idx.get(cfg.g("index.item_map_key", "item_map"), {})
    created, skipped = 0, 0
    for it in load_items(cfg):
        if only and it["raw"] not in only and it["pad"] not in only:
            continue
        out = cfg.ap("%s/%s.md" % (L, it["pad"]))
        if out.exists():
            skipped += 1
            continue
        ids = _evidence_ids(imap.get(it["raw"]))
        ev = ["- [ ] `%s` — %s" % (e, (entries.get(e, {}).get("title") or "")) for e in ids] or ["- _(none mapped)_"]
        body = ["---", "item: %s" % it["raw"], "status: todo            # todo | in-progress | ready",
                "last_reviewed:", "evidence: %s" % json.dumps(ids), "---", "",
                "# %s" % it["raw"], "", "> %s" % it["text"], "",
                "_Local guide. Fill the sections, or run `bizconnect compose run spec %s` for an "
                "agent first draft, then merge here._" % it["raw"], "",
                "## Summary", "", "## Key points", "- ", "", "## Evidence to use"] + ev + [
                "", "## Counterpoints", "- ", "", "## Notes", "- ", ""]
        out.write_text("\n".join(body), encoding="utf-8")
        created += 1
    return "scaffold: created %d, skipped %d (existing)" % (created, skipped)


# --------------------------------------------------------------- verbs
def _expand(cfg, stage, arg):
    if not STAGES[stage]["per_item"]:
        return [(None, None)]
    items = load_items(cfg)
    if arg in (None, "all"):
        return [(it["raw"], it["pad"]) for it in items]
    p2r = {it["pad"]: it["raw"] for it in items}
    r2p = {it["raw"]: it["pad"] for it in items}
    if arg in r2p:
        return [(arg, r2p[arg])]
    if arg in p2r:
        return [(p2r[arg], arg)]
    # tolerate unpadded/padded numeric forms
    pad = cfg.g("items.pad", 2)
    cand = pad_id(arg, pad)
    if cand in p2r:
        return [(p2r[cand], cand)]
    sys.exit("unknown item %r" % arg)


def cmd_status(cfg, args):
    man = load_manifest(cfg)
    filt = set(args)
    icon = {"FRESH": "✓", "STALE": "~", "MISSING": "·"}
    cur = None
    for stage, pid in all_targets(cfg):
        t = tid(stage, pid)
        if filt and t not in filt and stage not in filt:
            continue
        if stage != cur:
            print("\n%s  [%s]" % (stage, STAGES[stage]["owner"])); cur = stage
        st, why = state(cfg, stage, pid, man)
        print("  %s %-16s %-8s %s" % (icon[st], t, st, why))
    print()


def cmd_run(cfg, args):
    if not args:
        sys.exit("usage: compose run <stage> [id|all]")
    stage = args[0]
    if stage not in STAGES:
        sys.exit("unknown stage %r (one of: %s)" % (stage, ", ".join(STAGES)))
    man = load_manifest(cfg)
    for raw, pid in _expand(cfg, stage, args[1] if len(args) > 1 else None):
        if STAGES[stage]["owner"] == "code":
            msg = {"inputs": lambda: run_inputs(cfg),
                   "assemble": lambda: run_assemble(cfg, raw, pid),
                   "lint": lambda: run_lint(cfg), "render": lambda: run_render(cfg)}[stage]()
            man[tid(stage, pid)] = {"inputs": current_inputs(cfg, stage, pid)}
            print("[code] %s — %s" % (tid(stage, pid), msg))
        else:
            if stage in ("spec", "draft") and cfg.sha("%s/%s.context.md" % (cfg.build_dir, pid)) is None:
                run_assemble(cfg, raw, pid)
                man[tid("assemble", pid)] = {"inputs": current_inputs(cfg, "assemble", pid)}
            cfg.ap(cfg.build_dir).mkdir(parents=True, exist_ok=True)
            pf = "%s/%s.%s.prompt.md" % (cfg.build_dir, pid or "submission", stage)
            cfg.ap(pf).write_text(assemble_prompt(cfg, stage, raw, pid), encoding="utf-8")
            print("[llm]  %s — prompt ready: %s" % (tid(stage, pid), pf))
            print("        run it with an agent; save output to: %s" % gen_for(cfg, stage, pid))
            canon = canonical_for(cfg, stage, pid)
            if canon:
                print("        then promote/merge into: %s   (never overwritten by compose)" % canon)
            print("        then record: bizconnect compose accept %s %s" % (stage, raw or ""))
    save_manifest(cfg, man)


def cmd_accept(cfg, args):
    if not args:
        sys.exit("usage: compose accept <stage> [id|all]")
    stage = args[0]
    man = load_manifest(cfg)
    for raw, pid in _expand(cfg, stage, args[1] if len(args) > 1 else None):
        out = output_for(cfg, stage, pid)
        if cfg.sha(out) is None:
            print("  ! %s — output %s missing; not accepted" % (tid(stage, pid), out)); continue
        man[tid(stage, pid)] = {"inputs": current_inputs(cfg, stage, pid)}
        print("  ✓ accepted %s" % tid(stage, pid))
    save_manifest(cfg, man)


def cmd_scaffold(cfg, args):
    print(run_scaffold(cfg, set(args)))


def cmd_graph(cfg, _args):
    print("config: %s\n" % cfg.file)
    print("targets <- inputs  (per-item targets shown as <id>):")
    aggregate = {"ladder", "lint", "render"}
    for stage in STAGES:
        pid = "<id>" if STAGES[stage]["per_item"] else None
        ins = inputs_for(cfg, stage, pid)
        if stage in aggregate:                       # hide the expanded answer glob; show a note
            ins = [i for i in ins if not i.startswith(cfg.loc("answers_dir", "answers") + "/")]
            ins.append("(all answers)")
        print("  %-14s [%s] <- %s" % (tid(stage, pid), STAGES[stage]["owner"], ", ".join(ins)))


VERBS = {"status": cmd_status, "run": cmd_run, "accept": cmd_accept,
         "scaffold": cmd_scaffold, "graph": cmd_graph}


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if not fn:
        sys.exit("unknown compose verb %r. One of: %s" % (verb, ", ".join(VERBS)))
    fn(Cfg(), rest)
    return 0
