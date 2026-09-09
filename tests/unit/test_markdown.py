"""终端 markdown 渲染的行为测试。

重点在两处历史上出过问题的地方：
  1. 流式与非流式必须渲染出同样的可见文本 —— 之前是两套实现，分叉过。
  2. 不能产出 SGR 以外的转义(如 OSC 8 超链接)，否则 cli 的 footer 宽度算错。
"""
from app.util import markdown as md
from app.util.text import display_width, strip_ansi


def render(text: str, width: int = 40) -> str:
    return md.render(text, width_fn=lambda: width)


def plain(text: str, width: int = 40) -> str:
    return strip_ansi(render(text, width))


def stream(text: str, width: int = 40, chunk: int = 1) -> str:
    """按 chunk 大小切碎喂进去，模拟任意切分的 provider 增量。"""
    renderer = md.MarkdownRenderer(width_fn=lambda: width)
    pieces = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    return "".join(renderer.feed(p) for p in pieces) + renderer.flush()


# ── 行内 ─────────────────────────────────────────────────────────────────────
def test_bold_italic_strike_are_rendered_not_printed():
    out = plain("**粗** 和 *斜* 和 ~~删~~")
    assert out.strip() == "粗 和 斜 和 删"


def test_bold_requires_matching_delimiters():
    """**a__ 不是一对，不能当粗体吃掉。"""
    assert "__" in plain("**a__")


def test_inline_code_content_is_not_treated_as_markdown():
    """代码里的 ** 是内容，不是粗体标记。"""
    assert "**" in plain("`a ** b`")


def test_link_shows_text_and_url_without_osc_escape():
    out = render("[文档](https://example.com/a)")
    assert "文档" in strip_ansi(out)
    assert "https://example.com/a" in strip_ansi(out)
    # OSC 8 的 \033] 不会被 strip_ansi 剥掉，会把 footer 宽度算错
    assert "\033]" not in out


def test_strip_ansi_removes_everything_the_renderer_emits():
    """渲染器只能产出 SGR 序列，否则宽度计算会带上不可见字符。"""
    out = render("# 标题\n\n**粗** `码` [链接](http://x.y)\n\n> 引用\n")
    assert "\033" not in strip_ansi(out)


# ── 块级 ─────────────────────────────────────────────────────────────────────
def test_heading_marker_is_consumed():
    assert plain("## 复杂度").strip() == "复杂度"


def test_bullets_and_ordered_list():
    assert "•" in plain("- 一项")
    assert plain("3. 第三项").strip().startswith("3.")


def test_blockquote_and_rule():
    assert md.MarkdownRenderer.QUOTE_BAR.strip() in plain("> 注意")
    assert plain("---", width=12).strip() == "─" * 12


def test_code_block_replaces_fence_with_bar_and_language_label():
    out = plain("```python\ndef f():\n    return 1\n```\n")
    assert "```" not in out
    assert "python" in out
    assert "│ def f():" in out
    # 缩进必须原样保留，pygments 的 stripall 会吃掉它
    assert "│     return 1" in out


def test_code_block_without_language_has_no_label_line():
    out = plain("```\nplain text\n```\n")
    assert "│ plain text" in out
    assert out.splitlines()[0].strip() == "│ plain text"


def test_table_columns_align_on_wide_characters():
    out = plain(
        "| 算法 | 复杂度 |\n"
        "|------|--------|\n"
        "| 快排 | O(n log n) |\n"
        "| 归 | O(n) |\n"
    )
    rows = [line for line in out.splitlines() if "│" in line]
    assert len(rows) == 3  # 表头 + 两行数据（分隔行是 ─┼─，不含 │）
    assert len({display_width(r) for r in rows}) == 1, rows


def test_table_rows_do_not_leave_blank_lines_while_buffering():
    """缓冲表格行时不能每行留一个空行。"""
    out = plain("正文\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n结束\n")
    assert "\n\n\n" not in out


# ── 流式 ─────────────────────────────────────────────────────────────────────
DOC = """# 标题

- **粗体** 和 `代码`
- [链接](https://example.com)

> 引用

| 列 | 值 |
|----|----|
| 甲 | 1 |

---

```python
def f():
    return 1
```

结尾。"""


def test_streaming_matches_non_streaming_visible_text():
    for chunk in (1, 3, 17):
        assert strip_ansi(stream(DOC, chunk=chunk)) == strip_ansi(render(DOC)), chunk


def test_streaming_preserves_content_exactly_for_plain_prose():
    """纯散文不能因为缓冲丢字或重复 —— footer 重绘曾经把内容打两遍。"""
    text = "Hello, 本地模型!"
    assert strip_ansi(stream(text)).rstrip("\n") == text


def test_streaming_flush_emits_trailing_line_without_newline():
    """模型末尾常常不带换行，flush 必须把它补出来。"""
    renderer = md.MarkdownRenderer(width_fn=lambda: 40)
    fed = renderer.feed("最后一行没有换行")
    assert "最后一行没有换行" in strip_ansi(fed + renderer.flush())


def test_streaming_flush_closes_unterminated_code_block():
    renderer = md.MarkdownRenderer(width_fn=lambda: 40)
    out = renderer.feed("```python\ndef f():\n") + renderer.flush()
    assert "│ def f():" in strip_ansi(out)


# ── 降级 ─────────────────────────────────────────────────────────────────────
def test_color_disabled_passes_text_through_untouched():
    renderer = md.MarkdownRenderer(color=False)
    assert renderer.feed("**粗**\n") + renderer.flush() == "**粗**\n"


def test_unknown_language_falls_back_without_raising():
    out = plain("```notalanguage\nsome text\n```\n")
    assert "│ some text" in out
