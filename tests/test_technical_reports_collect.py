import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from technical_reports import __main__ as cli
from technical_reports import acquire as acquisition
from technical_reports.store import report_id


def archived_report():
    item = {
        'vendor': 'Anthropic', 'title': 'Archived fake paper', 'source': 'Fake official research',
        'published_at': '2026-09-30', 'url': 'https://example.test/research/paper',
        'report_url': 'https://arxiv.org/pdf/2609.12345v1', 'paper_id': '2609.12345v1',
        'supplementary_urls': ['https://example.test/paper-appendix'],
        'own_original_urls': ['https://example.test/formal-paper'],
        'read_status': 'full_text_read', 'content_hash': 'complete-read-hash',
        'overview': 'Verified fake overview', 'summary': 'Verified fake summary',
        'reading_evidence': {'sha256': 'complete-read-hash', 'reader': 'agent', 'read_chars': 100},
        'feishu_url': 'https://example.test/archived-document', 'versions': [],
    }
    item['id'] = report_id(item)
    return item


def discovered_report(old, **changes):
    return {**{key: old[key] for key in ('vendor', 'title', 'source', 'published_at', 'url')},
            'title': 'Corrected fake paper', 'supplementary_urls': [], **changes}


def collect_records(tmp_path, archive, discoveries, fake_acquire):
    output = tmp_path / 'candidates.json'
    adapter = SimpleNamespace(discover=lambda *args: {'items': discoveries, 'coverage': {}, 'errors': []})
    with patch.object(cli, 'load', return_value=archive), \
         patch.object(acquisition, 'acquire', side_effect=fake_acquire), \
         patch.object(cli.importlib, 'import_module', return_value=adapter):
        result = cli.collect('2026-09-27', '2026-10-02', ['Anthropic'], output)
    return result, json.loads(output.read_text(encoding='utf-8'))


def resolved_original(item):
    item = copy.deepcopy(item)
    has_all_originals = bool(item.get('supplementary_urls') and item.get('own_original_urls'))
    return {**item, 'id': 'stale-discovery-id', 'paper_id': '2609.12345v2',
            'report_url': 'https://arxiv.org/pdf/2609.12345v2', 'error': None,
            'read_status': 'pending_full_text',
            'content_hash': 'complete-read-hash' if has_all_originals else 'primary-only-hash'}


def test_blog_discovery_inherits_full_originals_and_stable_paper_id_once(tmp_path):
    old = archived_report()
    fresh = discovered_report(old)
    calls = []
    def acquire(item):
        calls.append(copy.deepcopy(item))
        return resolved_original(item)
    result, payload = collect_records(tmp_path, {'items': [old]}, [fresh], acquire)
    assert len(calls) == 1
    assert calls[0]['supplementary_urls'] == old['supplementary_urls']
    assert calls[0]['own_original_urls'] == old['own_original_urls']
    saved, = payload['items']
    assert saved['id'] == old['id'] == report_id(saved)
    assert saved['paper_id'] == '2609.12345v2'
    assert saved['title'] == fresh['title']
    assert saved['summary'] == old['summary']
    assert saved['reading_evidence'] == old['reading_evidence']
    assert saved['read_status'] == 'full_text_read' and saved['unchanged']
    assert result['pending'] == 0


def test_identity_known_after_resolution_fetches_old_attachments_before_reusing_reading(tmp_path):
    old = archived_report()
    fresh = discovered_report(old, url='https://example.test/research/new-location')
    calls = []
    def acquire(item):
        calls.append(copy.deepcopy(item))
        return resolved_original(item)
    _, payload = collect_records(tmp_path, {'items': [old]}, [fresh], acquire)
    saved, = payload['items']
    fresh_calls = [item for item in calls if item['url'] == fresh['url']]
    assert len(fresh_calls) == 2
    assert not fresh_calls[0].get('supplementary_urls')
    assert fresh_calls[1]['supplementary_urls'] == old['supplementary_urls']
    assert fresh_calls[1]['own_original_urls'] == old['own_original_urls']
    assert saved['id'] == old['id']
    assert saved['content_hash'] == old['content_hash']
    assert saved['summary'] == old['summary'] and saved['read_status'] == 'full_text_read'


def test_revised_original_cannot_keep_old_summary_or_reading_attestation(tmp_path):
    old = archived_report()
    def acquire(item):
        return {**resolved_original(item), 'content_hash': 'revised-full-original-hash',
                'summary': old['summary'], 'overview': old['overview'], 'reading_evidence': old['reading_evidence']}
    result, payload = collect_records(tmp_path, {'items': [old]}, [discovered_report(old)], acquire)
    saved, = payload['items']
    assert saved['id'] == old['id']
    assert saved['read_status'] == 'pending_full_text' and not saved['unchanged']
    assert not {'summary', 'overview', 'reading_evidence'} & saved.keys()
    assert result['pending'] == 1


def test_new_declared_appendix_is_read_and_cannot_be_hidden_by_old_hash(tmp_path):
    old = archived_report()
    extra = 'https://example.test/new-appendix'
    fresh = discovered_report(old, supplementary_urls=[extra], related_urls=['https://example.test/citation'])
    calls = []
    def acquire(item):
        calls.append(copy.deepcopy(item))
        return {**resolved_original(item), 'content_hash': 'hash-including-new-appendix'}
    _, payload = collect_records(tmp_path, {'items': [old]}, [fresh], acquire)
    assert calls[0]['supplementary_urls'] == [*old['supplementary_urls'], extra]
    assert calls[0]['own_original_urls'] == old['own_original_urls']
    assert 'https://example.test/citation' not in calls[0]['supplementary_urls']
    assert payload['items'][0]['read_status'] == 'pending_full_text'


def test_two_known_papers_on_same_landing_page_do_not_share_reading_or_attachments(tmp_path):
    old = archived_report()
    fresh = discovered_report(old, paper_id='2609.67890', report_url='https://arxiv.org/pdf/2609.67890')
    calls = []
    def acquire(item):
        calls.append(copy.deepcopy(item))
        if item.get('paper_id', '').startswith('2609.67890'):
            return {**item, 'content_hash': 'different-paper-hash', 'read_status': 'pending_full_text', 'error': None}
        return resolved_original(item)
    _, payload = collect_records(tmp_path, {'items': [old]}, [fresh], acquire)
    new_call = next(item for item in calls if item['title'] == fresh['title'])
    assert not new_call.get('supplementary_urls') and not new_call.get('own_original_urls')
    assert len(payload['items']) == 2
    new = next(item for item in payload['items'] if item['id'] == report_id(fresh))
    assert new['read_status'] == 'pending_full_text' and 'summary' not in new


@pytest.mark.parametrize('retirement', ['aliases', 'excluded_reports'])
def test_resolved_retired_identity_stays_out_of_candidates(tmp_path, retirement):
    old = archived_report()
    archive = {'items': [], retirement: {old['id']: 'kept-primary-or-audited-exclusion'}}
    result, payload = collect_records(tmp_path, archive, [discovered_report(old)], resolved_original)
    assert payload['items'] == []
    assert result['discovered'] == result['pending'] == 0


def test_failed_fetch_keeps_known_entity_id_without_claiming_previous_reading(tmp_path):
    old = archived_report()
    def acquire(item):
        raise RuntimeError('Fake unavailable original')
    result, payload = collect_records(tmp_path, {'items': [old]}, [discovered_report(old)], acquire)
    saved, = payload['items']
    assert saved['id'] == old['id']
    assert saved['read_status'] == 'pending_full_text'
    assert not {'summary', 'overview', 'reading_evidence'} & saved.keys()
    assert payload['fetch_errors'][0]['id'] == old['id']
    assert result['fetch_failed_count'] == 1


def test_archived_recheck_cannot_hide_fresh_candidate_missing_appendix(tmp_path):
    old = archived_report()
    extra = 'https://example.test/new-appendix'
    fresh = discovered_report(old, url='https://example.test/research/new-location', supplementary_urls=[extra])
    def acquire(item):
        if extra in item.get('supplementary_urls', []) and item.get('own_original_urls'):
            raise RuntimeError('New appendix unavailable')
        return resolved_original(item)
    result, payload = collect_records(tmp_path, {'items': [old]}, [fresh], acquire)
    saved, = payload['items']
    assert saved['id'] == old['id']
    assert saved['url'] == fresh['url']
    assert saved['read_status'] == 'pending_full_text' and 'summary' not in saved
    assert 'New appendix unavailable' in saved['error']
    assert result['fetch_failed_count'] == result['pending'] == 1


def test_legacy_original_document_declarations_survive_source_rediscovery(tmp_path):
    old = archived_report()
    old.pop('own_original_urls', None)
    old.pop('supplementary_urls', None)
    remarks = 'https://example.test/remarks.pdf'
    appendix = 'https://example.test/appendix.pdf'
    old['original_documents'] = [
        {'url': old['report_url'], 'kind': 'pdf'},
        {'url': remarks, 'kind': 'pdf'},
        {'url': appendix, 'role': 'supplementary', 'kind': 'pdf'},
    ]
    fetched = []

    def acquire(item):
        fetched.append(item)
        return resolved_original(item)

    collect_records(tmp_path, {'items': [old]}, [discovered_report(old)], acquire)
    assert fetched
    assert all(remarks in item['own_original_urls'] for item in fetched)
    assert all(appendix in item['supplementary_urls'] for item in fetched)


def test_date_recovery_hook_adds_only_reviewed_items_and_keeps_scope_candidates_separate(tmp_path):
    recovered = {**archived_report(), 'vendor': 'Google', 'paper_id': '2609.12345',
                 'date_recovered': True, 'scope_status': 'eligible'}
    review = {'vendor': 'Google', 'title': 'Unreviewed exact-date paper',
              'url': 'https://research.test/unreviewed', 'published_at': '2026-09-30'}
    recovery = {'items': [recovered], 'new_date_verified_candidates': [review],
                'diagnostics': [{'status': 'recovered'}, {'status': 'pending_scope_review'}],
                'stats': {'recovered_count': 1, 'scope_review_count': 1},
                'complete_candidate_resolution': False}
    calls = []
    with patch.object(cli, 'recover_undated_discoveries', return_value=recovery) as hook:
        result, payload = collect_records(tmp_path, {'items': []}, [],
            lambda item: calls.append(item) or resolved_original(item))
    hook.assert_called_once_with({'Anthropic': {}}, '2026-09-27', '2026-10-02')
    assert len(calls) == 1 and calls[0]['title'] == recovered['title']
    assert payload['new_date_verified_candidates'] == [review]
    assert payload['publication_date_recovery']['stats']['recovered_count'] == 1
    assert 'items' not in payload['publication_date_recovery']
    assert result['new_date_verified_candidate_count'] == 1
    assert 'technical_reports.publication_recovery --record-scope' in payload['agent_instruction']


def test_date_recovery_failure_does_not_block_other_discovered_reports(tmp_path):
    fresh = archived_report()
    with patch.object(cli, 'recover_undated_discoveries', side_effect=ValueError('malformed local audit')):
        result, payload = collect_records(tmp_path, {'items': []}, [fresh], resolved_original)
    assert len(payload['items']) == 1
    assert result['errors'][-1]['source'] == 'publication_date_recovery'
    assert not payload['publication_date_recovery']['complete_candidate_resolution']
