import json
from copy import deepcopy

import pytest

from agent_frameworks import collector, pipeline
from technical_reports.common import digest, write_json
from technical_reports.renderer import report_blocks
import task_email


def feed(title='v1', date='2026-10-03T01:00:00Z', body='Complete official release notes with useful changes.'):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>{title}</title>
    <updated>{date}</updated><link href="https://github.com/owner/repo/releases/tag/{title}"/>
    <content type="html">&lt;p&gt;{body}&lt;/p&gt;</content></entry></feed>'''


SOURCE = {'id': 'sample', 'name': '版本更新', 'url': 'https://github.com/owner/repo/releases', 'kind': 'atom'}
LOWER, UPPER = '2026-10-03T00:00:00+08:00', '2026-10-03T12:00:00+08:00'


def test_atom_updated_timestamp_is_not_claimed_as_first_publication():
    row = collector.feed_entries(feed())[0]
    assert row['published_at'] is None
    assert row['event_at'] == '2026-10-03T09:00:00+08:00'
    assert row['date_basis'] == 'feed_updated'


def test_no_history_no_future_and_no_repeat(monkeypatch):
    text = feed()[:-7] + feed('old', '2026-10-01T01:00:00Z').split('<entry>')[1].split('</feed>')[0].join(['<entry>', '']) + '</feed>'
    monkeypatch.setattr(collector, 'fetch', lambda url: (text, url, 'application/atom+xml'))
    rows, checkpoint, errors = collector.discover(SOURCE, {}, LOWER, UPPER)
    assert [r['title'] for r in rows] == ['v1']
    assert not errors
    assert 'Complete official release notes' in rows[0]['fulltext']
    assert not collector.discover(SOURCE, checkpoint, LOWER, UPPER)[0]
    monkeypatch.setattr(collector, 'fetch', lambda url: (feed('future', '2026-10-04T01:00:00Z'), url, 'xml'))
    rows, checkpoint, _ = collector.discover(SOURCE, {}, LOWER, UPPER)
    assert not rows and not checkpoint['seen']


def test_release_revision_reenters_when_content_changes(monkeypatch):
    monkeypatch.setattr(collector, 'fetch', lambda url: (feed(), url, 'xml'))
    _, checkpoint, _ = collector.discover(SOURCE, {}, LOWER, UPPER)
    monkeypatch.setattr(collector, 'fetch', lambda url: (feed(body='New correction in the same release log.'), url, 'xml'))
    rows, _, _ = collector.discover(SOURCE, checkpoint, LOWER, UPPER)
    assert rows[0]['publication_event'] == 'revision'


def test_article_card_never_borrows_neighbour_date():
    raw = '<div><a href="/blog/a">A Sep 29, 2026</a><a href="/blog/b">B Oct 3, 2026</a></div>'
    rows = collector.directory_entries(raw, 'https://example.com/blog', {'path': '/blog/'})
    assert rows[0]['event_at'].startswith('2026-09-29')
    assert rows[1]['event_at'].startswith('2026-10-03')


def test_footer_dates_and_exclusion_of_pagination():
    raw = '<div class="Post"><p><a href="/blog/a">A</a></p><footer>Oct 3, 2026</footer></div><a href="/blog/page/2">Next</a>'
    rows = collector.directory_entries(raw, 'https://example.com/blog', {'path': '/blog/'})
    assert len(rows) == 1 and rows[0]['event_at'].startswith('2026-10-03')


def test_filtered_blog_with_no_relevant_posts_is_not_a_parse_failure():
    rows = collector.directory_entries('<a href="/blog/other">Other technology Oct 3, 2026</a>',
                                       'https://example.com/blog', {'path': '/blog/', 'title_filter': 'smolagents'})
    assert rows == []
    with pytest.raises(ValueError, match='No article links'):
        collector.directory_entries('<p>Login required</p>', 'https://example.com/blog',
                                    {'path': '/blog/', 'title_filter': 'smolagents'})


def test_undated_initial_baseline_and_later_discovery_are_distinct(monkeypatch):
    source = {**SOURCE, 'kind': 'index', 'path': '/blog/'}
    page = '<a href="/blog/old">Existing article</a>'
    monkeypatch.setattr(collector, 'fetch', lambda url: (page, 'https://example.com/blog', 'html'))
    rows, checkpoint, failures = collector.discover(source, {}, LOWER, UPPER)
    assert not rows and not failures and len(checkpoint['initial_undated_baseline']) == 1
    page += '<a href="/blog/new">New undated article</a>'
    monkeypatch.setattr(collector, 'acquire_entry', lambda row: {**row, 'fulltext': 'Full technical contents. ' * 20,
                                                             'content_hash': 'hash'})
    rows, _, failures = collector.discover(source, checkpoint, LOWER, UPPER)
    assert not failures and len(rows) == 1
    assert rows[0]['published_at'] is None and rows[0]['date_basis'] == 'first_observed_undated'
    assert rows[0]['event_at'] == UPPER


def test_snapshot_initial_baseline_then_revision(monkeypatch):
    source = {**SOURCE, 'kind': 'snapshot'}
    raw = '<main>' + 'Original contents. ' * 20 + '</main>'
    monkeypatch.setattr(collector, 'fetch', lambda url: (raw, url, 'html'))
    rows, checkpoint, _ = collector.discover(source, {}, LOWER, UPPER)
    assert not rows
    raw += '<main>New additional content.</main>'
    # Change within the same semantic content body, not the surrounding chrome.
    raw = raw.replace('Original contents.', 'Updated contents.')
    rows, _, _ = collector.discover(source, checkpoint, LOWER, UPPER)
    assert rows and rows[0]['date_basis'] == 'observed_revision'


def candidate_and_decision(tmp_path):
    body = 'The complete original content with details of the update.'
    (tmp_path / 'original.txt').write_text(body)
    candidate = {'id': 'one', 'framework': 'Test', 'source': 'Test · 官方博客', 'source_id': 'test',
                 'url': 'https://example.com/new', 'title': 'New feature', 'fulltext_path': 'original.txt',
                 'content_hash': digest(body), 'event_at': LOWER}
    decision = {'id': 'one', 'decision': 'include', 'overview': '新增持久执行，任务中断后可恢复。',
                'what_changed': '新增持久执行。', 'usefulness': '恢复中断任务。', 'summary': '**技术变化**：新增持久执行。\n\n- **用途**：恢复中断任务。',
                'reading_evidence': {'reader': 'agent', 'read_at': UPPER, 'sha256': digest(body), 'char_count': len(body), 'read_chars': len(body)}}
    return candidate, decision


def test_reject_incomplete_or_wrong_original_reading(tmp_path):
    candidate, decision = candidate_and_decision(tmp_path)
    decision['reading_evidence']['read_chars'] = 1
    with pytest.raises(ValueError, match='full-original'):
        pipeline.validate_decisions([candidate], [decision], tmp_path)


def test_reject_missing_and_duplicate_decisions(tmp_path):
    candidate, decision = candidate_and_decision(tmp_path)
    for decisions in ([], [decision, decision]):
        with pytest.raises(ValueError, match='every candidate'):
            pipeline.validate_decisions([candidate], decisions, tmp_path)


def test_native_body_fold_source_only_link_and_no_markdown_leak(tmp_path):
    candidate, decision = candidate_and_decision(tmp_path)
    included, _, _ = pipeline.validate_decisions([candidate], [decision], tmp_path)
    block = report_blocks(included[0])[0]
    assert block['block_type'] == 2 and block['text']['style']['folded'] is True
    runs = block['text']['elements']
    assert runs[1]['text_run']['text_element_style']['link']['url'] == candidate['url']
    assert runs[2]['text_run']['content'] == '  New feature\n' + decision['overview']
    assert len([r for r in runs if r['text_run']['text_element_style'].get('link')]) == 1
    serialized = json.dumps(block['children'], ensure_ascii=False)
    assert '**' not in serialized and '总结（展开' not in serialized


def test_failed_source_preserves_checkpoint_and_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'DATA', tmp_path)
    monkeypatch.setattr(pipeline, 'manifest', lambda: {'frameworks': [{'name': 'Test', 'sources': [SOURCE]}]})
    write_json(tmp_path / 'state.json', {'sources': {'sample': {'seen': {'old': {'url': 'x'}}}}})
    candidate, _ = candidate_and_decision(tmp_path)
    write_json(tmp_path / 'pending.json', [candidate])
    monkeypatch.setattr(pipeline, 'discover', lambda *args: (_ for _ in ()).throw(RuntimeError('network down')))
    result = pipeline.collect('2026-10-03')
    report = pipeline.load(result['run_path'], {})
    state = pipeline.load(tmp_path / 'state.json', {})
    assert state['sources']['sample']['seen']['old']['url'] == 'x'
    assert report['candidates'][0]['id'] == 'one'
    assert report['errors'] and report['source_status'][0]['status'] == 'failed'
    assert pipeline.collect('2026-10-03')['run_id'] == result['run_id']


def test_email_shows_framework_change_and_use_without_news_deployment():
    report = {'run_id': 'test', 'report_type': 'agent_frameworks', 'task': 'Agent Framework', 'status': 'partial',
              'started_at': LOWER, 'finished_at': UPPER, 'summary': '1项更新', 'errors': ['One source failed'],
              'framework_updates': {'Test': [{'title': 'Memory', 'what_changed': '新增记忆', 'usefulness': '保存跨轮上下文', 'url': 'https://example.com/new'}]},
              'source_status': [{'framework': 'Test', 'name': 'Blog', 'status': 'failed', 'candidate_count': 1}]}
    _, plain, rich = task_email.render(report, {})
    assert '技术变化：新增记忆' in plain and '用途：保存跨轮上下文' in plain
    assert 'One source failed' in plain and 'GitHub 同步' not in plain and 'Render 部署' not in plain


def test_zero_update_run_still_sends_email_and_mail_retry_skips_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, 'DATA', tmp_path)
    report = {'run_id': 'test', 'report_type': 'agent_frameworks', 'day': '2026-10-03', 'task': 'Agent Framework',
              'started_at': LOWER, 'collected_at': LOWER, 'candidates': [], 'errors': [],
              'source_status': [{'framework': 'Test', 'name': 'Blog', 'status': 'success', 'candidate_count': 0}]}
    path = tmp_path / 'run.json'
    write_json(path, report)
    calls = []
    monkeypatch.setattr(pipeline, 'sync', lambda *args: calls.append('sync') or {'daily_url': 'https://example.com/daily'})
    monkeypatch.setattr(task_email, 'send_report', lambda *args: calls.append('email') or {'ok': True})
    assert pipeline.finish(path)['status'] == 'success'
    assert calls == ['sync', 'email']
    pipeline.finish(path)
    assert calls == ['sync', 'email', 'email']  # SMTP module de-duplicates by run_id.
