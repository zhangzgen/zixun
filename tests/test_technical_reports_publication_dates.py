import json
from urllib.error import HTTPError

import pytest

from technical_reports.publication_dates import (
    arxiv_atom_matches, crossref_evidence, doi_from_url, normalized_title,
    openreview_evidence, precise_date, publisher_html_evidence,
    resolve_publication_date, zenodo_evidence,
)


TITLE = 'A Theoretical Framework for Statistical Evaluability of Generative Models'


def atom(*papers):
    entries = []
    for identifier, published in papers:
        entries.append(f'''<entry><id>http://arxiv.org/abs/{identifier}v3</id>
          <title>{TITLE}</title><published>{published}T01:53:59Z</published>
          <updated>2026-08-17T16:34:31Z</updated><author><name>Fake Author</name></author>
          <link href="https://arxiv.org/pdf/{identifier}v3" type="application/pdf"/></entry>''')
    return '<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(entries) + '</feed>'


def test_crossref_publication_fields_never_use_created_deposited_or_indexed():
    payload = {'message': {'title': [TITLE], 'published-online': {'date-parts': [[2026, 4, 13]]},
                          'published-print': {'date-parts': [[2026, 4, 12]]},
                          'created': {'date-parts': [[2025, 1, 1]]},
                          'deposited': {'date-parts': [[2026, 7, 3]]},
                          'indexed': {'date-parts': [[2026, 8, 1]]}}}
    evidence = crossref_evidence(payload, TITLE, 'https://api.crossref.org/works/fake')
    assert {row['date'] for row in evidence} == {'2026-04-12', '2026-04-13'}
    assert all(row['kind'] == 'publisher_publication' for row in evidence)


def test_imprecise_dates_stay_imprecise_and_invalid_dates_are_rejected():
    assert precise_date([2026])['precision'] == 'year'
    assert precise_date([2026, 4]) == {'date': '2026-04', 'precision': 'month', 'raw': '2026-04'}
    assert precise_date('2026/04/07')['date'] == '2026-04-07'
    assert precise_date('2026/7') == {'date': '2026-07', 'precision': 'month', 'raw': '2026-7'}
    assert precise_date('2026/4/7')['date'] == '2026-04-07'
    assert precise_date('2026-02-30') is None
    assert precise_date('2026-13') is None
    assert precise_date('May 2026') is None


def test_title_matching_preserves_identity_across_markup_and_accents():
    assert normalized_title('high-T<sub>c</sub> Türkiye') == normalized_title('high-Tc Turkiye')
    with pytest.raises(ValueError, match='title does not match'):
        crossref_evidence({'title': ['A different paper'], 'published': {'date-parts': [[2026, 5, 1]]}}, TITLE, 'fake')


def test_arxiv_first_publication_is_not_latest_version_update():
    matches = arxiv_atom_matches(atom(('2604.05324', '2026-04-07')), TITLE, 'https://export.arxiv.org/api/query')
    match, = matches
    assert match['paper_id'] == '2604.05324'
    assert match['evidence']['date'] == '2026-04-07'
    assert match['evidence']['field'] == 'entry.published'
    assert match['updated_at'].startswith('2026-08-17')
    assert match['report_url'].endswith('2604.05324v3')


def test_arxiv_expected_identity_and_authors_reject_same_title_unrelated_paper():
    xml = atom(('2604.05324', '2026-04-07'))
    assert arxiv_atom_matches(xml, TITLE, 'fake', paper_id='2604.99999') == []
    assert arxiv_atom_matches(xml, TITLE, 'fake', authors=['Different Author']) == []
    assert len(arxiv_atom_matches(xml, TITLE, 'fake', authors=['Fake Author'])) == 1


def test_openreview_requires_actual_public_visibility_date():
    base = {'content': {'title': {'value': TITLE}}, 'cdate': 1770076800000, 'mdate': 1782864000000}
    assert openreview_evidence({'notes': [base]}, TITLE, 'fake') == []
    assert openreview_evidence({'notes': [{**base, 'pdate': 1775520000000, 'readers': ['Program_Chairs']}]}, TITLE, 'fake') == []
    public = openreview_evidence({'notes': [{**base, 'pdate': 1775520000000, 'readers': ['everyone']}]}, TITLE, 'fake')
    assert len(public) == 1 and public[0]['field'] == 'pdate'


def test_icml_poster_creation_and_generic_schema_dates_are_not_paper_publication():
    page = f'''<meta name="citation_title" content="{TITLE}">
      <meta name="citation_date" content="2026/05/05">
      <script type="application/ld+json">{{"datePublished":"2026-05-05","dateModified":"2026-08-01"}}</script>'''
    assert publisher_html_evidence(page, TITLE, 'https://icml.cc/virtual/2026/poster/65788') == []
    assert publisher_html_evidence(f'<meta name="citation_title" content="{TITLE}"><script>{{"dateModified":"2026-08-01"}}</script>', TITLE, 'https://publisher.test/article') == []


def test_zenodo_uses_explicit_publication_date_and_checks_title():
    payload = {'created': '2026-03-01', 'metadata': {'title': TITLE, 'publication_date': '2026-04-13'}}
    evidence, = zenodo_evidence(payload, TITLE, 'https://zenodo.org/api/records/fake')
    assert evidence['date'] == '2026-04-13'
    assert evidence['field'] == 'metadata.publication_date'
    with pytest.raises(ValueError):
        zenodo_evidence(payload, 'Unrelated paper', 'fake')


def test_lookup_publisher_only_does_not_claim_verified_first_publication():
    record = {'title': TITLE, 'report_urls': ['https://doi.org/10.1234/fake']}
    def fetch(url):
        if 'api.crossref.org' in url:
            return json.dumps({'message': {'title': [TITLE], 'published-online': {'date-parts': [[2026, 4, 13]]}}})
        raise OSError('offline fake source')
    result = resolve_publication_date(record, fetcher=fetch)
    assert result['status'] == 'publisher_date_only'
    assert result['published_at'] == '2026-04-13'
    assert not result['first_publication_verified']
    assert result['arxiv_search_status'] == 'unavailable'


def test_lookup_follows_conference_openreview_but_arxiv_initial_date_wins():
    record = {'title': TITLE, 'report_urls': ['https://icml.cc/virtual/2026/poster/65788']}
    calls = []
    def fetch(url):
        calls.append(url)
        if 'icml.cc/' in url:
            return '<script>{"datePublished":"2026-05-05"}</script><a href="https://openreview.net/forum?id=fake">Paper</a>'
        if 'api2.openreview.net/' in url:
            return json.dumps({'notes': [{'content': {'title': {'value': TITLE}}, 'pdate': 1782864000000, 'readers': ['everyone']}]})
        if 'export.arxiv.org' in url:
            return atom(('2604.05324', '2026-04-07'))
        raise AssertionError(url)
    result = resolve_publication_date(record, fetcher=fetch)
    assert result['status'] == 'verified_first_publication' and result['first_publication_verified']
    assert result['published_at'] == '2026-04-07'
    assert result['paper_id'] == '2604.05324'
    assert any('api2.openreview.net' in url for url in calls)
    assert not any(row['date'] == '2026-05-05' for row in result['publication_date_evidence'])


def test_exact_title_ambiguity_is_not_resolved_by_picking_first_search_hit():
    result = resolve_publication_date({'title': TITLE}, fetcher=lambda url: atom(('2604.05324', '2026-04-07'), ('2604.99999', '2026-04-08')))
    assert result['status'] == 'ambiguous_arxiv_identity'
    assert not result['first_publication_verified'] and result['published_at'] is None


def test_crossref_month_does_not_become_first_day_of_month():
    def fetch(url):
        if 'api.crossref.org' in url:
            return json.dumps({'message': {'title': [TITLE], 'published': {'date-parts': [[2026, 4]]}}})
        return '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    result = resolve_publication_date({'title': TITLE, 'report_urls': ['https://doi.org/10.1234/fake']}, fetcher=fetch)
    assert result['published_at'] is None and result['status'] == 'unresolved'
    assert result['publication_date_evidence'][0]['precision'] == 'month'


def test_acm_query_identifier_and_arxiv_version_url():
    assert doi_from_url('https://doi.acm.org?doi=3786583.3786858') == '10.1145/3786583.3786858'
    assert doi_from_url('https://dl.acm.org/doi/pdf/10.1145/3786175.3788348') == '10.1145/3786175.3788348'


def test_tdcommons_explicit_release_field_and_citation_are_publication_evidence():
    page = f'<meta property="og:title" content="{TITLE}"><h3>Publication Date</h3><p>2026-01-07</p>'
    row, = publisher_html_evidence(page, TITLE, 'https://www.tdcommons.org/dpubs_series/9132/')
    assert row['date'] == '2026-01-07' and row['field'] == 'Publication Date'
    page = f'<meta property="og:title" content="{TITLE}"><p>Technical Disclosure Commons, (March 24, 2026)</p><p>Since October 02, 2026</p>'
    row, = publisher_html_evidence(page, TITLE, 'https://www.tdcommons.org/dpubs_series/9605/')
    assert row['date'] == '2026-03-24'
    assert row['field'] == 'Recommended Citation publication date'
    with pytest.raises(ValueError):
        publisher_html_evidence(page, 'Different paper', 'https://www.tdcommons.org/dpubs_series/9605/')


def test_acl_month_metadata_is_preserved_without_inventing_a_day():
    page = f'<meta name="citation_title" content="{TITLE}"><meta name="citation_publication_date" content="2026/7">'
    row, = publisher_html_evidence(page, TITLE, 'https://aclanthology.org/2026.acl-industry.142/')
    assert row['date'] == '2026-07' and row['precision'] == 'month'


def test_default_fetcher_spaces_arxiv_requests_without_delaying_publishers(monkeypatch):
    from technical_reports import publication_dates as dates
    clock = [100.0]
    waits = []
    monkeypatch.setattr(dates, '_arxiv_next_request', 0.0)
    monkeypatch.setattr(dates, '_arxiv_blocked_until', 0.0)
    monkeypatch.setattr(dates.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(dates.time, 'sleep', sleep)
    monkeypatch.setattr(dates, 'fetch_bytes', lambda url: (b'complete metadata', url, 'text/plain'))
    dates._fetch_text('https://export.arxiv.org/api/query?id_list=2604.05324')
    dates._fetch_text('https://api.crossref.org/works/10.1234/fake')
    assert waits == []
    dates._fetch_text('https://export.arxiv.org/api/query?id_list=2604.05364')
    assert waits == [3.0]


def test_arxiv_429_defers_queued_titles_and_preserves_status_code(monkeypatch):
    from technical_reports import publication_dates as dates
    monkeypatch.setattr(dates, '_arxiv_next_request', 0.0)
    monkeypatch.setattr(dates, '_arxiv_blocked_until', 0.0)
    monkeypatch.setattr(dates.time, 'monotonic', lambda: 100.0)
    requests = []
    def fetch(url):
        requests.append(url)
        raise HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '120'}, None)
    monkeypatch.setattr(dates, 'fetch_bytes', fetch)
    first = dates.resolve_publication_date({'title': TITLE})
    second = dates.resolve_publication_date({'title': 'Another title'})
    assert len(requests) == 1
    assert first['errors'][0]['status_code'] == 429
    assert second['status'] == 'unresolved' and second['arxiv_search_status'] == 'unavailable'
    assert dates._arxiv_blocked_until == 220.0
