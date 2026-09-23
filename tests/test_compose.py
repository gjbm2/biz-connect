"""compose render helpers (bizconnect.connectors.compose)."""
from __future__ import annotations

from bizconnect.connectors import compose as C


def test_question_subhead_label_is_configurable():
    ans = "## Q1 — Pricing\n\nOur answer."
    assert C.inject_question(ans, "How should prices work?") == \
        "## Q1 — Pricing\n\n> **Question.** How should prices work?\n\nOur answer."
    assert "> **Ofgem's question.** How" in C.inject_question(ans, "How?", "Ofgem's question")
    assert C.inject_question(ans, "How?", "") == ans                  # "" = no subhead
    assert C.inject_question(ans, "How?", None) == ans
    own = "## Q1\n> **The ask.** How?\n\nOur answer."                   # the answer carries its own
    assert C.inject_question(own, "How?", "The ask") == own
    assert C.inject_question("No heading.", "How?") == "> **Question.** How?\n\nNo heading."


def test_render_reads_question_label_from_pipeline_yaml(tmp_path, monkeypatch):
    (tmp_path / "pipeline.yaml").write_text(
        'title: "# Doc"\nquestion_label: "Ofgem\'s question"\n'
        "items: {source: items.json, list_key: items, id_key: id, text_key: text, pad: 0}\n"
        "paths: {answers_dir: answers, final: final/doc.md}\n", encoding="utf-8")
    (tmp_path / "items.json").write_text('{"items": [{"id": "Q1", "text": "Why?"}]}', encoding="utf-8")
    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / "Q1.md").write_text("## Q1\n\nBecause.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    C.run_render(C.Cfg())
    assert "> **Ofgem's question.** Why?" in (tmp_path / "final" / "doc.md").read_text(encoding="utf-8")
