import json
from copy import deepcopy

import pytest

from technical_reports.task_report import begin, changes, finish, snapshot
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
