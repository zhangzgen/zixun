import json
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
from unittest.mock import patch
import pytest

from technical_reports.common import digest
from technical_reports.store import merge, latest, load, export_search, collapse_aliases
from technical_reports.renderer import section_blocks, report_blocks, card_sections
from technical_reports.reader import reading_chunks
from news_web.archive_store import ArchiveStore


def report(root, number=1, text=None):
    text = text or ('[Page 1]\nComplete original with methodology, evaluation and limitations.\n' * 30)
    path = root / f'original-{number}-{digest(text)[:8]}.txt'
    path.write_text(text, encoding='utf-8')
    return {'title': f'Agent memory research {number}', 'vendor': 'Kimi', 'source': 'Moonshot AI 官方研究',
            'published_at': f'2026-09-{number:02d}', 'url': f'https://www.kimi.com/en/blog/report-{number}',
            'overview': '新的记忆方法提高了长程智能体任务表现。', 'summary': '**核心结论**：长期记忆提高任务成功率。\n\n**依据**：实验覆盖长期任务。\n\n**边界**：仅在报告设置下验证。',
            'fulltext_path': path.name, 'read_status': 'full_text_read',
            'reading_evidence': {'sha256': digest(text), 'char_count': len(text), 'read_chars': len(text), 'reader': 'agent', 'read_at': '2026-10-02T23:00:00+08:00'}}


def test_no_summary_without_complete_original(tmp_path):
    item = report(tmp_path)
    item['reading_evidence']['read_chars'] -= 1
    with pytest.raises(ValueError, match='incomplete'):
        merge([item], tmp_path/'archive.json', tmp_path)
    item['reading_evidence']['read_chars'] += 1
    item['reading_evidence']['sha256'] = 'wrong'
    with pytest.raises(ValueError, match='hash'):
        merge([item], tmp_path/'archive.json', tmp_path)


def test_idempotency_revisions_and_failure_preserve_read_version(tmp_path):
    archive = tmp_path/'archive.json'
    first = report(tmp_path)
    assert merge([first], archive, tmp_path)['added'] == 1
    assert merge([first], archive, tmp_path)['updated'] == 0
    merge([{**first, 'read_status': 'pending_full_text', 'error': 'HTTP 403'}], archive, tmp_path)
    assert latest(15, archive)[0]['summary'] == first['summary']
    second = report(tmp_path, text='Revised report full original. ' * 100)
    second['summary'] = '修订版总结'
    assert merge([second], archive, tmp_path)['updated'] == 1
    assert len(load(archive)['items'][0]['versions']) == 1


def test_queued_old_reader_cannot_replace_recovered_original(tmp_path):
    archive = tmp_path / 'archive.json'
    old = report(tmp_path)
    old['original_acquired_at'] = '2026-10-02T23:00:00+08:00'
    merge([old], archive, tmp_path)
    recovered = report(tmp_path, text='Complete report and recovered graphical appendix. ' * 100)
    recovered.update(original_acquired_at='2026-10-03T01:00:00+08:00',
                     own_original_urls=['https://example.com/appendix.pdf'])
    merge([recovered], archive, tmp_path)
    assert merge([old], archive, tmp_path)['stale_ignored'] == 1
    legacy = {key: value for key, value in old.items() if key != 'original_acquired_at'}
    assert merge([legacy], archive, tmp_path)['stale_ignored'] == 1
    current = load(archive)['items'][0]
    assert current['content_hash'] == recovered['reading_evidence']['sha256']
    assert current['own_original_urls'] == recovered['own_original_urls']
    assert len(current['versions']) == 1
    assert current['versions'][0]['original_acquired_at'] == old['original_acquired_at']


def test_legacy_same_second_conflicting_reader_cannot_replace_current_snapshot(tmp_path):
    archive = tmp_path / 'archive.json'
    old = report(tmp_path)
    old['original_acquired_at'] = '2026-10-03T01:00:00+08:00'
    recovered = report(tmp_path, text='Complete original with the newly recovered chart. ' * 100)
    recovered['original_acquired_at'] = old['original_acquired_at']
    merge([recovered], archive, tmp_path)
    assert merge([old], archive, tmp_path)['stale_ignored'] == 1
    current = load(archive)['items'][0]
    assert current['content_hash'] == recovered['reading_evidence']['sha256']
    # An equal-time correction of metadata on the same original is safe.
    corrected = {**recovered, 'title': 'Corrected official title'}
    assert merge([corrected], archive, tmp_path)['stale_ignored'] == 0
    assert load(archive)['items'][0]['title'] == corrected['title']


def test_microsecond_acquisitions_order_two_revisions_in_the_same_second(tmp_path):
    archive = tmp_path / 'archive.json'
    old = report(tmp_path)
    old['original_acquired_at'] = '2026-10-03T01:00:00.100000+08:00'
    recovered = report(tmp_path, text='Later exact snapshot with recovered appendix. ' * 100)
    recovered['original_acquired_at'] = '2026-10-03T01:00:00.200000+08:00'
    merge([old], archive, tmp_path)
    assert merge([recovered], archive, tmp_path)['updated'] == 1
    assert merge([old], archive, tmp_path)['stale_ignored'] == 1
    assert load(archive)['items'][0]['content_hash'] == recovered['reading_evidence']['sha256']


def test_queued_old_alias_reader_cannot_erase_newer_retired_original(tmp_path):
    archive = tmp_path / 'archive.json'
    primary = report(tmp_path, number=1)
    old = report(tmp_path, number=2)
    old['original_acquired_at'] = '2026-10-03T00:00:00+08:00'
    merge([primary, old], archive, tmp_path)
    primary_id, duplicate_id = [item['id'] for item in sorted(load(archive)['items'], key=lambda item: item['published_at'])]
    collapse_aliases({duplicate_id: primary_id}, archive)
    recovered = report(tmp_path, number=2, text='Retired edition plus the recovered appendix. ' * 100)
    recovered['original_acquired_at'] = '2026-10-03T01:00:00+08:00'
    merge([recovered], archive, tmp_path)
    assert merge([old], archive, tmp_path)['stale_ignored'] == 1
    state = load(archive)
    assert len(state['items']) == 1
    assert state['merged_reports'][duplicate_id]['content_hash'] == recovered['reading_evidence']['sha256']


def test_concurrent_search_exports_cannot_restore_old_summary_or_delete_new_day(tmp_path):
    from technical_reports import store
    archive = tmp_path / 'archive.json'
    output = tmp_path / 'technical_report_daily'
    old = report(tmp_path)
    merge([old], archive, tmp_path)
    first_writing, release_first, second_started, second_finished = (threading.Event() for _ in range(4))
    writer = store.write_json
    blocked = [False]
    def write_snapshot(path, data):
        if Path(path).parent == output and not blocked[0]:
            blocked[0] = True
            first_writing.set()
            assert release_first.wait(5)
        writer(path, data)
    def second_export():
        second_started.set()
        export_search(archive, output)
        second_finished.set()
    with patch.object(store, 'write_json', side_effect=write_snapshot), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(export_search, archive, output)
        assert first_writing.wait(5)
        updated = report(tmp_path, text='New original edition with a revised result. ' * 100)
        updated['summary'] = '修订后的新总结'
        merge([updated, report(tmp_path, number=2)], archive, tmp_path)
        second = pool.submit(second_export)
        assert second_started.wait(5)
        # The second export must wait for the first snapshot to finish writing.
        try:
            assert not second_finished.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=5); second.result(timeout=5)
    assert sorted(path.name for path in output.glob('*.json')) == ['2026-09-01.json', '2026-09-02.json']
    saved = json.loads((output / '2026-09-01.json').read_text())
    assert saved['items'][0]['content'] == updated['summary']


def test_latest_fifteen_and_shared_folded_layout(tmp_path):
    archive = tmp_path/'archive.json'
    merge([report(tmp_path, i) for i in range(1,19)], archive, tmp_path)
    records = latest(15, archive)
    assert len(records) == 15
    assert records[0]['published_at'] == '2026-09-18'
    assert records[-1]['published_at'] == '2026-09-04'
    blocks = section_blocks(records)
    assert blocks[0]['block_type'] == 5
    assert blocks[0]['heading3']['style']['folded'] is False
    assert blocks[1:] == sum((report_blocks(item) for item in records), [])
    assert sum(b['block_type'] == 2 and b['text']['style'].get('folded', False) and bool(b.get('children')) for b in blocks) == 15
    card, = card_sections(records)
    assert card['tag'] == 'markdown' and len(card['content'].splitlines()) == 5


def test_reports_index_separate_from_news_labels_summary(tmp_path):
    archive = tmp_path/'archive.json'
    news = tmp_path/'news'; news.mkdir()
    output = tmp_path/'reports'
    merge([report(tmp_path)], archive, tmp_path)
    export_search(archive, output)
    (news/'2026-09-01.json').write_text(json.dumps({'items':[{'archive_id':'news-1','资讯标题':'另一条资讯','超链接':'https://example.com/news','发布时间':'2026-09-01 12:00:00','来源':'媒体','desc':'新闻摘要'}]}))
    store = ArchiveStore(news, tmp_path/'index.db', output)
    store.refresh(True)
    assert store.stats()['article_count'] == 2
    found = store.search('记忆')['items'][0]
    assert found['content_kind'] == '技术报告总结'
    assert found['source'] == 'Kimi 技术报告'


def test_contiguous_original_chunks_cover_appendix_without_truncation():
    text = ('methods\n' * 12000) + 'appendix: negative results and limitations'
    chunks = reading_chunks(text, 14000)
    assert ''.join(chunk for _,chunk in chunks) == text
    assert chunks[-1][1].endswith('limitations')
    assert [start for start,_ in chunks] == list(range(0,len(text),14000))


def test_daily_section_replacement_preserves_other_root_sections():
    from technical_reports.feishu import update_daily_section
    blocks = [{'block_id':'doc','children':['news','n1','reports','r1','other','o1']},
              {'block_id':'news','block_type':5,'heading3':{'elements':[{'text_run':{'content':'🟣 AI 资讯'}}]}},
              {'block_id':'reports','block_type':5,'heading3':{'elements':[{'text_run':{'content':'🟢 技术报告'}}]}},
              {'block_id':'other','block_type':5,'heading3':{'elements':[{'text_run':{'content':'其他'}}]}}]
    with patch('technical_reports.feishu.core.list_doc_blocks',return_value=blocks),patch('technical_reports.feishu.api') as api,patch('technical_reports.feishu.write_blocks'):
        update_daily_section('token','doc',[])
    assert api.call_args.args[-1] == {'start_index':2,'end_index':4}


def test_failed_replacement_keeps_existing_feishu_content():
    from technical_reports.feishu import write_blocks
    existing = [{'block_id': 'doc', 'children': ['old']}]
    with patch('technical_reports.feishu.core.list_doc_blocks', return_value=existing), patch('technical_reports.feishu.api', side_effect=RuntimeError('write failed')) as api:
        with pytest.raises(RuntimeError):
            write_blocks('token', 'doc', [{'block_type': 2}], replace=True)
    assert api.call_count == 1
    assert api.call_args.args[2] == 'POST'


def test_same_source_report_and_news_remain_searchable(tmp_path):
    archive = tmp_path / 'archive.json'
    news = tmp_path / 'news'; news.mkdir()
    output = tmp_path / 'reports'
    item = report(tmp_path)
    merge([item], archive, tmp_path)
    export_search(archive, output)
    (news / '2026-10-02.json').write_text(json.dumps({'items': [{'archive_id': 'news-1', '资讯标题': '记忆研究发布', '超链接': item['url'], '发布时间': '2026-10-02 12:00:00', '来源': '媒体', 'desc': '研究记忆方法'}]}))
    store = ArchiveStore(news, tmp_path / 'index.db', output)
    store.refresh(True)
    results = store.search('记忆')['items']
    assert len(results) == 2
    assert {r['content_kind'] for r in results} == {'摘要', '技术报告总结'}


def test_related_edition_merges_without_losing_reading_evidence(tmp_path):
    from technical_reports.store import collapse_aliases
    archive = tmp_path / 'archive.json'
    primary, related = report(tmp_path, 1), report(tmp_path, 2)
    merge([primary, related], archive, tmp_path)
    before = load(archive)['items']
    ids = {i['title']: i['id'] for i in before}
    duplicate, target = ids[related['title']], ids[primary['title']]
    output = tmp_path / 'export'
    export_search(archive, output)
    collapse_aliases({duplicate: target}, archive)
    merge([related], archive, tmp_path)
    after = load(archive)
    assert len(after['items']) == 1
    assert after['merged_reports'][duplicate]['reading_evidence'] == related['reading_evidence']
    export_search(archive, output)
    assert not (output / '2026-09-02.json').exists()


def test_unchanged_original_retains_corrected_metadata_and_reading(tmp_path):
    from types import SimpleNamespace
    from technical_reports.__main__ import collect
    from technical_reports.store import report_id
    old = report(tmp_path)
    old['id'] = report_id(old)
    old['content_hash'] = old['reading_evidence']['sha256']
    new = {**old, 'title': 'Corrected report title', 'published_at': '2026-09-02', 'read_status': 'pending_full_text'}
    adapter = SimpleNamespace(discover=lambda *a: {'items': [new], 'coverage': {}, 'errors': []})
    output = tmp_path / 'candidates.json'
    with patch('technical_reports.__main__.load', return_value={'items': [old]}), patch('technical_reports.acquire.acquire', return_value=new), patch('technical_reports.__main__.importlib.import_module', return_value=adapter):
        collect('2026-09-01', '2026-09-03', ['Kimi'], output, recheck_archived=True)
    saved = json.loads(output.read_text())['items'][0]
    assert saved['title'] == new['title']
    assert saved['published_at'] == new['published_at']
    assert saved['reading_evidence'] == old['reading_evidence']
    assert saved['summary'] == old['summary']
    assert saved['read_status'] == 'full_text_read'


def test_late_reader_does_not_restore_excluded_announcement(tmp_path):
    archive = tmp_path / 'archive.json'
    item = report(tmp_path)
    merge([item], archive, tmp_path)
    saved = load(archive)
    saved['excluded_reports'] = {saved['items'][0]['id']: saved['items'].pop()}
    archive.write_text(json.dumps(saved))
    merge([item], archive, tmp_path)
    assert load(archive)['items'] == []
    assert len(load(archive)['excluded_reports']) == 1


def test_missing_graphics_and_unmapped_math_glyphs_are_not_full_reading(tmp_path):
    from technical_reports.store import is_fully_read
    item = report(tmp_path)
    item['original_documents'] = [{'kind': 'pdf', 'empty_pages': [2]}]
    assert not is_fully_read(item)
    with pytest.raises(ValueError, match='omitted pages'):
        merge([item], tmp_path / 'archive.json', tmp_path)
    item['original_documents'][0]['extraction_evidence'] = {'certified_blank_pages': [{'page': 2}]}
    assert is_fully_read(item)
    merge([item], tmp_path / 'archive.json', tmp_path)
    broken = report(tmp_path, text='Formula contains an unmapped bracket \x03. ' * 100)
    with pytest.raises(ValueError, match='glyphs'):
        merge([broken], tmp_path / 'archive.json', tmp_path)


def test_daily_section_retry_removes_all_partial_copies():
    from technical_reports.feishu import update_daily_section
    ids = ['news', 'n', 'reports1', 'r1', 'other', 'o', 'reports2', 'partial']
    blocks = [{'block_id': 'doc', 'children': ids}]
    for key, text in [('news', '🟣 AI 资讯'), ('reports1', '🟢 技术报告'),
                      ('other', '其他'), ('reports2', '🟢 技术报告')]:
        blocks.append({'block_id': key, 'block_type': 5,
                       'heading3': {'elements': [{'text_run': {'content': text}}]}})
    with patch('technical_reports.feishu.core.list_doc_blocks', return_value=blocks), \
         patch('technical_reports.feishu.api') as api, patch('technical_reports.feishu.write_blocks'):
        update_daily_section('token', 'doc', [])
    assert [call.args[-1] for call in api.call_args_list] == [
        {'start_index': 6, 'end_index': 8}, {'start_index': 2, 'end_index': 4}]


def test_related_feishu_article_keeps_evidence_and_points_to_primary(tmp_path):
    from technical_reports import feishu
    old, primary = report(tmp_path, 1), report(tmp_path, 2)
    old.update(id='old', feishu_document_id='old-doc', feishu_node_token='old-node')
    primary.update(id='primary', feishu_document_id='new-doc', feishu_url='https://example.com/new')
    archive = {'items': [primary], 'aliases': {'old': 'primary'}, 'merged_reports': {'old': old}}
    state = {'reports': {'old': {'document': 'old-doc'}}}
    with patch.object(feishu, 'STATE', tmp_path / 'state.json'), patch.object(feishu, 'api'), \
         patch.object(feishu, 'write_blocks') as write:
        assert feishu.reconcile_retired_documents('token', 'space', archive, state) == []
        assert write.call_count == 1
        rendered = json.dumps(write.call_args.args[2], ensure_ascii=False)
        assert primary['feishu_url'] in rendered
        assert old['overview'] in rendered and '核心结论' in rendered
        feishu.reconcile_retired_documents('token', 'space', archive, state)
        assert write.call_count == 1
    assert state['retired_reports']['old']['merged_into'] == 'primary'
