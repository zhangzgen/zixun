from technical_reports.docx_markdown import summary_blocks
from technical_reports.renderer import report_blocks


def runs(block):
    return next(value['elements'] for value in block.values()
                if isinstance(value, dict) and 'elements' in value)


def content(block):
    return ''.join(e['text_run']['content'] for e in runs(block))


def test_multiline_summary_has_native_headings_and_individual_list_items():
    blocks = summary_blocks('## 结论\n正文\n\n## 方法\n- **第一项**：说明\n- 第二项\n\n### 边界\n1. 条件一\n2. 条件二')
    assert [b['block_type'] for b in blocks] == [8, 2, 8, 12, 12, 9, 13, 13]
    assert [content(b) for b in blocks] == ['结论', '正文', '方法', '第一项：说明', '第二项', '边界', '条件一', '条件二']
    assert runs(blocks[3])[0]['text_run']['text_element_style']['bold'] is True


def test_inline_links_nested_emphasis_and_literal_escapes():
    block = summary_blocks('**重点 [原文](https://example.com/a_(b))**、*斜体*、`x**y`、~~旧值~~、\\*字面星号\\*')[0]
    by_text = {r['text_run']['content']: r['text_run']['text_element_style'] for r in runs(block)}
    assert by_text['原文'] == {'bold': True, 'link': {'url': 'https://example.com/a_(b)'}}
    assert by_text['斜体']['italic']
    assert by_text['x**y']['inline_code']
    assert by_text['旧值']['strikethrough']
    assert '*字面星号*' in content(block)


def test_long_formatted_text_preserves_every_character_and_style():
    text = '长段落' * 1400
    blocks = summary_blocks('**' + text + '**')
    assert ''.join(content(b) for b in blocks) == text
    assert all(len(content(b)) <= 1800 for b in blocks)
    assert all(e['text_run']['text_element_style']['bold'] for b in blocks for e in runs(b))


def test_summary_headings_cannot_end_fold_or_hide_the_next_report():
    item = {'title': '报告', 'overview': '概述', 'source': '官方', 'published_at': '2026-10-01',
            'url': 'https://example.com', 'summary': '# 结论\n文本\n\n## 依据\n- **证据**'}
    blocks = report_blocks(item) + report_blocks({**item, 'title': '下一份'})
    assert len(blocks) == 2
    assert all(block['text']['style']['folded'] for block in blocks)
    assert all(block.get('children') for block in blocks)
    assert content(blocks[1]).endswith('下一份\n概述')
    assert all('下一份' not in content(child) for child in blocks[0]['children'])
