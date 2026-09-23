"""Documentation examples must work as written: users and agents copy them verbatim.

- The example notion.yaml in skills/notion-sync/SKILL.md and the one in the notiontree module
  docstring (what `bizconnect notion help` prints) are written into a temp repo, with the files
  and folders they name, and loaded offline: Tree() builds, check_overlaps() passes, and every
  `in:` resolves. Illustrative Notion URLs ("...Hub-<page-id>") are swapped for placeholder ids
  (0123456789abcdef0123456789abcdef + n); no Notion API is called.
- The skills, README and plugin CLAUDE.md use the `bizconnect` command (no `B='python ...'`
  shell-variable shorthand, which fails when run as written), the skills that run it pre-approve
  every launcher form, and generic docs carry no one user's people, companies or repos.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from bizconnect.connectors import notiontree as T

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
SYNC_SKILL = SKILLS / "notion-sync" / "SKILL.md"
PLACEHOLDER = 0x0123456789ABCDEF0123456789ABCDEF


# ------------------------------------------------------------------ extracting the examples
def _skill_manifests():
    """Every ```yaml block in the notion-sync skill that is a notion.yaml (has hub: and map:)."""
    text = SYNC_SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```ya?ml\n(.*?)```", text, re.S)
    return [b for b in blocks if re.search(r"^hub:", b, re.M) and re.search(r"^map:", b, re.M)]


def _docstring_manifest():
    """The indented notion.yaml example in the notiontree module docstring."""
    lines = (T.__doc__ or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"\s+hub:", ln)), None)
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = []
    for ln in lines[start:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) < indent:
            break                                   # prose resumes: the example is over
        out.append(ln[indent:] if ln.strip() else "")
    return "\n".join(out).rstrip() + "\n"


_NOTION_URL = re.compile(r"https?://(?:www\.)?notion\.(?:so|site)/\S+")


def _with_placeholder_ids(text):
    """Swap each Notion URL that carries no real id for a parseable one (a distinct id per URL)."""
    seen = {}

    def sub(m):
        url = m.group(0)
        seg = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        if len(re.sub(r"[^0-9a-fA-F]", "", seg)) >= 32:
            return url
        if url not in seen:
            seen[url] = "https://www.notion.so/Example-%032x" % (PLACEHOLDER + len(seen))
        return seen[url]

    return _NOTION_URL.sub(sub, text)


def _materialise(folder, man):
    """Create the files and folders a manifest names, so every entry has something to map."""
    for e in man.get("map") or []:
        path = str(e.get("path") or "").strip().strip("/")
        if "database" in e or "pages" in e:
            d = folder / path
            d.mkdir(parents=True, exist_ok=True)
            (d / "example-note.md").write_text("---\ndate: 2026-01-01\n---\n# Example note\n\nText.\n",
                                               encoding="utf-8")
        elif re.search(r"[*?\[]", path):
            sample = folder / re.sub(r"\*\*/?", "", path).replace("*", "sample").replace("?", "x")
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b"%PDF-1.4\n")
        else:
            f = folder / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# %s\n\nText.\n" % f.stem, encoding="utf-8")


def _load(tmp_path, monkeypatch, text):
    """Write `text` as projects/x/notion.yaml in a fresh repo and load it the way the sync does."""
    root = tmp_path / "repo"
    proj = root / "projects" / "x"
    proj.mkdir(parents=True)
    (root / "connections.yaml").write_text("notion: {}\n", encoding="utf-8")
    text = _with_placeholder_ids(text)
    (proj / "notion.yaml").write_text(text, encoding="utf-8")
    man = YAML(typ="safe").load(text)
    assert isinstance(man, dict) and isinstance(man.get("map"), list), "not a notion.yaml"
    _materialise(proj, man)
    monkeypatch.chdir(root)
    tree = T.Tree(root, proj / "notion.yaml", dry=True, out=lambda *a: None)
    tree.check_overlaps()
    for key, it in tree.items.items():
        if it["entry"].get("in"):
            tree.container_id(it)                   # `in:` must name a page entry or a URL
    for e in man["map"]:
        p = str(e["path"]).strip().strip("/")
        assert any(k == p or k == p + "/" or fnmatch.fnmatch(k, p) for k in tree.items), \
            "map entry %r produced no item" % p
    return tree


# ------------------------------------------------------------------ the example manifests
def test_skill_has_an_example_manifest():
    assert _skill_manifests(), "no example notion.yaml found in %s" % SYNC_SKILL


@pytest.mark.parametrize("n", range(len(_skill_manifests())))
def test_skill_example_manifest_loads(tmp_path, monkeypatch, n):
    tree = _load(tmp_path, monkeypatch, _skill_manifests()[n])
    # the skill's own `map ... --in X` examples must name a page entry of its example manifest
    for ref in re.findall(r"--in\s+([^\s\]`]+)", SYNC_SKILL.read_text(encoding="utf-8")):
        it = tree.items.get(ref) or tree.items.get(ref.rstrip("/") + "/")
        assert it is not None and it["kind"] == "page", "`--in %s` names no page entry" % ref


def test_docstring_example_manifest_loads(tmp_path, monkeypatch):
    text = _docstring_manifest()
    assert text, "no example notion.yaml found in the notiontree docstring"
    _load(tmp_path, monkeypatch, text)


def test_example_connections_yaml_parses():
    data = YAML(typ="safe").load((REPO / "examples" / "connections.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("notion"), dict)


# ------------------------------------------------------------------ command form + generic docs
def _docs():
    return sorted(SKILLS.glob("*/SKILL.md")) + [REPO / "README.md", REPO / "CLAUDE.md"]


@pytest.mark.parametrize("path", _docs(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_shell_variable_shorthand(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*[A-Z]+=['\"]python", text, re.M), "B='python ...' shorthand in %s" % path.name
    assert not re.search(r"\$BC?\b", text), "$B / $BC shorthand in %s" % path.name


@pytest.mark.parametrize("path", sorted(SKILLS.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skills_preapprove_every_launcher_form(path):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^allowed-tools:(.*)$", text, re.M)
    assert m, "no allowed-tools in %s" % path
    if "bizconnect " in text:
        for form in ("Bash(bizconnect *)", "Bash(python *)", "Bash(python3 *)", "Bash(py *)"):
            assert form in m.group(1), "%s: allowed-tools lacks %s" % (path.parent.name, form)


GENERIC_DOCS = _docs() + [REPO / "examples" / "connections.example.yaml"]
LEAKS = re.compile(r"\b(Elman|Josh|muse\.md|notion-bot|nous-reg|Nous)\b")


@pytest.mark.parametrize("path", GENERIC_DOCS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_generic_docs_have_no_personal_examples(path):
    hits = LEAKS.findall(path.read_text(encoding="utf-8"))
    assert not hits, "%s mentions %s — use neutral examples (maintainer notes go in docs/)" % (path.name, hits)


def _as_written(raw):
    """A front-matter value's text as its author meant it: surrounding quotes removed and
    escapes undone (a plain scalar is taken verbatim)."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r'\\(["\\])', r"\1", raw[1:-1])
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


@pytest.mark.parametrize("path", sorted(SKILLS.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skill_front_matter_parses_as_yaml(path):
    """Claude Code reads SKILL.md front matter as YAML: in a plain scalar ' #' starts a comment
    (the description is silently cut short) and ': ' is a parse error. Quote such values."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", path.read_text(encoding="utf-8"), re.S)
    assert m, "no front matter in %s" % path
    data = YAML(typ="safe").load(m.group(1))
    assert isinstance(data, dict) and data.get("name") == path.parent.name and data.get("description")
    raw = next(ln for ln in m.group(1).splitlines() if ln.startswith("description:"))[len("description:"):].strip()
    if raw[:1] not in (">", "|"):                            # a block scalar spans lines: skip
        assert data["description"] == _as_written(raw), "%s: the YAML description is not the text as written" \
            % path.parent.name


def test_notion_notes_routes_file_sync_to_notion_sync():
    head = (SKILLS / "notion-notes" / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert "notion-sync" in head, "notion-notes' description must point file sync at notion-sync"
