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
    spec:<id>       (llm)  the item's argument guide    -> <local_dir>/<id>.md   (an INPUT — see WARNING)
    draft:<id>      (llm)  the item's answer            -> <answers_dir>/<id>.md (an OUTPUT)
    critique:<id>   (llm)  adversarial review           -> <build>/<id>.critique.gen.md
    ladder          (llm)  front matter: template+intro over answers -> <submission> (an OUTPUT)
    lint            (code) completeness / provenance    -> <build>/lint-report.md
    render          (code) assembled document           -> <final>

INPUTS vs OUTPUTS — the build CONSUMES inputs and PRODUCES outputs; know which is which:
    inputs  (human-owned; the build READS them, never writes them): <global_context>,
            <local_dir>/<id>.md (the per-item guides), the items JSON, the corpus index,
            prompts/*, the intro and the front-matter template.
    outputs (the build CREATES them): <answers_dir>/<id>.md, <submission>, <final>.
    scratch: <build>/* — evidence packs, prompts, and *.gen.md proposals (git-ignored).
A "build" = draft -> ladder -> lint -> render -> publish. It only ever CREATES outputs.

WARNING — `spec` is the ONE stage that writes BACKWARD into an input. Its promotion target
<local_dir>/<id>.md is the per-item guide that `draft` later READS (see draft's inputs).
So `spec` is an OPTIONAL, pre-build authoring aid for an *empty* guide, which a human merges
in. A routine build does NOT run `spec` over a guide that already has content, and NEVER
auto-promotes a `spec` gen over an existing guide — that would destroy a human-authored input.
Every other stage promotes FORWARD into an output.

code targets run here (deterministic). llm targets are run by an AGENT: compose assembles
the exact prompt (your template + resolved context) into <build>/, you run it, promote the
result FORWARD into its output file, then record it with `accept`. compose NEVER overwrites
any human-owned file (clobber-safe) — and never writes an input for you.

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
    "harvest":  {"owner": "code", "per_item": False},
    "coherence": {"owner": "llm", "per_item": False},
    "assimilate": {"owner": "llm", "per_item": False},
    "digest":   {"owner": "llm",  "per_item": False},
}

# Stages whose promotion target is itself a downstream INPUT (not a build output). `spec`
# promotes into <local_dir>/<id>.md — the per-item guide that `draft` reads — so promoting it
# REWRITES a human-authored input. Every other stage promotes forward into an output. The
# build must never auto-promote these; `run` warns loudly when one is invoked (see cmd_run).
INPUT_PROMOTION_STAGES = {"spec"}


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
    if stage == "harvest":
        sub = cfg.loc("submission")
        return cfg.globrel(A) + ([sub] if sub else [])
    if stage == "coherence":
        return ["%s/coherence.md" % P, G, cfg.loc("final", "final/document.md")]
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
        "harvest":  "%s/harvest.json" % FB,
        "coherence": "%s/coherence.gen.md" % B,
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
            "coherence": "%s/coherence.gen.md" % B,
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
    if stage == "coherence":
        m["DOCUMENT"] = (cfg.read(cfg.loc("final", "final/document.md"))
                         or "(no rendered document yet — run render first)")
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
        if typ == "notion":
            from .notion import sync_to_dir
            summary = sync_to_dir(ref, cfg.ap(dest), exclude=spec.get("exclude"),
                                  max_depth=int(spec.get("max_depth", 3)),
                                  download_files=bool(spec.get("download_files", True)),
                                  follow_links=bool(spec.get("follow_links", True)),
                                  catalog_links=bool(spec.get("catalog_links", True)))
            synced += 1
            if summary.get("refreshed"):
                refreshed += 1
            lock[handle] = {"extract_to": dest, "type": "notion", **summary}
            continue
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


# --------------------------------------------------------- body cleaning / open points
# An answer / front-matter file is CLEAN publishable prose followed by an OPTIONAL trailing
# ```open-points YAML block. The body that renders/publishes must carry no open-point markers;
# every open point lives in the block, from where `harvest` lifts it into the register and onto
# the review Doc as a comment. These helpers split, clean, and parse that structure.
OPEN_POINTS_FENCE = "```open-points"
_INLINE_MARKER_RE = re.compile(r"\[(?:VERIFY|DECISION|RECONCILE|UPDATE)\b[^\]]*\]", re.I)


def split_open_points(text):
    """(clean_body, block_text|None). The block is everything from the ```open-points fence to
    the end (with any immediately-preceding 'Open points' heading folded out of the body)."""
    if not text:
        return text or "", None
    idx = text.find(OPEN_POINTS_FENCE)
    if idx == -1:
        return text, None
    body = re.sub(r"(?:\n)#{1,6}[ \t]*open[ \t-]*points[^\n]*[ \t]*$", "", text[:idx], flags=re.I)
    return body.rstrip() + "\n", text[idx:]


def _strip_inline_markers(text):
    """Belt-and-braces: drop any stray inline [VERIFY/DECISION/RECONCILE/UPDATE: …] aside the
    prompts already forbid, and tidy the whitespace it leaves, so a residual never publishes."""
    if not text:
        return text
    text = _INLINE_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_for_render(text):
    body, _ = split_open_points(text)
    return _strip_inline_markers(body).rstrip()


def inject_question(answer_text, qtext):
    """Place the source question as a subhead directly under the answer's first heading."""
    if not qtext:
        return answer_text
    lines = answer_text.split("\n")
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#"):
            if i + 1 < len(lines) and "question." in lines[i + 1].lower():
                return answer_text
            lines[i + 1:i + 1] = ["", "> **Ofgem's question.** %s" % qtext.strip()]
            return "\n".join(lines)
    return "> **Ofgem's question.** %s\n\n%s" % (qtext.strip(), answer_text)


def parse_open_points(text, qid):
    """Parse the trailing ```open-points YAML list into dicts (tolerant: [] if absent/bad)."""
    _, block = split_open_points(text)
    if not block:
        return []
    m = re.search(r"```open-points[ \t]*\n(.*?)```", block, re.S)
    if not m:
        return []
    try:
        import io
        from ruamel.yaml import YAML
        data = YAML(typ="safe").load(io.StringIO(m.group(1)))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        if isinstance(e, dict):
            e = dict(e)
            e.setdefault("question", qid)
            out.append(e)
    return out


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
            clean, _block = split_open_points(ans)
            inline = sum(clean.count(mk) for mk in markers)
            if inline:
                body.append("- ✗ %d inline marker(s) in body — must be 0; open points belong in "
                            "the open-points block / register, not the prose" % inline); problems += 1
            ops = parse_open_points(ans, raw)
            if ops:
                body.append("- %d open point(s) in block (harvested to register)" % len(ops))
            if prov and prov not in clean:
                body.append("- ⚠ no `%s…` provenance citations" % prov); problems += 1
            wc = len(clean.split())
            soft = cfg.g("soft_cap_words")
            note = "  ⚠ long — review for concision" if (soft and wc > int(soft)) else ""
            body.append("- %d words%s" % (wc, note))
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
        # Clean-body model: open points live in the register + each answer's open-points block,
        # NOT as inline [DECISION: ISS-nnn] markers. So a register id with no in-text marker is
        # normal (not an issue). Only an in-text ISS that has NO register row is a real error.
        for i in sorted(intext - reg_ids):
            body.append("- marker %s in text but not in register" % i); problems += 1
        body.append("- register: %d open point(s); %d in-text ISS marker(s)" % (len(reg_ids), len(intext)))
        body.append("")
    sub = cfg.loc("submission")
    if sub:
        body.append("## %s: %s" % (sub, "present" if cfg.read(sub) else "NOT written"))
    out = ["# Lint report", "", "**%d issue(s) found.**" % problems, ""] + body
    cfg.ap("%s/lint-report.md" % cfg.build_dir).write_text("\n".join(out), encoding="utf-8")
    return "wrote %s/lint-report.md (%d issue(s))" % (cfg.build_dir, problems)


def run_render(cfg):
    """Assemble the final document from CLEAN bodies only: each answer/front-matter file has its
    trailing open-points block and any stray inline markers stripped, and each answer gets the
    source question injected as a subhead under its heading."""
    final = cfg.loc("final", "final/document.md")
    cfg.ap(final).parent.mkdir(parents=True, exist_ok=True)
    A = cfg.loc("answers_dir", "answers")
    parts = [cfg.g("title", "# Document"), ""]
    sub = cfg.loc("submission")
    if sub and cfg.read(sub):
        parts += [clean_for_render(cfg.read(sub)), "", "---", ""]
    for it in load_items(cfg):
        a = cfg.read("%s/%s.md" % (A, it["pad"]))
        if a:
            parts += [inject_question(clean_for_render(a), it.get("text", "")), "", "---", ""]
    cfg.ap(final).write_text("\n".join(parts), encoding="utf-8")
    return "wrote %s" % final


# --------------------------------------------------- harvest (open points -> register)
# `harvest` is the build's bridge into the open-points register: it parses every answer's (and
# the front matter's) trailing open-points block, allocates a stable ISS id per point via the
# register, and writes <feedback>/harvest.json (the point list + anchors) for the publish step
# to post as Doc comments. It is the build-side analogue of `assimilate` (reviewer feedback).
_KIND_DISP = {"verify": "research", "decision": "discussion", "reconcile": "rethink"}


def _norm_point(op, qid, target):
    kind = (str(op.get("kind", "verify")).strip().lower() or "verify")
    anchor = str(op.get("anchor", "")).strip()
    note = str(op.get("note", "")).strip()
    disp = str(op.get("disposition") or _KIND_DISP.get(kind, "research")).strip().lower()
    layer = str(op.get("layer") or ("front-matter" if qid == "front-matter" else "answer")).strip()
    iss = str(op.get("iss") or "").strip()
    # STABLE IDENTITY across revisions: once a point has been assigned an ISS, the draft carries
    # that `iss:` forward in its open-points block, and we key dedupe on it — so a reworded prose
    # (different anchor) updates the SAME register row instead of spawning a twin. Only a brand-new
    # point (no ISS yet) falls back to an anchor-hash id, which upsert then promotes to a fresh ISS.
    sig = hashlib.sha1(("%s|%s|%s" % (qid, kind, anchor)).encode("utf-8")).hexdigest()[:8]
    return {"question": qid, "kind": kind, "anchor": anchor, "note": note,
            "disposition": disp, "layer": layer, "iss": iss,
            "src_id": iss if iss else "build:%s:%s:%s" % (qid, kind, sig), "target": target}


def _harvest_points(cfg):
    A = cfg.loc("answers_dir", "answers")
    pts = []
    for it in load_items(cfg):
        for op in parse_open_points(cfg.read("%s/%s.md" % (A, it["pad"])), it["raw"]):
            pts.append(_norm_point(op, it["raw"], "%s/%s.md" % (A, it["pad"])))
    sub = cfg.loc("submission")
    if sub:
        for op in parse_open_points(cfg.read(sub), "front-matter"):
            pts.append(_norm_point(op, "front-matter", sub))
    return pts


def _point_to_delta(p):
    row = {
        "title": "%s — %s" % (p["question"], (p["note"][:60] or p["kind"])),
        "questions": [p["question"]],
        "disposition": p["disposition"],
        "layer": p["layer"],
        "targets": p["target"],
        "marker": "[%s] %s" % (p["kind"].upper(), p["note"][:120]),
        "author": "build",
        "source_comment_id": p["src_id"],
        "input": 'Build-generated %s point on %s. Anchored to: "%s". %s' % (
            p["kind"], p["question"], p["anchor"], p["note"]),
        "interpretation": p["note"],
        "history_append": "raised by build harvest",
    }
    if p["iss"]:
        row["iss"] = p["iss"]
    return row


def run_harvest(cfg):
    """Lift every open point from the answers + front matter into the register, and write
    <feedback>/harvest.json for publish. Never fails the build: if the register/Notion is
    unavailable the points are still written locally and flagged."""
    fb = cfg.loc("feedback_dir", "%s/feedback" % cfg.build_dir)
    cfg.ap(fb).mkdir(parents=True, exist_ok=True)
    pts = _harvest_points(cfg)
    dpath = cfg.ap("%s/harvest.deltas.json" % fb)
    dpath.write_text(json.dumps([_point_to_delta(p) for p in pts], indent=2, ensure_ascii=False),
                     encoding="utf-8")
    reg_status = "register not configured (points written locally only)"
    if cfg.loc("register"):
        if not pts:
            reg_status = "no open points to upsert"
        else:
            try:
                from . import register
                register.cmd_upsert([str(dpath)])
                iss_map = register.source_id_to_iss()
                for p in pts:
                    p["iss"] = iss_map.get(p["src_id"], p.get("iss") or "")
                reg_status = "upserted %d point(s) to register" % len(pts)
            except SystemExit as e:
                reg_status = "register upsert skipped (%s)" % (str(e) or "no register / Notion")
            except Exception as e:                      # never fail the build on harvest
                reg_status = "register upsert error: %s" % e
    cfg.ap("%s/harvest.json" % fb).write_text(json.dumps(pts, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    return "%d open point(s) harvested; %s" % (len(pts), reg_status)


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
                "_Local guide — a build INPUT: the `draft` stage READS this file. Fill the sections "
                "by hand, or run `bizconnect compose run spec %s` to get an agent-proposed draft in "
                "build/ and merge the good parts in. Once it has content the build drafts from it as-is; "
                "`spec` must not overwrite it._" % it["raw"], "",
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
                   "lint": lambda: run_lint(cfg), "render": lambda: run_render(cfg),
                   "harvest": lambda: run_harvest(cfg)}[stage]()
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
            if canon and stage in INPUT_PROMOTION_STAGES:
                nonempty = cfg.sha(canon) is not None and len((cfg.read(canon) or "").strip()) > 0
                print("        ⚠ %s is a build INPUT (the `draft` stage reads it), NOT an output." % canon)
                print("          `spec` only PROPOSES a guide; promoting its gen REWRITES a human input.")
                if nonempty:
                    print("          This guide already HAS content — do not auto-promote. Merge by hand, with sign-off.")
                print("          A routine build skips `spec` and drafts from the existing guide as-is.")
            elif canon:
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
    aggregate = {"ladder", "lint", "render", "harvest"}
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
