import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from technical_reports import __main__ as cli
from technical_reports import acquire as acquisition
from technical_reports import adapters_china as china
from technical_reports import adapters_google_seed as google_seed
from technical_reports import publication_recovery as recovery
from technical_reports.discovery import KnownReports
from technical_reports.store import report_id


def report(**changes):
    row = {'vendor': 'Anthropic', 'title': 'An archived report', 'source': 'Official research',
           'published_at': '2026-10-01', 'url': 'https://example.test/research/paper',
           'report_url': 'https://arxiv.org/pdf/2610.12345v1', 'paper_id': '2610.12345v1',
           'read_status': 'full_text_read', 'summary': 'Original summary',
           'content_hash': 'original-hash', 'supplementary_urls': ['https://example.test/appendix.pdf'],
           **changes}
    row['id'] = report_id(row)
    return row


def run_collect(tmp_path, archive, discoveries, acquired=None, errors=()):
    output = tmp_path / 'candidates.json'
    adapter = SimpleNamespace(discover=lambda *args: {'items': discoveries, 'coverage': {}, 'errors': list(errors)})
    with patch.object(cli, 'load', return_value=archive), \
         patch.object(cli.importlib, 'import_module', return_value=adapter), \
         patch.object(cli, 'refresh_primary_original', side_effect=AssertionError('no historical refresh')), \
         patch.object(acquisition, 'acquire', side_effect=acquired or (lambda item: {**item, 'content_hash': 'new-hash'})) as fetch:
        cli.collect('2026-09-27', '2026-10-04', ['Anthropic'], output)
    return json.loads(output.read_text()), fetch


@pytest.mark.parametrize('rediscovered', [False, True])
@pytest.mark.parametrize('status', ['full_text_read', 'pending_full_text'])
def test_no_new_reports_never_fetches_history_even_pending_or_rediscovered(tmp_path, rediscovered, status):
    old = report(read_status=status)
    archive = {'items': [old]}
    before = copy.deepcopy(archive)
    data, fetch = run_collect(tmp_path, archive, [old] if rediscovered else [])
    fetch.assert_not_called()
    assert data['items'] == [] and data['pending_count'] == data['new_report_count'] == 0
    assert data['source_refresh_errors'] == [] and data['mode'] == 'new_only'
    assert 'skip reader, ingest, Feishu sync, GitHub and Render' in data['agent_instruction']
    assert archive == before


def test_source_failure_remains_visible_without_triggering_historical_downloads(tmp_path):
    error = {'source': 'https://example.test/index', 'error': 'HTTP 403'}
    data, fetch = run_collect(tmp_path, {'items': [report()]}, [], errors=[error])
    fetch.assert_not_called()
    assert len(data['errors']) == 1
    assert data['errors'][0]['vendor'] == 'Anthropic' and 'HTTP 403' in data['errors'][0]['error']


def test_same_paper_new_arxiv_version_is_skipped_before_download(tmp_path):
    old = report()
    edition = report(url='https://arxiv.org/abs/2610.12345v3', paper_id='2610.12345v3',
                     report_url='https://arxiv.org/pdf/2610.12345v3')
    data, fetch = run_collect(tmp_path, {'items': [old]}, [edition])
    fetch.assert_not_called()
    assert data['items'] == []


def test_new_paper_on_existing_announcement_gets_full_original_without_old_attachments(tmp_path):
    old = report()
    fresh = report(title='A distinct new paper', paper_id='2610.67890',
                   report_url='https://arxiv.org/pdf/2610.67890', supplementary_urls=[])
    data, fetch = run_collect(tmp_path, {'items': [old]}, [old, fresh])
    fetch.assert_called_once()
    assert fetch.call_args.args[0]['supplementary_urls'] == []
    saved, = data['items']
    assert saved['id'] == fresh['id'] and saved['read_status'] == 'pending_full_text'
    assert 'summary' not in saved and data['new_report_count'] == 1


def test_new_url_resolving_to_known_paper_does_not_refetch_old_appendices(tmp_path):
    old = report()
    carrier = {'vendor': 'Anthropic', 'title': 'A new carrier', 'source': 'Official research',
               'published_at': '2026-10-02', 'url': 'https://example.test/new-carrier'}
    data, fetch = run_collect(tmp_path, {'items': [old]}, [carrier], acquired=lambda item: {**item,
        'report_url': old['report_url'], 'paper_id': old['paper_id'], 'content_hash': old['content_hash']})
    fetch.assert_called_once()
    assert data['items'] == []


def test_retired_aliases_and_excluded_publications_do_not_return(tmp_path):
    old = report()
    data, fetch = run_collect(tmp_path, {'items': [], 'excluded_reports': {old['id']: old}}, [old])
    fetch.assert_not_called()
    assert not data['items']
    assert KnownReports({'items': [], 'aliases': {old['id']: 'primary'}})(old)


def test_seed_transparency_fetches_only_new_pdf(tmp_path):
    old = report(vendor='Seed', url='https://seed.bytedance.com/old.pdf', paper_id='',
                 report_url='https://seed.bytedance.com/old.pdf')
    new_url = 'https://seed.bytedance.com/new.pdf'
    known = KnownReports({'items': [old]})
    (tmp_path / 'full.txt').write_text('Last update: 2 October 2026\nVersion of the Summary: 1.0')
    page = '<a href="/old.pdf">PDF</a><a href="/new.pdf">PDF</a>'
    with patch.object(google_seed, '_fetch', return_value=page), \
         patch.object(acquisition, 'acquire', side_effect=lambda item: {**item, 'fulltext_path': 'full.txt'}) as fetch, \
         patch('technical_reports.common.ROOT', tmp_path):
        data = google_seed._seed_transparency('2026-09-27', '2026-10-04', known)
    fetch.assert_called_once()
    assert fetch.call_args.args[0]['url'] == new_url
    assert data['items'][0]['url'] == new_url and not data['errors']


def test_deepseek_archived_model_card_is_not_downloaded_for_its_date():
    card = 'https://deepseek.com/model-card-old.pdf'
    known = KnownReports({'items': [report(vendor='DeepSeek', url=card, report_url=card, paper_id='')]})
    def get(url):
        if 'api.github.com' in url:
            return '[]'
        if url.endswith('/transparency/'):
            return f'DeepSeek-V4 2026年10月1日 <a href="{card}">Model card</a>'
        return ''
    with patch.object(china, '_get', side_effect=get), \
         patch.object(china, '_model_card_publication', side_effect=AssertionError('no archived PDF download')) as fetch:
        data = china.discover('DeepSeek', '2026-09-27', '2026-10-04', known)
    fetch.assert_not_called()
    assert not data['items'] and not data['errors']


def test_google_year_index_skips_known_detail_page_and_still_checks_index():
    url = 'https://research.google/pubs/known-agent/'
    known = KnownReports({'items': [report(vendor='Google', url=url)]})
    page = f'<a class="row-card__heading glue-link" href="{url}">Language model agent</a>'
    with patch.object(google_seed, '_fetch', side_effect=lambda target: page if '/pubs/?' in target else
                      pytest.fail('archived detail page must not be fetched: ' + target)) as fetch:
        data = google_seed._google_research('2026-09-27', '2026-10-04', known)
    fetch.assert_called_once()
    assert not data['items'] and not data['errors']


def test_known_undated_candidate_never_triggers_date_or_scope_recovery(tmp_path):
    old = report(vendor='Google')
    data = recovery.recover_undated_discoveries({'Google': {'undated_candidates': [old]}},
        '2026-09-27', '2026-10-04', known=KnownReports({'items': [old]}),
        audit_path=tmp_path / 'audit.json', scope_path=tmp_path / 'scope.json', cache_path=tmp_path / 'cache.json',
        fetcher=lambda url: pytest.fail('archived metadata must not be fetched'))
    assert data['items'] == data['new_date_verified_candidates'] == data['diagnostics'] == []
