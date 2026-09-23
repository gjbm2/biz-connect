"""Markdown <-> Notion block conversion (bizconnect.notion_md)."""
from __future__ import annotations

import pytest

from bizconnect import notion_md as M

SAMPLE = """\
Intro paragraph that is
soft-wrapped over two lines, with **bold**, *italic*, `code`, ~~gone~~ and a [link](https://x.com/a).

## Section

- one
- two with a [relative link](other.md)
  - nested **deep**
    - deeper still
- [ ] todo open
- [x] todo done

1. first
2. second

> A quote
> that wraps.

> [!WARNING]
> Careful now.

```python
print("hi")
```

| Name | Value |
|---|---|
| a \\| b | `x` |

![A caption](media/chart.png)

---

<!-- notion:child_page "ignored" -->

Final line.<br>With a break.
"""


def _types(blocks):
    return [b["type"] for b in blocks]


def test_parse_block_types():
    b = M.parse_blocks(SAMPLE)
    assert _types(b) == ["paragraph", "heading_2", "bulleted_list_item", "bulleted_list_item",
                         "to_do", "to_do", "numbered_list_item", "numbered_list_item", "quote",
                         "callout", "code", "table", "image", "divider", "paragraph"]
    assert b[4]["to_do"]["checked"] is False and b[5]["to_do"]["checked"] is True
    assert b[10]["code"]["language"] == "python"
    assert b[9]["callout"]["icon"]["emoji"] == M.ALERTS["WARNING"]
    assert b[12]["image"]["_src"] == "media/chart.png"


def test_soft_wrap_and_inline_annotations():
    p = M.parse_blocks(SAMPLE)[0]["paragraph"]["rich_text"]
    text = M.plain(p)
    assert text.startswith("Intro paragraph that is soft-wrapped over two lines")
    anns = {M._seg_parts(r)[0]: M._seg_parts(r)[2] for r in p}
    assert anns["bold"][0] and anns["italic"][1] and anns["code"][3] and anns["gone"][2]
    link = [r for r in p if M._seg_parts(r)[1]]
    assert M._seg_parts(link[0])[:2] == ("link", "https://x.com/a")


def test_nested_lists():
    b = M.parse_blocks(SAMPLE)
    two = b[3]["bulleted_list_item"]
    nested = two["children"][0]
    assert M.plain(nested["bulleted_list_item"]["rich_text"]) == "nested deep"
    assert M.plain(nested["bulleted_list_item"]["children"][0]["bulleted_list_item"]["rich_text"]) == "deeper still"


def test_table_and_escaped_pipe():
    t = [x for x in M.parse_blocks(SAMPLE) if x["type"] == "table"][0]["table"]
    assert t["table_width"] == 2
    cells = t["children"][1]["table_row"]["cells"]
    assert M.plain(cells[0]) == "a | b"


def test_break_and_comment():
    b = M.parse_blocks(SAMPLE)
    assert M.plain(b[-1]["paragraph"]["rich_text"]) == "Final line.\nWith a break."


@pytest.mark.parametrize("md", [
    SAMPLE,
    "Plain * star and snake_case_name and 3 * 4.",
    "Text with [E1] refs and a trailing \\\\ backslash.",
    "***bold italic*** then *a* **b** _c_",
    "*(Source: local file* *`xlsx`**; GDP ...)*",
    "- item\n\n  continued paragraph under the item\n- next",
    "1. one\n   - child\n2. two",
    "# Heading\n\nPara\n\n#### Deep heading",
    "A line that starts\n- with a dash is a list",
    "Quote next:\n\n> [!NOTE] inline alert text",
    "| a |\n|---|",
    "`` code with ` tick ``",
])
def test_render_is_idempotent_and_lossless(md):
    b1 = M.parse_blocks(md)
    r1 = M.render_blocks(b1)
    b2 = M.parse_blocks(r1)
    assert M.render_blocks(b2) == r1

    def norm(blocks):
        out = []
        for x in blocks:
            t = x["type"]
            body = x[t]
            out.append((t, [M._seg_parts(r) for r in M._merge(body.get("rich_text", []))],
                        norm(body.get("children", [])) if t != "table" else
                        [[M.plain(c) for c in r["table_row"]["cells"]] for r in body["children"]]))
        return out
    assert norm(b1) == norm(b2)


def test_paragraph_that_looks_like_a_marker_stays_a_paragraph():
    blk = {"type": "paragraph", "paragraph": {"rich_text": [M.seg("- not a list")]}}
    md = M.render_blocks([blk])
    assert M.parse_blocks(md)[0]["type"] == "paragraph"
    blk = {"type": "paragraph", "paragraph": {"rich_text": [M.seg("1. not numbered")]}}
    assert M.parse_blocks(M.render_blocks([blk]))[0]["type"] == "paragraph"


def test_unmanaged_blocks_marker_only_on_pull():
    blocks = [{"type": "child_page", "child_page": {"title": "Sub"}, "id": "x"},
              {"type": "paragraph", "paragraph": {"rich_text": [M.seg("hi")]}}]
    assert M.render_blocks(blocks) == "hi"
    assert M.render_blocks(blocks, markers=True) == '<!-- notion:child_page "Sub" -->\n\nhi'
    assert not M.is_managed({"type": "paragraph", "paragraph": {"rich_text": []}})


def test_signature_ignores_notion_only_styling():
    remote = {"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "hello "}, "plain_text": "hello ",
         "annotations": {"bold": False, "italic": False, "strikethrough": False, "code": False,
                         "underline": True, "color": "red"}},
        {"type": "text", "text": {"content": "world"}, "plain_text": "world",
         "annotations": {"bold": False, "color": "default"}}]}}
    local = M.parse_blocks("hello world")[0]
    assert M.signature(remote) == M.signature(local)


def test_code_language_normalised():
    assert M.parse_blocks("```sh\nls\n```")[0]["code"]["language"] == "shell"
    assert M.parse_blocks("```weird\nx\n```")[0]["code"]["language"] == "plain text"
    assert M.render_blocks(M.parse_blocks("```text\nx\n```")) == "```\nx\n```"


def test_front_matter_and_title():
    fm, rest = M.split_front_matter("---\na: 1\n---\n# Title *x*\n\nBody")
    assert fm == "a: 1\n"
    title, body = M.split_title(rest)
    assert title == "Title x" and body.strip() == "Body"
    assert M.split_front_matter("no fm") == (None, "no fm")
