"""One report layout shared by vendor documents and the daily digest."""
import re
import run_ai_news as core


def heading(level, title, folded=False, link=None):
    key = 'heading' + str(level)
    data = core.text_data([core.text_run(title, link=link)])
    data['style'] = {'folded': folded}
    return {'block_type': level + 2, key: data}


def paragraph(text):
    return {'block_type': 2, 'text': core.text_data(core.parse_bold_summary(text))}


def published_label(item):
    if item.get('publication_event') == 'revision':
        first = '首发 ' + item['first_published_at'][:10] if item.get('first_published_at') else '首发日期未披露'
        return f"{item.get('version_published_at') or item['published_at'][:10]}（本版更新；{first}）"
    if item.get('date_precision') == 'inferred_day':
        return item['published_at'][:10] + '（关联官方发布日；报告未单独标注日期）'
    if item.get('date_precision') in ('month', 'year'):
        return item.get('published_at_raw') or item['published_at'][:7]
    return item['published_at'][:10]


def report_blocks(item):
    blocks = [heading(4, item['title'], link=item.get('feishu_url') or item['url']),
              paragraph(item['overview'])]
    elements = [core.text_run(f"来源：{item['source']} ｜ 发布时间：{published_label(item)} ｜ "),
                core.text_run('原文', link=item.get('report_url') or item['url'])]
    if item.get('report_url') and item['report_url'] != item['url']:
        elements += [core.text_run(' ｜ '), core.text_run('官方页面', link=item['url'])]
    blocks.append({'block_type': 2, 'text': core.text_data(elements)})
    blocks.append(heading(5, '📌 总结（展开查看）', folded=True))
    # Root heading levels delimit each folded summary; the next report is H4.
    for part in re.split(r'\n\s*\n', item['summary']):
        part = re.sub(r'^#{1,6}\s+', '', part.strip())
        for start in range(0, len(part), 1800):
            blocks.append(paragraph(part[start:start + 1800]))
    return blocks


def section_blocks(items, folded=False):
    blocks = [heading(3, '🟢 技术报告', folded=folded)]
    if not items:
        blocks.append(paragraph('暂无已完成全文阅读的技术报告。'))
    for item in items:
        blocks.extend(report_blocks(item))
    return blocks


def markdown(items):
    parts = ['### 🟢 技术报告', '']
    for item in items:
        parts += [f"#### [{item['title']}]({item.get('feishu_url') or item['url']})", '', item['overview'], '',
                  f"来源：{item['source']} · 发布时间：{published_label(item)} · [原文]({item.get('report_url') or item['url']})", '',
                  '<details><summary>📌 总结（展开查看）</summary>', '', item['summary'], '', '</details>', '']
    return '\n'.join(parts)


def card_sections(items):
    """Card v2 collapsible panels; detailed wiki uses the same report text."""
    panels = []
    for item in items:
        panels.append({'tag': 'markdown', 'content': f"**[{item['title']}]({item.get('feishu_url') or item['url']})**\n{item['overview']}\n来源：{item['source']} · {published_label(item)}"})
        panels.append({'tag': 'collapsible_panel', 'expanded': False,
                       'header': {'title': {'tag': 'plain_text', 'content': '📌 总结（展开查看）'},
                                  'icon': {'tag': 'standard_icon', 'token': 'down-small-ccm_outlined', 'size': '16px 16px'},
                                  'icon_position': 'right'},
                       'elements': [{'tag': 'markdown', 'content': item['summary']}]})
    return panels
