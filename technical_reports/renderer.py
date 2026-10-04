"""One report layout shared by vendor documents and the daily digest."""
import re
import html
import run_ai_news as core
from .docx_markdown import summary_blocks
from .store import is_fully_read


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


def leaf_report_blocks(item):
    """Keep existing per-report pages in their pre-migration layout."""
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


def ordered_reports(items):
    return sorted(items, key=lambda item: (item.get('published_at', ''), item.get('id', item['title'])), reverse=True)


def report_blocks(item):
    """Body preview with a real child hierarchy for paragraph folding.

    The internal children payload is consumed by feishu.write_blocks; the API
    only receives the parent itself, then descendants under its returned ID.
    """
    complete = is_fully_read({**item, 'read_status': item.get('read_status', 'full_text_read')})
    complete = complete and bool(item.get('summary'))
    overview = item.get('overview') if complete else '已发现报告；原文完整读取（包括图表）和总结尚待完成。'
    elements = [core.text_run('🔸 '),
                core.text_run(f"[{item['source']}]", bold=True, link=item.get('report_url') or item['url']),
                core.text_run('  ' + item['title'] + '\n' + overview)]
    for element in elements:
        element['text_run']['text_element_style'].setdefault('bold', False)
    data = core.text_data(elements)
    data['style']['folded'] = complete
    block = {'block_type': 2, 'text': data}
    if complete:
        block['children'] = summary_blocks(item['summary'], folded_level=4)
        # The source label is the entry's only hyperlink; summary text stays intact.
        for child in block['children']:
            for value in child.values():
                if isinstance(value, dict):
                    for element in value.get('elements', []):
                        element.get('text_run', {}).get('text_element_style', {}).pop('link', None)
    return [block]


def catalogue_blocks(items):
    items = ordered_reports(items)
    blocks = []
    for item in items:
        blocks.extend(report_blocks(item))
    return blocks


def section_blocks(items, folded=False):
    blocks = [heading(3, '🟢 技术报告', folded=folded)]
    if not items:
        blocks.append(paragraph('暂无已完成全文阅读的技术报告。'))
    for item in ordered_reports(items):
        blocks.extend(report_blocks(item))
    return blocks


def leaf_section_blocks(items, folded=False):
    blocks = [heading(3, '🟢 技术报告', folded=folded)]
    if not items:
        blocks.append(paragraph('暂无已完成全文阅读的技术报告。'))
    for item in items:
        blocks.extend(leaf_report_blocks(item))
    return blocks


def markdown(items):
    parts = ['### 🟢 技术报告', '']
    for item in items:
        parts += [f"#### [{item['title']}]({item.get('feishu_url') or item['url']})", '', item['overview'], '',
                  f"来源：{item['source']} · 发布时间：{published_label(item)} · [原文]({item.get('report_url') or item['url']})", '',
                  '<details><summary>📌 总结（展开查看）</summary>', '', item['summary'], '', '</details>', '']
    return '\n'.join(parts)


def chat_overview(item, limit=60):
    """Prefer the Agent's short sentence; keep legacy overviews compact."""
    value = item.get('chat_overview') or item.get('overview') or item['title']
    value = re.sub(r'\s+', ' ', re.sub(r'\*\*|^#{1,6}\s*', '', value)).strip()
    if len(value) > limit:
        sentence = re.search(r'[。！？]', value[:limit])
        value = value[:sentence.end()] if sentence else value[:limit - 1].rstrip('，、；： ') + '…'
    return value


def card_sections(items):
    """Latest five short numbered previews, matching the AI news list."""
    lines = []
    for index, item in enumerate(ordered_reports(items)[:5], 1):
        vendor = html.escape(item.get('vendor') or item['source'], quote=True)
        overview = html.escape(chat_overview(item), quote=False)
        lines.append(f"{index}. {overview}<text_tag color='green'>{vendor}</text_tag>")
    return [{'tag': 'markdown', 'content': '\n'.join(lines) or '暂无已完成全文阅读的技术报告。'}]
