import json
from pathlib import Path
import pytest

from technical_reports import publication_recovery as recovery


TITLE = 'An Evaluated Language Model Method'
URL = 'https://research.google/pubs/evaluated-language-model-method/'
DAY = '2026-09-30'
CHECKED = '2026-10-03T10:00:00+08:00'


def candidate(title=TITLE, url=URL, **changes):
    return {'title': title, 'url': url, **changes}


def atom(title=TITLE, published=DAY):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/abs/2609.12345v2</id><title>{title}</title>
      <published>{published}T01:00:00Z</published><updated>2026-10-02T01:00:00Z</updated>
      <author><name>Fake Author</name></author>
      <link href="https://arxiv.org/pdf/2609.12345v2" type="application/pdf"/>
      </entry></feed>'''


def paths(tmp_path):
    return {'audit_path': tmp_path / 'audit.json', 'scope_path': tmp_path / 'scope.json',
            'cache_path': tmp_path / 'cache.json', 'checked_at': CHECKED}


def write(path, payload):
    Path(path).write_text(json.dumps(payload), encoding='utf-8')


def scope(path, status='eligible', title=TITLE, url=URL):
    return recovery.record_scope_decision('Google', title, url, status,
        'Original implements and evaluates a language-model method', decisions_path=path,
        evidence_urls=['https://arxiv.org/abs/2609.12345'])


def recover(coverage, **kwargs):
    return recovery.recover_undated_discoveries(coverage, '2026-09-27', '2026-10-02', **kwargs)


def test_unknown_exact_date_is_reviewed_before_original_acquisition_and_next_run_recovers(tmp_path):
    options = paths(tmp_path)
    coverage = {'Google': {'google_research_supplement': {'undated_candidates': [candidate()]}}}
    calls = []
    def fetch(url):
        calls.append(url)
        return atom()
    first = recover(coverage, fetcher=fetch, **options)
    assert first['items'] == []
    review, = first['new_date_verified_candidates']
    assert review['published_at'] == DAY and review['first_publication_verified']
    assert review['scope_status'] == 'not_reviewed'
    assert not first['complete_candidate_resolution']
    scope(options['scope_path'])
    second = recover(coverage, fetcher=lambda url: pytest.fail('verified date must use cache'), **options)
    item, = second['items']
    assert item['published_at'] == DAY and item['scope_status'] == 'eligible'
    assert item['report_url'] == 'https://arxiv.org/pdf/2609.12345'
    assert item['paper_id'] == '2609.12345'
    assert item['date_recovered'] and 'summary' not in item and 'reading_evidence' not in item
    assert second['stats']['cache_hit_count'] == second['stats']['recovered_count'] == 1
    assert second['complete_candidate_resolution']
    assert len(calls) == 1


def test_scope_exclusion_is_persistent_and_never_silently_becomes_reader_candidate(tmp_path):
    options = paths(tmp_path)
    scope(options['scope_path'], 'excluded')
    result = recover({'Google': {'undated_candidates': [candidate()]}},
                     fetcher=lambda url: pytest.fail('scope-excluded candidate requires no lookup'), **options)
    assert result['items'] == result['new_date_verified_candidates'] == []
    assert result['stats']['excluded_scope_count'] == 1
    assert result['diagnostics'][0]['status'] == 'excluded_scope'


def test_scope_decision_does_not_transfer_to_new_title_at_old_url(tmp_path):
    options = paths(tmp_path)
    scope(options['scope_path'])
    title = 'A Different Language Model Method'
    result = recover({'Google': {'undated_candidates': [candidate(title=title)]}},
                     fetcher=lambda url: atom(title), **options)
    assert not result['items']
    assert result['new_date_verified_candidates'][0]['scope_status'] == 'not_reviewed'


def test_only_explicit_lists_are_processed_and_nested_duplicates_merge_sources(tmp_path):
    options = paths(tmp_path)
    calls = []
    coverage = {'Google': {'excluded': [candidate(title='Excluded cited title')],
        'references': [candidate(title='Cited title')], 'undated_candidates': [candidate()],
        'nested': {'unverified_date_candidates': [candidate(report_url='https://arxiv.org/pdf/2609.12345')]}}}
    def fetch(url):
        calls.append(url)
        return atom()
    result = recover(coverage, fetcher=fetch, **options)
    assert result['stats']['candidate_count'] == 1
    assert len(calls) == 1 and 'id_list=2609.12345' in calls[0]
    assert result['diagnostics'][0]['discovery_paths'] == [
        'Google.undated_candidates', 'Google.nested.unverified_date_candidates']


def test_publisher_only_day_inside_window_stays_pending_despite_eligible_scope(tmp_path):
    options = paths(tmp_path)
    scope(options['scope_path'])
    def fetch(url):
        if 'crossref' in url:
            return json.dumps({'message': {'title': [TITLE], 'published': {'date-parts': [[2026, 9, 30]]}}})
        return '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    result = recover({'Google': {'undated_candidates': [candidate(report_urls=['https://doi.org/10.1234/fake'])]}},
                     fetcher=fetch, **options)
    assert not result['items'] and not result['new_date_verified_candidates']
    diagnostic, = result['diagnostics']
    assert diagnostic['status'] == 'pending_first_publication_verification'
    assert diagnostic['date_lookup']['published_at'] == DAY
    assert not diagnostic['date_lookup']['first_publication_verified']


@pytest.mark.parametrize('publication_parts,upper_bound', [([2026, 8], '2026-08-31'), ([2025], '2025-12-31')])
def test_imprecise_publisher_dates_prove_outside_window_without_filling_first_day(tmp_path, publication_parts, upper_bound):
    options = paths(tmp_path)
    def fetch(url):
        if 'crossref' in url:
            return json.dumps({'message': {'title': [TITLE], 'published': {'date-parts': [publication_parts]}}})
        return '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    result = recover({'Google': {'undated_candidates': [candidate(report_urls=['https://doi.org/10.1234/fake'])]}},
                     fetcher=fetch, **options)
    diagnostic, = result['diagnostics']
    assert diagnostic['status'] == 'outside_window'
    assert diagnostic['first_publication_upper_bound'] == upper_bound
    assert diagnostic['date_lookup']['published_at'] is None
    assert diagnostic['date_lookup']['publication_date_evidence'][0]['precision'] != 'day'


def test_later_arxiv_first_day_does_not_validate_earlier_publisher_as_exact_first_release(tmp_path):
    options = paths(tmp_path)
    scope(options['scope_path'])
    def fetch(url):
        if 'crossref' in url:
            return json.dumps({'message': {'title': [TITLE], 'published': {'date-parts': [[2026, 9, 28]]}}})
        return atom()
    result = recover({'Google': {'undated_candidates': [candidate(report_urls=['https://doi.org/10.1234/fake'])]}},
                     fetcher=fetch, **options)
    assert result['items'] == result['new_date_verified_candidates'] == []
    assert result['stats']['pending_date_count'] == 1


def test_cached_public_upper_bound_remains_outside_after_retry_interval(tmp_path):
    options = paths(tmp_path)
    coverage = {'Google': {'undated_candidates': [candidate(report_urls=['https://doi.org/10.1234/fake'])]}}
    def fetch(url):
        if 'crossref' in url:
            return json.dumps({'message': {'title': [TITLE], 'published': {'date-parts': [[2026, 3, 20]]}}})
        return '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    first = recover(coverage, fetcher=fetch, **options)
    assert first['stats']['outside_window_count'] == 1
    second = recover(coverage, fetcher=lambda url: pytest.fail('a public release bound cannot become later'),
                     **{**options, 'checked_at': '2026-10-06T10:00:00+08:00'})
    assert second['stats']['cache_hit_count'] == second['stats']['outside_window_count'] == 1
    assert second['diagnostics'][0]['first_publication_upper_bound'] == '2026-03-20'


def test_failed_lookup_retries_after_24_hours_and_new_sources_retry_immediately(tmp_path):
    options = paths(tmp_path)
    coverage = {'Google': {'undated_candidates': [candidate()]}}
    first = recover(coverage, fetcher=lambda url: (_ for _ in ()).throw(OSError('offline')), **options)
    assert first['stats']['lookup_count'] == first['stats']['pending_date_count'] == 1
    second = recover(coverage, fetcher=lambda url: pytest.fail('negative metadata within TTL must be cached'), **options)
    assert second['stats']['cache_hit_count'] == 1
    next_day = {**options, 'checked_at': '2026-10-04T10:00:01+08:00'}
    third = recover(coverage, fetcher=lambda url: atom(), **next_day)
    assert third['stats']['lookup_count'] == third['stats']['scope_review_count'] == 1
    # A changed original source invalidates the old cache without waiting.
    changed = {'Google': {'undated_candidates': [candidate(report_urls=['https://arxiv.org/pdf/2609.12345'])]}}
    fourth = recover(changed, fetcher=lambda url: atom(), **next_day)
    assert fourth['stats']['lookup_count'] == 1


def test_curated_duplicate_and_public_upper_bound_skip_network_but_are_window_specific(tmp_path):
    options = paths(tmp_path)
    audit = [
        {**candidate(), 'vendor': 'Google', 'scope_status': 'eligible',
         'first_publication_upper_bound': '2026-03-27',
         'date_bound_evidence': [{'source': 'https://public.test/unedited-proof', 'date': '2026-03-27'}]},
        {**candidate(title='Duplicate title', url=URL + 'duplicate/'), 'vendor': 'Google',
         'scope_status': 'duplicate', 'duplicate_of': URL},
    ]
    write(options['audit_path'], {'items': audit})
    coverage = {'Google': {'undated_candidates': [candidate(), candidate(title='Duplicate title', url=URL + 'duplicate/')]}}
    result = recover(coverage, fetcher=lambda url: pytest.fail('known outside/duplicate must not fetch'), **options)
    assert result['stats']['outside_window_count'] == result['stats']['duplicate_count'] == 1
    assert result['diagnostics'][0]['first_publication_upper_bound'] == '2026-03-27'
    earlier = recovery.recover_undated_discoveries({'Google': {'undated_candidates': [candidate()]}},
        '2026-03-01', '2026-04-01', fetcher=lambda url: atom(published='2026-03-20'), **options)
    assert earlier['items'][0]['published_at'] == '2026-03-20'


def test_frozen_google_audit_recovers_only_four_reviewed_new_papers_and_keeps_gaps(tmp_path):
    options = paths(tmp_path)
    options['audit_path'] = recovery.PUBLIC_AUDIT
    audit = json.loads(recovery.PUBLIC_AUDIT.read_text(encoding='utf-8'))
    coverage = {'Google': {'undated_candidates': [candidate(row['title'], row['url'], report_urls=row['report_urls'])
        for row in audit['items']]}}
    # Audit lookups are deliberately still within the retry interval for this
    # regression; ongoing collection will retry unresolved sources after a day.
    options['checked_at'] = audit['updated_at']
    result = recovery.recover_undated_discoveries(coverage, '2026-04-02', '2026-10-02',
        fetcher=lambda url: pytest.fail('frozen audit regression must not call external APIs'), **options)
    assert result['stats'] == {'candidate_count': 83, 'lookup_count': 0, 'cache_hit_count': 0,
        'recovered_count': 4, 'scope_review_count': 0, 'excluded_scope_count': 27,
        'duplicate_count': 4, 'outside_window_count': 33, 'pending_date_count': 15}
    assert {item['paper_id'] for item in result['items']} == {'2604.05324', '2607.20268', '2604.05364', '2608.05578'}
    assert not result['complete_candidate_resolution']


def test_scope_cli_records_metadata_only_and_updates_same_identity(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'scope.json'
    monkeypatch.setattr('sys.argv', ['publication_recovery', '--record-scope', 'uncertain',
        '--vendor', 'Google', '--title', TITLE, '--url', URL, '--reason', 'Full original still needed',
        '--evidence-url', 'https://official.test/paper', '--decisions-path', str(path)])
    recovery.main()
    assert json.loads(capsys.readouterr().out)['decisions_path'] == str(path)
    scope(path)
    saved = json.loads(path.read_text())
    record, = saved['items']
    assert record['scope_status'] == 'eligible'
    assert record['scope_evidence_urls'] == ['https://arxiv.org/abs/2609.12345']
    assert not {'summary', 'fulltext', 'reading_evidence'} & record.keys()


@pytest.mark.parametrize('changes', [{'scope_status': 'auto'}, {'title': ''}, {'url': 'file:///private/local'}, {'reason': ''}])
def test_invalid_scope_decisions_are_rejected(tmp_path, changes):
    args = {'vendor': 'Google', 'title': TITLE, 'url': URL, 'scope_status': 'eligible', 'reason': 'Reviewed original'}
    with pytest.raises(ValueError):
        recovery.record_scope_decision(**{**args, **changes}, decisions_path=tmp_path / 'scope.json')
