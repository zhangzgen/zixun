"""Convert summary Markdown to native Docx blocks, inside a folded heading."""
from copy import deepcopy
from urllib.parse import urlsplit

import mistune


_parse = mistune.create_markdown(renderer='ast', plugins=['strikethrough'])


def inline_elements(nodes, style=None):
    elements = []
    style = style or {}
    for node in nodes:
        kind = node['type']
        current = dict(style)
        flag = {'strong': 'bold', 'emphasis': 'italic',
                'strikethrough': 'strikethrough', 'codespan': 'inline_code'}.get(kind)
        if flag:
            current[flag] = True
        if kind in ('link', 'image'):
            url = node.get('attrs', {}).get('url', '')
            if urlsplit(url).scheme.lower() in ('http', 'https', 'mailto'):
                current['link'] = {'url': url}
        if 'children' in node:
            elements.extend(inline_elements(node['children'], current))
        else:
            value = '\n' if kind in ('softbreak', 'linebreak') else node.get('raw', '')
            if value:
                elements.append({'text_run': {'content': value, 'text_element_style': current}})
    return elements


def _text_blocks(kind, elements, style=None, limit=1800):
    """Split only rendered runs, retaining formatting even at the length boundary."""
    groups, group, size = [], [], 0
    for element in elements:
        run = element['text_run']
        content = run['content']
        while content:
            part, content = content[:limit - size], content[limit - size:]
            group.append({'text_run': {**deepcopy(run), 'content': part}})
            size += len(part)
            if size == limit:
                groups.append(group)
                group, size = [], 0
    if group:
        groups.append(group)
    result = []
    types = {'text': 2, 'bullet': 12, 'ordered': 13, 'code': 14, 'quote': 15}
    for index, group in enumerate(groups):
        # Continuations must not create additional list items or section headings.
        key = kind if index == 0 or kind == 'code' else 'text'
        data = {'elements': group, 'style': dict(style or {}) if key == kind else {}}
        block_type = int(key[7:]) + 2 if key.startswith('heading') else types[key]
        result.append({'block_type': block_type, key: data})
    return result


def summary_blocks(markdown, folded_level=5):
    """Keep all Markdown section headings below the outer fold's level.

    Sending Markdown in text_run does not invoke a Markdown renderer. Lists need
    their own blocks and emphasis/links need text_element_style. Native headings
    at or above the fold's level would prematurely end the folded region.
    """
    nodes = _parse(markdown)
    levels = []

    def find_levels(children):
        for node in children:
            if node['type'] == 'heading':
                levels.append(node['attrs']['level'])
            find_levels(node.get('children', []))

    find_levels(nodes)
    minimum = min(levels, default=1)

    def render(children, list_kind=None, quote=False):
        blocks = []
        for node in children:
            kind = node['type']
            if kind == 'blank_line':
                continue
            if kind == 'list':
                key = 'ordered' if node['attrs']['ordered'] else 'bullet'
                for child in node['children']:
                    blocks.extend(render(child['children'], list_kind=key, quote=quote))
            elif kind in ('paragraph', 'block_text', 'heading'):
                key = list_kind or ('quote' if quote else 'text')
                if kind == 'heading':
                    level = min(9, folded_level + 1 + node['attrs']['level'] - minimum)
                    key = 'heading' + str(level)
                blocks.extend(_text_blocks(key, inline_elements(node['children'])))
                list_kind = None
            elif kind == 'block_quote':
                blocks.extend(render(node['children'], quote=True))
            elif kind == 'block_code':
                elements = [{'text_run': {'content': node['raw'], 'text_element_style': {}}}]
                blocks.extend(_text_blocks('code', elements, {'wrap': True}))
            elif kind == 'thematic_break':
                blocks.append({'block_type': 22, 'divider': {}})
            else:
                # Preserve unrecognised content instead of silently dropping it.
                blocks.extend(_text_blocks('text', inline_elements([node])))
        return blocks

    return render(nodes)
