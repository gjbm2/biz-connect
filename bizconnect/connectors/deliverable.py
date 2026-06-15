"""bizconnect.deliverable — stand up a new deliverable in an umbrella repo.

An umbrella repo hosts many deliverables (one consultation response / report each) under
`deliverables/<slug>/`, each with its own pipeline.yaml and — bound under
`deliverables.<slug>` in the umbrella connections.yaml — its own Notion register + docs-registry
and output Doc. This connector provisions one:

  deliverable list                       enumerate the repo's deliverables (slug · title · dir)
  deliverable new <slug> [opts]          scaffold deliverables/<slug>/ + a connections.yaml stub,
                                         and (with --hub) mark the Notion hub page as a submission

  opts:  --title "…"        human title for the deliverable (-> pipeline.yaml title)
         --hub <page>       an existing Notion page to use as the submission hub: gets a 📨 icon,
                            a "this is a submission" marker callout, and getting-started links
         --drive-folder ID  a Drive subfolder for this deliverable's Docs (else the umbrella root)

`new` does NOT create the Notion register/docs-registry databases itself — that's `register init`
/ `docreg init`, run from inside the new folder (they now write their binding into
deliverables.<slug>). The `new-submission` skill ties the whole sequence together. There is no
central "submissions" database: the repo (connections.yaml + deliverables/<slug>/) is the registry.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import config

_DIRS = [
    "consultation", "index", "final",
    "response/00.inputs", "response/01.context/questions", "response/02.prompts",
    "response/03.build", "response/04.answers", "response/05.submission", "response/06.register",
]

_PIPELINE_TMPL = """# biz-connect `compose` config for the {slug} deliverable.
# Committed; NO secrets. Run compose/gdoc/register from inside this folder.

title: "# {title}"

# Names this deliverable; the engine scopes connections.yaml (notion.register_db,
# notion.docs_registry, inputs, google.drive_folder) to deliverables.{slug}.
deliverable: {slug}

items:
  source: index/questions.json     # [{{ "id": "Q1", "text": "…" }}, …] under `questions`
  list_key: questions
  id_key: id
  text_key: text
  pad: 2

index:
  source: index/corpus.json        # structured evidence index + question_map (optional but recommended)
  entries_key: entries
  entry_id_key: repoPath
  item_map_key: question_map

paths:
  global_context: response/01.context/house-position.md
  local_dir: response/01.context/questions
  prompts_dir: response/02.prompts
  answers_dir: response/04.answers
  submission: response/05.submission/front-matter.md
  intro: //nous-background.md       # umbrella-shared (`//` = repo root)
  front_matter_template: response/05.submission/front-matter-template.md
  build_dir: response/03.build
  final: final/response.md
  register: response/06.register/open-points.md
  brief: response/06.register/brief.md
  feedback_dir: response/03.build/feedback
  register_journal: response/06.register/journal

markers: ["[VERIFY", "[DECISION", "[RECONCILE", "[Nous team to add", "[Nous "]
provenance: "(src:"
soft_cap_words: 450
"""

_README_TMPL = """# {title}

Deliverable `{slug}` of the nous-reg umbrella workspace. Run builds from inside this folder
(`cd deliverables/{slug}`); see [`response/pipeline.md`](response/pipeline.md) for the operating
manual and the umbrella [`README.md`](../../README.md) for the layering.

To set it up: author `index/questions.json` (the items), the `response/01.context/` guides, the
`response/02.prompts/` templates and `response/05.submission/front-matter-template.md`, then
`compose status`.
"""


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _positional(argv):
    for a in argv:
        if not a.startswith("-"):
            return a
    return None


def cmd_list(argv):
    rows = config.list_deliverables()
    if not rows:
        print("no deliverables found (no deliverables/<slug>/pipeline.yaml under the repo root).")
        return 0
    w = max(len(r["slug"]) for r in rows)
    for r in rows:
        print("  %-*s  %s" % (w, r["slug"], r["title"] or "(untitled)"))
    return 0


def _scaffold(root: Path, slug: str, title: str):
    base = root / "deliverables" / slug
    if base.exists():
        sys.exit("deliverables/%s already exists — refusing to overwrite. "
                 "Pick another slug or finish setting it up by hand." % slug)
    for d in _DIRS:
        (base / d).mkdir(parents=True, exist_ok=True)
    # keep otherwise-empty dirs in git
    for d in ("response/04.answers", "response/05.submission", "response/06.register",
              "index", "final", "consultation"):
        (base / d / ".gitkeep").write_text("", encoding="utf-8")
    (base / "pipeline.yaml").write_text(_PIPELINE_TMPL.format(slug=slug, title=title), encoding="utf-8")
    (base / "response" / "README.md").write_text(_README_TMPL.format(slug=slug, title=title), encoding="utf-8")
    (base / "index" / "questions.json").write_text('{\n  "questions": []\n}\n', encoding="utf-8")
    (base / "index" / "corpus.json").write_text('{\n  "entries": [],\n  "question_map": {}\n}\n', encoding="utf-8")
    return base


def _ensure_connections_stub(data, conn_path, slug, drive_folder=None):
    """Make sure a deliverables.<slug> block exists so the binding is visible; `register init` /
    `docreg init` fill in notion.register_db / notion.docs_registry (scoped) when run from the folder."""
    from ruamel.yaml.comments import CommentedMap
    dl = data.get("deliverables")
    if not isinstance(dl, dict):
        dl = CommentedMap(); data["deliverables"] = dl
    if slug not in dl or not isinstance(dl.get(slug), dict):
        dl[slug] = CommentedMap()
    slot = dl[slug]
    if drive_folder:
        g = slot.get("google")
        if not isinstance(g, dict):
            g = CommentedMap(); slot["google"] = g
        g["drive_folder"] = drive_folder
    config.save_connections(data, conn_path)


def _mark_hub(hub, slug, title, root):
    """Stamp the Notion hub page as a submission: a 📨 icon + a marker callout linking to the
    getting-started + pipeline pages. Human-facing; the repo remains the source of truth."""
    from . import notion
    pid = notion.norm_id(hub)
    notion.api("PATCH", "/pages/%s" % pid, body={"icon": {"type": "emoji", "emoji": "📨"}})
    gs = "https://app.notion.com/p/380e4fd0813681038f32d267666c51c8"   # How to get started
    pp = "https://app.notion.com/p/380e4fd08136814eb928f894d0902263"   # How the pipeline works
    callout = {
        "object": "block", "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "📨"},
            "color": "blue_background",
            "rich_text": [
                {"type": "text", "text": {"content": "This page is an automated-document SUBMISSION "}},
                {"type": "text", "text": {"content": "(%s). " % slug},
                 "annotations": {"bold": True}},
                {"type": "text", "text": {"content": "Its register + docs-registry live below; the build lives in the nous-reg repo under deliverables/%s/. " % slug}},
                {"type": "text", "text": {"content": "Getting started", "link": {"url": gs}}},
                {"type": "text", "text": {"content": " · "}},
                {"type": "text", "text": {"content": "How the pipeline works", "link": {"url": pp}}},
            ],
        },
    }
    notion.attach(pid, [callout])
    return pid


def cmd_new(argv):
    slug = _positional(argv)
    if not slug:
        sys.exit("usage: deliverable new <slug> [--title …] [--hub <page>] [--drive-folder ID]")
    data, conn_path = config.require_connections()
    root = conn_path.parent
    title = _opt(argv, "--title") or slug
    hub = _opt(argv, "--hub")
    drive_folder = _opt(argv, "--drive-folder")

    base = _scaffold(root, slug, title)
    _ensure_connections_stub(data, conn_path, slug, drive_folder)
    print("scaffolded %s" % base.relative_to(root).as_posix())
    print("  wrote pipeline.yaml (deliverable: %s) + response/ tree + connections.yaml deliverables.%s stub" % (slug, slug))

    if hub:
        try:
            pid = _mark_hub(hub, slug, title, root)
            print("  marked Notion hub %s as a submission (📨 icon + callout)" % pid)
        except Exception as e:
            print("  (could not mark Notion hub: %s)" % e)

    print("\nNext — from inside the new folder:")
    print("  cd deliverables/%s" % slug)
    if hub:
        print("  bizconnect register init --parent %s     # open-points DB (binds under deliverables.%s)" % (hub, slug))
        print("  bizconnect docreg   init --parent %s     # docs-registry DB" % hub)
    else:
        print("  bizconnect register init --parent <hub-page>   # open-points DB")
        print("  bizconnect docreg   init --parent <hub-page>   # docs-registry DB")
    print("  # author index/questions.json, response/01.context/ guides, response/02.prompts/ + the")
    print("  # front-matter template, then: bizconnect compose status")
    return 0


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    if verb == "list":
        return cmd_list(rest)
    if verb == "new":
        return cmd_new(rest)
    sys.exit("unknown deliverable verb %r (try: list | new)" % verb)
