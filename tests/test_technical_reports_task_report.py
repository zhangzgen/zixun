import json
from copy import deepcopy

import pytest

from technical_reports.task_report import begin, changes, finalize, finish, snapshot
from task_email import render


def item(uid='existing', vendor='Anthropic', **extra):
    return {'id': uid, 'vendor': vendor, 'title': '报告 ' + uid, 'source': vendor + ' Research',
            'url': 'https://example.com/' + uid, 'published_at': '2026-10-01',
            'content_hash': 'original-a', 'read_status': 'full_text_read',
            'overview': '概述', 'summary': '完整总结', **extra}


def test_mail_diff_distinguishes_new_revised_completed_and_summary_only_changes():
    before_items = [item(), item('pending', 'Kimi', read_status='pending_full_text'),
                    item('summary', 'Google')]
    after_items = [item(content_hash='original-b'), item('pending', 'Kimi'),
                   item('summary', 'Google', summary='更新总结'),
                   item('new', 'Seed'), item('new-pending', 'GLM', read_status='pending_full_text')]
    updates = changes(snapshot({'items': before_items}), snapshot({'items': after_items}))
    assert [(r['id'], r['kind']) for r in updates['Anthropic']] == [('existing', 'revised')]
    assert updates['Kimi'][0]['kind'] == 'reading_completed'
    assert updates['Google'][0]['kind'] == 'summary_updated'
    assert updates['Seed'][0]['kind'] == 'new'
    assert updates['GLM'][0]['kind'] == 'new' and updates['GLM'][0]['read_status'] == 'pending_full_text'
    assert updates['OpenAI'] == updates['DeepSeek'] == []


def test_check_times_links_and_card_format_changes_are_not_report_revisions():
    existing = item()
    before = snapshot({'items': [existing]})
    after = snapshot({'items': [{**existing, 'last_checked_at': 'later',
                                'feishu_url': 'https://example.com/wiki', 'chat_overview': '新版群聊概述'}]})
    assert not any(changes(before, after).values())


def test_begin_finish_uses_this_run_baseline_and_preserves_actual_execution_results(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': [item()]}))
    path = begin(archive, tmp_path)
    report = json.loads(path.read_text())
    report.update(status='partial', summary='发现新报告，飞书同步失败。', feishu='失败',
                  github='未执行', render='未核验', source_status='Seed 已核查', errors=['权限错误'])
    path.write_text(json.dumps(report))
    archive.write_text(json.dumps({'items': [item(), item('new', 'Seed')]}))
    result = finish(path, archive, tmp_path)
    assert result['run_id'] == report['run_id']
    assert result['status'] == 'partial' and result['errors'] == ['权限错误']
    assert result['counts']['report_added_count'] == result['counts']['report_changed_vendor_count'] == 1
    title, plain, rich = render(result, {})
    assert '部分完成' in title and 'Seed 技术报告' in plain + rich
    assert '新增 · 报告 new（已全文阅读）' in plain
    assert 'https://example.com/new' in rich and '未核验' in plain


def test_missing_or_foreign_baseline_cannot_claim_no_updates(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)
    report = json.loads(path.read_text())
    with pytest.raises(ValueError, match='actual final status'):
        finish(path, archive, tmp_path)
    report.update(status='failed', summary='采集失败')
    path.write_text(json.dumps(report))
    baseline_path = tmp_path / report['technical_report_baseline']
    baseline = json.loads(baseline_path.read_text())
    baseline['run_id'] = 'different-run'
    baseline_path.write_text(json.dumps(baseline))
    with pytest.raises(ValueError, match='different run'):
        finish(path, archive, tmp_path)
    baseline_path.unlink()
    with pytest.raises(FileNotFoundError):
        finish(path, archive, tmp_path)


def test_email_no_updates_and_vendor_titles_are_escaped_and_redacted():
    report = {'run_id': 'preview', 'task': '技术报告每日采集与飞书归档', 'status': 'success',
              'started_at': '2026-10-03T00:00:00+08:00', 'finished_at': '2026-10-03T00:02:00+08:00',
              'summary': '本轮完成', 'technical_report_updates': {'Anthropic': []}}
    assert '本轮归档未记录新增' in render(report, {})[1]
    updated = deepcopy(report)
    updated['technical_report_updates']['Anthropic'] = [{
        'title': '<script>test</script> secret-example', 'kind': 'new',
        'read_status': 'pending_full_text', 'original_url': 'https://example.com/report'}]
    _, plain, rich = render(updated, {'TEST_TOKEN': 'secret-example'})
    assert '待完整阅读' in plain and '&lt;script&gt;' in rich
    assert '<script>' not in rich and 'secret-example' not in plain + rich


def test_finalize_retries_email_without_recalculating_or_repeating_business(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)
    report = json.loads(path.read_text())
    report.update(status='success', summary='采集完成，无新增',
                  source_status=[{'vendor': 'Kimi', 'status': 'success'}])
    path.write_text(json.dumps(report))
    calls, waits = [], []

    def sender(data, values):
        render(data, values)
        calls.append(deepcopy(data))
        if len(calls) < 3:
            # If verification runs again after an email error, this change would leak in.
            archive.write_text(json.dumps({'items': [item('later', 'Seed')]}))
            raise TimeoutError('secret-server-details')
        return {'ok': True, 'email': 'accepted_by_smtp', 'run_id': data['run_id']}

    result = finalize(path, archive, tmp_path, sender=sender, values={}, sleep_fn=waits.append)
    assert result['email'] == 'accepted_by_smtp' and waits == [5, 5]
    assert {c['run_id'] for c in calls} == {report['run_id']}
    assert len({c['finished_at'] for c in calls}) == 1
    assert all(c['counts']['report_added_count'] == 0 for c in calls)
    stored = json.loads(path.read_text())
    assert stored['email']['attempt_count'] == 3 and 'secret-server-details' not in path.read_text()

    finalize(path, archive, tmp_path, sender=sender, values={}, sleep_fn=waits.append)
    assert calls[-1]['counts']['report_added_count'] == 0
    assert calls[-1]['finished_at'] == calls[0]['finished_at']


@pytest.mark.parametrize('status', ['success', 'partial', 'failed', 'skipped'])
def test_finalize_missing_baseline_still_sends_honest_final_email(tmp_path, status):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)
    report = json.loads(path.read_text())
    (tmp_path / report['technical_report_baseline']).unlink()
    report.update(status=status, summary='本轮业务结果',
                  counts={'report_added_count': 10}, technical_report_updates={'Seed': []})
    path.write_text(json.dumps(report))
    messages = []

    def sender(data, values):
        messages.append(render(data, values)[1])
        return {'ok': True, 'email': 'accepted_by_smtp', 'run_id': data['run_id']}

    assert finalize(path, archive, tmp_path, sender=sender, values={})['ok']
    stored = json.loads(path.read_text())
    assert stored['technical_report_verification'] == 'failed'
    assert 'report_added_count' not in stored['counts']
    assert '厂商变更未核验' in messages[0] and '本轮归档未记录新增' not in messages[0]


def test_begin_archive_failure_and_unfinished_run_can_send_failure_summary(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text('invalid json')
    path = begin(archive, tmp_path)
    assert json.loads(path.read_text())['status'] == 'failed'
    messages = []

    def sender(data, values):
        messages.append(render(data, values)[1])
        return {'ok': True, 'email': 'accepted_by_smtp', 'run_id': data['run_id']}

    assert finalize(path, archive, tmp_path, sender=sender, values={})['ok']
    assert '开始快照失败' in messages[0] and '厂商变更未核验' in messages[0]
    archive.write_text(json.dumps({'items': []}))
    unfinished = begin(archive, tmp_path)
    assert finalize(unfinished, archive, tmp_path, sender=sender, values={})['ok']
    assert '最终业务状态未填写' in messages[-1]


def test_finalize_preserves_failed_email_for_later_retry_with_same_run(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)

    def rejected(data, values):
        raise ConnectionError('private credentials')

    result = finalize(path, archive, tmp_path, sender=rejected, values={}, sleep_fn=lambda _: None)
    assert not result['ok'] and result['attempt_count'] == 3
    stored = json.loads(path.read_text())
    assert stored['email']['status'] == 'failed' and 'private credentials' not in path.read_text()

    def accepted(data, values):
        return {'ok': True, 'email': 'accepted_by_smtp', 'run_id': data['run_id']}

    result = finalize(path, archive, tmp_path, sender=accepted, values={})
    assert result['run_id'] == stored['run_id'] and result['attempt_count'] == 4


def test_finalize_deduplicates_using_the_real_send_receipt(tmp_path):
    from task_email import send_report
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)
    sent = []

    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message):
            sent.append(message)
            return {}
        def close(self): pass

    def sender(data, values):
        return send_report(data, values, tmp_path / 'sent', SMTP)

    values = {'SMTP_USER': 'test@example.com', 'SMTP_PASSWORD': 'private'}
    assert finalize(path, archive, tmp_path, sender=sender, values=values)['email'] == 'accepted_by_smtp'
    assert finalize(path, archive, tmp_path, sender=sender, values=values)['email'] == 'already_sent'
    assert len(sent) == 1


def test_finalize_keeps_previously_finished_report_time_and_delta(tmp_path):
    archive = tmp_path / 'archive.json'
    archive.write_text(json.dumps({'items': []}))
    path = begin(archive, tmp_path)
    data = json.loads(path.read_text())
    data.update(status='partial', summary='本轮采集部分完成')
    path.write_text(json.dumps(data))
    finished = finish(path, archive, tmp_path)
    archive.write_text(json.dumps({'items': [item('next-run', 'Seed')]}))

    def sender(report, values):
        assert report['finished_at'] == finished['finished_at']
        assert report['counts']['report_added_count'] == 0
        return {'ok': True, 'email': 'accepted_by_smtp', 'run_id': report['run_id']}

    assert finalize(path, archive, tmp_path, sender=sender, values={})['ok']
