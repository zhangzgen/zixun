import copy
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from technical_reports import __main__ as cli
from technical_reports import acquire as acquisition
from technical_reports import primary_refresh as refresh
from technical_reports.store import report_id


def report(**changes):
    item = {
        'vendor': 'Anthropic', 'url': 'https://www.anthropic.com/research/fake-paper',
        'report_url': 'https://www-cdn.anthropic.com/old-paper.pdf',
        'title': 'Fake paper', 'source': 'Anthropic official research', 'published_at': '2026-09-01',
        'read_status': 'full_text_read', 'content_hash': 'read-original-hash',
        'overview': 'Verified fake overview', 'summary': 'Verified fake summary',
        'reading_evidence': {'sha256': 'read-original-hash', 'reader': 'agent'},
        **changes,
    }
    item['id'] = report_id(item)
    return item


def check_page(item, page, **kwargs):
    with patch.object(refresh, 'fetch_bytes', return_value=(page.encode(), item['url'], 'text/html')) as fetch:
        result = refresh.refresh_primary_original(item, **kwargs)
    assert fetch.call_count == 1
    return result


def test_explicit_download_replaces_old_cdn_and_keeps_own_appendix_only():
    item = report()
    result = check_page(item, '''<h1>Fake paper</h1>
      <p>Prior work used <a href="https://example.test/cited.pdf">an earlier paper</a>.</p>
      <a href="https://cdn.sanity.io/new-paper.pdf">Read in PDF</a>
      <a href="https://www-cdn.anthropic.com/appendix.pdf">Our appendix</a>
      <h2>References</h2><a href="https://example.test/reference.pdf">Download PDF</a>''')
    checked = result['item']
    assert checked['report_url'] == 'https://cdn.sanity.io/new-paper.pdf'
    assert checked['supplementary_urls'] == ['https://www-cdn.anthropic.com/appendix.pdf']
    assert checked['primary_link_check_status'] == 'refreshed'
    assert checked['summary'] == item['summary']  # Fulltext/hash comparison belongs to collect.
    assert result['source_refresh_error'] is None
    assert item['report_url'].endswith('old-paper.pdf')


def test_multiple_downloads_without_entity_match_are_unresolved():
    item = report()
    result = check_page(item, '<h1>Research</h1><a href="/a.pdf">Download PDF</a><a href="/b.pdf">Download PDF</a>')
    assert result['item']['report_url'] == item['report_url']
    assert result['item']['primary_link_check_status'] == 'unresolved'
    assert len(result['source_refresh_error']['candidate_urls']) == 2


def test_shared_blog_matches_paper_id_and_only_its_section_appendix():
    item = report(paper_id='2609.12345v1', report_url='https://arxiv.org/pdf/2609.12345v1')
    page = '''<h1>Two research papers</h1>
      <h2>Other paper</h2><a href="https://arxiv.org/abs/2609.67890">Paper</a>
      <h3>Appendix</h3><a href="/other-appendix.pdf">Download appendix</a>
      <h2>Fake paper</h2><a href="https://arxiv.org/abs/2609.12345v2">Paper</a>
      <h3>Appendix</h3><a href="/our-appendix.pdf">Download appendix</a>'''
    result = check_page(item, page, shared_landing=True)
    assert result['item']['report_url'] == 'https://arxiv.org/pdf/2609.12345v2'
    assert result['item']['supplementary_urls'] == ['https://www.anthropic.com/our-appendix.pdf']
    assert result['source_refresh_error'] is None


def test_shared_blog_matches_audited_canonical_pdf_even_when_it_is_second():
    item = report(canonical_url='https://www.anthropic.com/a.pdf', report_url='https://www.anthropic.com/a.pdf')
    result = check_page(item, '''<h1>Two papers</h1>
      <h2>Paper B</h2><a href="/b.pdf">Download PDF</a>
      <h2>Paper A</h2><a href="/a.pdf">Download PDF</a>
      <h3>Appendix</h3><a href="/a-appendix.pdf">Download appendix</a>''', shared_landing=True)
    assert result['item']['report_url'] == item['report_url']
    assert result['item']['supplementary_urls'] == ['https://www.anthropic.com/a-appendix.pdf']
    assert result['source_refresh_error'] is None


def test_unassigned_shared_appendix_requires_browser_recovery():
    item = report(paper_id='2609.12345', report_url='https://arxiv.org/pdf/2609.12345')
    result = check_page(item, '''<h1>Two papers</h1>
      <a href="https://arxiv.org/abs/2609.12345">Paper</a>
      <a href="https://arxiv.org/abs/2609.67890">Paper</a>
      <a href="/unknown-appendix.pdf">Download appendix</a>''', shared_landing=True)
    assert result['item']['primary_link_check_status'] == 'unresolved'
    assert 'uniquely assigned' in result['source_refresh_error']['error']
    assert 'supplementary_urls' not in result['item']


@pytest.mark.parametrize('page', [
    '<h1>Fake paper</h1><h2>References</h2><a href="/citation.pdf">Download PDF</a>',
    '<h1>Fake paper</h1><a href="https://untrusted.test/new.pdf">Download PDF</a>',
    '<h1>Fake paper</h1><a href="/main.pdf">Download PDF</a><a href="https://untrusted.test/appendix.pdf">Our appendix</a>',
    '<h1>Fake paper</h1><a href="https://[invalid">Download PDF</a>',
])
def test_citations_untrusted_downloads_and_malformed_links_preserve_history(page):
    item = report()
    result = check_page(item, page)
    assert result['item']['report_url'] == item['report_url']
    assert result['item']['summary'] == item['summary']
    assert result['source_refresh_error']['status'] == 'unresolved'


def test_different_arxiv_identity_is_separate_pending_metadata_without_old_title_or_date():
    item = report(paper_id='2609.12345', report_url='https://arxiv.org/pdf/2609.12345')
    result = check_page(item, '<h1>New research</h1><a href="https://arxiv.org/abs/2609.67890">Paper</a>')
    assert result['item']['id'] == item['id']
    assert result['item']['report_url'] == item['report_url']
    candidate = result['new_primary_candidate']
    assert candidate['id'] != item['id']
    assert candidate['paper_id'] == '2609.67890'
    assert candidate['landing_title'] == item['title']
    assert candidate['landing_published_at'] == item['published_at']
    assert candidate['read_status'] == 'pending_metadata' and candidate['metadata_needs_review']
    assert not {'title', 'published_at', 'summary', 'overview', 'reading_evidence'} & candidate.keys()
    assert result['source_refresh_error']['status'] == 'new_entity'


def test_explicit_new_pdf_overrides_stale_citation_metadata():
    item = report()
    result = check_page(item, '''<meta name="citation_title" content="Fake paper">
      <meta name="citation_pdf_url" content="https://www-cdn.anthropic.com/old-paper.pdf">
      <h1>Fake paper</h1><a href="/current.pdf">Download PDF</a>''')
    assert result['item']['report_url'] == 'https://www.anthropic.com/current.pdf'


def test_matching_official_citation_metadata_is_a_valid_fallback():
    item = report()
    result = check_page(item, '''<meta name="citation_title" content="Fake paper">
      <meta name="citation_pdf_url" content="https://www-cdn.anthropic.com/current.pdf">''')
    assert result['item']['report_url'] == 'https://www-cdn.anthropic.com/current.pdf'
    assert result['item']['primary_link_check_evidence']['selected_link_evidence'][0]['method'] == 'official_citation_metadata'


def test_http_403_preserves_verified_original_and_returns_specific_source_error():
    item = report()
    error = urllib.error.HTTPError(item['url'], 403, 'Forbidden', None, None)
    with patch.object(refresh, 'fetch_bytes', side_effect=error):
        result = refresh.refresh_primary_original(item)
    assert result['item']['report_url'] == item['report_url']
    assert result['item']['summary'] == item['summary']
    assert result['source_refresh_error']['url'] == item['url']
    assert result['source_refresh_error']['report_url'] == item['report_url']
    assert result['source_refresh_error']['status'] == 'blocked'
    assert 'HTTP 403' in result['source_refresh_error']['error']


def test_openai_deployment_safety_link_is_refreshed():
    item = report(vendor='OpenAI', url='https://deploymentsafety.openai.com/fake-model', report_url='https://cdn.openai.com/old.pdf')
    result = check_page(item, '<h1>Fake model system card</h1><a href="/fake-model/new.pdf">View PDF</a>')
    assert result['item']['report_url'] == 'https://deploymentsafety.openai.com/fake-model/new.pdf'


def test_non_official_and_direct_originals_do_not_fetch_a_landing():
    for item in [report(url='https://untrusted.test/landing'), report(url='https://www-cdn.anthropic.com/old-paper.pdf')]:
        with patch.object(refresh, 'fetch_bytes', side_effect=AssertionError('must not fetch')):
            assert refresh.refresh_primary_original(item)['item'] == item


def collect_fixture(tmp_path, item, discoveries, fake_acquire, archive_fields=None):
    output = tmp_path / 'candidates.json'
    adapter = SimpleNamespace(discover=lambda *args: {'items': discoveries})
    with patch.object(cli, 'load', return_value={'items': [item], **(archive_fields or {})}), \
         patch.object(acquisition, 'acquire', side_effect=fake_acquire), \
         patch.object(cli.importlib, 'import_module', return_value=adapter):
        result = cli.collect('2026-09-27', '2026-10-02', ['Anthropic'], output)
    return result, json.loads(output.read_text())


def test_collect_archive_only_cdn_recheck_fetches_new_original_and_requires_reading(tmp_path):
    item = report()
    fetched = []
    def acquire(candidate):
        fetched.append(candidate['report_url'])
        return {**copy.deepcopy(candidate), 'read_status': 'pending_full_text', 'content_hash': 'revised-hash', 'error': None}
    with patch.object(refresh, 'fetch_bytes', return_value=(b'<a href="/new.pdf">Download PDF</a>', item['url'], 'text/html')):
        result, payload = collect_fixture(tmp_path, item, [], acquire)
    saved, = payload['items']
    assert fetched == ['https://www.anthropic.com/new.pdf']
    assert saved['id'] == item['id']
    assert saved['read_status'] == 'pending_full_text' and 'summary' not in saved
    assert saved['primary_link_check_status'] == 'refreshed'
    assert result['source_refresh_errors'] == [] and result['pending'] == 1


def test_collect_failed_landing_retains_read_pdf_and_lists_browser_recovery_source(tmp_path):
    item = report()
    def acquire(candidate):
        return {**copy.deepcopy(candidate), 'read_status': 'pending_full_text', 'error': None}
    with patch.object(refresh, 'fetch_bytes', side_effect=urllib.error.HTTPError(item['url'], 403, 'Forbidden', None, None)):
        result, payload = collect_fixture(tmp_path, item, [], acquire)
    saved, = payload['items']
    assert saved['read_status'] == 'full_text_read' and saved['summary'] == item['summary']
    assert saved['primary_link_check_status'] == 'blocked'
    assert result['pending'] == result['fetch_failed_count'] == 0
    assert len(result['source_refresh_errors']) == 1
    assert payload['source_refresh_errors'] == result['source_refresh_errors']


def test_collect_new_arxiv_candidate_never_reuses_old_publication_metadata_in_items(tmp_path):
    item = report(paper_id='2609.12345', report_url='https://arxiv.org/pdf/2609.12345')
    def acquire(candidate):
        return {**copy.deepcopy(candidate), 'read_status': 'pending_full_text', 'error': None}
    with patch.object(refresh, 'fetch_bytes', return_value=(b'<a href="https://arxiv.org/abs/2609.67890">Paper</a>', item['url'], 'text/html')):
        result, payload = collect_fixture(tmp_path, item, [], acquire)
    saved, = payload['items']
    assert saved['id'] == item['id'] and saved['summary'] == item['summary']
    candidate, = payload['new_primary_candidates']
    assert candidate['id'] != item['id'] and not {'title', 'published_at'} & candidate.keys()
    assert result['new_primary_candidate_count'] == 1


def test_collect_fresh_landing_does_not_run_duplicate_source_refresh(tmp_path):
    item = report()
    fresh = {key: item[key] for key in ('vendor', 'url', 'title', 'source')}
    fresh['published_at'] = '2026-10-01'
    def acquire(candidate):
        return {**copy.deepcopy(candidate), 'report_url': item['report_url'], 'content_hash': item['content_hash'], 'error': None}
    with patch.object(cli, 'refresh_primary_original', side_effect=AssertionError('fresh landing is already acquired')):
        _, payload = collect_fixture(tmp_path, item, [fresh], acquire)
    assert len(payload['items']) == 1 and payload['items'][0]['read_status'] == 'full_text_read'


@pytest.mark.parametrize('retirement', ['aliases', 'excluded_reports'])
def test_refresh_does_not_queue_new_metadata_for_an_audited_retired_paper(tmp_path, retirement):
    item = report(paper_id='2609.12345', report_url='https://arxiv.org/pdf/2609.12345')
    retired_id = report_id({**item, 'paper_id': '2609.67890'})
    def acquire(candidate):
        return {**copy.deepcopy(candidate), 'read_status': 'pending_full_text', 'error': None}
    with patch.object(refresh, 'fetch_bytes', return_value=(b'<a href="https://arxiv.org/abs/2609.67890">Paper</a>', item['url'], 'text/html')):
        result, payload = collect_fixture(tmp_path, item, [], acquire, {retirement: {retired_id: 'audited'}})
    assert payload['new_primary_candidates'] == []
    assert result['new_primary_candidate_count'] == 0
    assert result['source_refresh_errors'][0]['new_identity_state'] == 'retired'


@pytest.mark.parametrize('location', ['archive', 'acquired'])
def test_uncertified_missing_pdf_page_cannot_reuse_previous_summary(tmp_path, location):
    omitted = [{'kind': 'pdf', 'empty_pages': [2], 'extraction_evidence': {}}]
    item = report(url='https://example.test/paper', original_documents=omitted if location == 'archive' else [])
    def acquire(candidate):
        return {**copy.deepcopy(candidate), 'original_documents': omitted, 'error': None}
    _, payload = collect_fixture(tmp_path, item, [], acquire)
    assert payload['items'][0]['read_status'] == 'pending_full_text'
    assert 'summary' not in payload['items'][0]
