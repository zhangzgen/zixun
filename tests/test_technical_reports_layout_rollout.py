"""Regression checks for the catalogue/digest-only Feishu layout migration."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from technical_reports import feishu, renderer
from technical_reports.common import VENDORS, digest


def report(**changes):
    return {
        'id': 'R-layout', 'vendor': 'Anthropic', 'title': 'Claude-shaped science',
        'source': 'Anthropic Research', 'published_at': '2026-10-01',
        'url': 'https://www.anthropic.com/research/claude-shaped-science',
        'report_url': 'https://www.anthropic.com/research/claude-shaped-science.pdf',
        'feishu_url': 'https://ocns5gikjtax.feishu.cn/wiki/leaf-example',
        'overview': '可核验的科研任务需要模型与专家合作。',
        'summary': '## 核心结论\n**人类判断**决定科学价值。\n\n## 依据\n- **验证**：专家检查结果。\n\n### 边界\n限定任务与实验设置。',
        'read_status': 'full_text_read', **changes,
    }


def runs(block):
    return next(value['elements'] for value in block.values()
                if isinstance(value, dict) and 'elements' in value)


def text(block):
    return ''.join(element['text_run']['content'] for element in runs(block))


def test_report_preview_is_one_folded_body_paragraph_with_only_source_link():
    item = report()
    block, = renderer.report_blocks(item)
    assert block['block_type'] == 2
    assert block['text']['style']['folded'] is True
    assert text(block).startswith('🔸 [Anthropic Research]')
    assert item['title'] + '\n' + item['overview'] in text(block)
    assert item['published_at'] not in text(block)
    assert '发布时间' not in text(block) and '总结' not in text(block)
    linked = [element['text_run'] for element in runs(block)
              if element['text_run'].get('text_element_style', {}).get('link')]
    assert len(linked) == 1
    assert linked[0]['content'] == '[Anthropic Research]'
    assert linked[0]['text_element_style']['bold'] is True
    assert linked[0]['text_element_style']['link']['url'] == item['report_url']
    assert all(element['text_run']['text_element_style'].get('bold') is False
               for element in runs(block)
               if not element['text_run']['text_element_style'].get('link'))
    assert item['feishu_url'] not in json.dumps(block, ensure_ascii=False)
    # The summary is a real child tree, rather than Markdown text at root level.
    assert block['children']
    assert any(child['block_type'] == 12 for child in block['children'])
    assert any('人类判断' in text(child) for child in block['children'])
    assert not any('## ' in text(child) for child in block['children'])


@pytest.mark.parametrize('changes', [
    {'read_status': 'pending_full_text'},
    {'original_quality_error': 'missing chart'},
    {'original_documents': [{'kind': 'pdf', 'empty_pages': [2]}]},
    {'summary': ''},
])
def test_pending_preview_has_no_fold_or_stale_summary(changes):
    item = report(**changes)
    block, = renderer.report_blocks(item)
    assert block['block_type'] == 2
    assert block['text']['style'].get('folded') is False
    assert not block.get('children')
    assert '原文完整读取（包括图表）和总结尚待完成' in text(block)
    assert item['overview'] not in text(block)
    assert '人类判断' not in json.dumps(block, ensure_ascii=False)


def test_catalogue_keeps_newest_first_without_showing_dates_or_reordering_input():
    items = [report(id='a', title='Old', published_at='2026-04-03'),
             report(id='b', title='Recent B', published_at='2026-10-01'),
             report(id='c', title='Recent C', published_at='2026-10-01')]
    before = deepcopy(items)
    blocks = renderer.catalogue_blocks(items)
    previews = [block for block in blocks if block.get('children')]
    assert [next(item['title'] for item in items if item['title'] in text(block))
            for block in previews] == ['Recent C', 'Recent B', 'Old']
    assert all(block['text']['style']['folded'] for block in previews)
    assert all(item['published_at'] not in text(block)
               for item in items for block in blocks)
    assert items == before


def test_existing_leaf_renderer_retains_the_former_cached_render_hash():
    # Frozen from git HEAD's pre-migration renderer on this exact fixture. A
    # catalogue change must not invalidate every existing per-report leaf cache.
    rendered = renderer.leaf_section_blocks([report()], folded=False)
    assert digest(json.dumps(rendered, ensure_ascii=False, sort_keys=True)) == (
        '4ff3091852c80d3ae752e3d8a56ec11f629e35b10b50f39a6218e5410d02de21')


def nested_fixture():
    parent = renderer.paragraph('Visible overview')
    child = renderer.paragraph('Summary subsection')
    child['children'] = [renderer.paragraph('Complete evidence')]
    parent['children'] = [child, renderer.paragraph('Summary limitations')]
    return [parent]


def test_nested_writer_posts_children_under_returned_parents_before_deletion():
    source = nested_fixture()
    before = deepcopy(source)
    events = []

    def api(token, route, method='GET', body=None):
        events.append((route, method, deepcopy(body)))
        if method == 'DELETE':
            return {}
        assert method == 'POST'
        # Internal payload child dictionaries must never reach the children API.
        assert all('children' not in block for block in body['children'])
        created = []
        for block in body['children']:
            created.append({**deepcopy(block), 'block_id': 'new-' + text(block)})
        return {'children': created}

    with patch.object(feishu.core, 'list_doc_blocks', return_value=[
            {'block_id': 'doc', 'children': ['old1', 'old2']}]), \
         patch.object(feishu, 'api', side_effect=api), \
         patch.object(feishu.time, 'sleep'):
        created = feishu.write_blocks('token', 'doc', source, replace=True)
    assert [block['block_id'] for block in created] == ['new-Visible overview']
    posts = [event for event in events if event[1] == 'POST']
    assert '/blocks/doc/children?' in posts[0][0]
    assert any('/blocks/new-Visible overview/children?' in event[0] and
               [text(block) for block in event[2]['children']] ==
               ['Summary subsection', 'Summary limitations'] for event in posts)
    assert any('/blocks/new-Summary subsection/children?' in event[0] and
               text(event[2]['children'][0]) == 'Complete evidence' for event in posts)
    assert events[-1][1:] == ('DELETE', {'start_index': 0, 'end_index': 2})
    assert sum(event[1] == 'DELETE' for event in events) == 1
    assert source == before


def test_descendant_write_failure_retains_all_previous_document_blocks():
    events = []

    def api(token, route, method='GET', body=None):
        events.append((route, method, deepcopy(body)))
        if '/blocks/new-Summary subsection/children?' in route:
            raise RuntimeError('grandchild rejected')
        assert method == 'POST'
        return {'children': [{**deepcopy(block), 'block_id': 'new-' + text(block)}
                             for block in body['children']]}

    with patch.object(feishu.core, 'list_doc_blocks', return_value=[
            {'block_id': 'doc', 'children': ['old-content']}]), \
         patch.object(feishu, 'api', side_effect=api), \
         patch.object(feishu.time, 'sleep'):
        with pytest.raises(RuntimeError, match='grandchild rejected'):
            feishu.write_blocks('token', 'doc', nested_fixture(), replace=True)
    assert events and all(event[1] == 'POST' for event in events)


def test_bulk_later_batch_failure_keeps_old_content_and_never_splits_report_tree():
    source = []
    for index in range(2):
        parent = renderer.paragraph('Report ' + str(index))
        parent['children'] = [renderer.paragraph('Evidence ' + str(number)) for number in range(450)]
        source.append(parent)
    events = []

    def api(token, route, method='GET', body=None):
        events.append((route, method, deepcopy(body)))
        assert method == 'POST'
        assert len(body['descendants']) == 451 and len(body['children_id']) == 1
        if len(events) == 2:
            raise RuntimeError('second report batch rejected')
        return {'block_id_relations': [{'temporary_block_id': block['block_id'],
                                       'block_id': 'actual-' + str(index)}
                                      for index, block in enumerate(body['descendants'])]}

    with patch.object(feishu.core, 'list_doc_blocks', return_value=[
            {'block_id': 'doc', 'children': ['old-content']}]), \
         patch.object(feishu, 'api', side_effect=api), \
         patch.object(feishu.time, 'sleep'):
        with pytest.raises(RuntimeError, match='second report batch rejected'):
            feishu.write_blocks('token', 'doc', source, replace=True, bulk=True)
    assert len(events) == 2 and all(event[1] == 'POST' for event in events)


def test_catalogue_only_sync_does_not_write_label_rename_or_reconcile_leaves(tmp_path):
    (tmp_path / 'technical_reports').mkdir()
    state_path = tmp_path / 'state.json'
    leaf_cache = {'document': 'leaf-doc', 'render_hash': 'existing-cache',
                  'synced_at': '2026-10-01T00:00:00+08:00'}
    state_path.write_text(json.dumps({'vendors': {}, 'reports': {'R-layout': leaf_cache}}))
    items = [report(feishu_document_id='leaf-doc', feishu_node_token='leaf-node'),
             report(id='pending', title='Pending', read_status='pending_full_text',
                    original_quality_error='missing chart', feishu_document_id='pending-leaf')]
    archive = {'items': items, 'aliases': {'old': 'R-layout'},
               'merged_reports': {'old': report(feishu_document_id='old-leaf')},
               'excluded_reports': {'excluded': report(feishu_document_id='excluded-leaf')}}
    directories = {vendor: {'node_token': 'node-' + vendor, 'obj_token': 'doc-' + vendor,
                            'title': vendor + ' 技术报告'} for vendor in VENDORS}

    def ensure_node(token, space, parent, title, known):
        assert parent == feishu.REPORT_PARENT, 'must not create per-report children'
        return directories[title.removesuffix(' 技术报告')]

    with patch.object(feishu, 'ROOT', tmp_path), \
         patch.object(feishu, 'STATE', state_path), \
         patch.object(feishu, 'load', return_value=archive), \
         patch.object(feishu.core, 'tenant_token', return_value='token'), \
         patch.object(feishu, 'node', return_value={'space_id': 'space', 'obj_token': 'root-doc'}), \
         patch.object(feishu, 'children', return_value=list(directories.values())), \
         patch.object(feishu, 'ensure_node', side_effect=ensure_node), \
         patch.object(feishu, 'write_blocks') as write, \
         patch.object(feishu, 'api', side_effect=AssertionError('leaf mutation forbidden')), \
         patch.object(feishu, 'label_incomplete_original', side_effect=AssertionError('leaf label forbidden')), \
         patch.object(feishu, 'reconcile_retired_documents', side_effect=AssertionError('leaf reconciliation forbidden')), \
         patch.object(feishu, 'export_search'), \
         patch.object(feishu, 'latest', return_value=[items[0]]):
        result = feishu.sync(tmp_path / 'archive.json', update_today=False, update_reports=False)
    assert {call.args[1] for call in write.call_args_list} == {
        'root-doc', *(directory['obj_token'] for directory in directories.values())}
    assert write.call_count == len(VENDORS) + 1
    assert all(call.kwargs.get('replace') is True for call in write.call_args_list)
    assert result['published'] == 0 and result['pending_labelled'] == 0
    assert json.loads(state_path.read_text())['reports']['R-layout'] == leaf_cache
