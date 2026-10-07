"""Record this collection run's archive changes for the final email."""
import argparse
import json
import time
import uuid
from pathlib import Path

from .common import ROOT, VENDORS, digest, now, write_json
from .store import ARCHIVE, is_fully_read, load


def snapshot(archive):
    return {item['id']: {
        'vendor': item['vendor'], 'title': item['title'],
        'source': item['source'], 'original_url': item.get('report_url') or item['url'],
        'feishu_url': item.get('feishu_url', ''),
        'published_at': item['published_at'], 'fully_read': is_fully_read(item),
        'content_hash': item.get('content_hash') or (item.get('reading_evidence') or {}).get('sha256'),
        'version_published_at': item.get('version_published_at'),
        'summary_hash': digest(json.dumps([item.get('overview'), item.get('summary')], ensure_ascii=False)),
    } for item in archive['items']}


def changes(before, after):
    updates = {vendor: [] for vendor in VENDORS}
    for uid, item in sorted(after.items(), key=lambda pair: (pair[1]['published_at'], pair[0]), reverse=True):
        old = before.get(uid)
        if old is None:
            kind = 'new'
        elif not old['fully_read'] and item['fully_read']:
            kind = 'reading_completed'
        elif old['content_hash'] != item['content_hash'] or old['version_published_at'] != item['version_published_at']:
            kind = 'revised'
        elif old['summary_hash'] != item['summary_hash']:
            kind = 'summary_updated'
        else:
            continue
        updates[item['vendor']].append({
            'id': uid, 'kind': kind, 'title': item['title'], 'source': item['source'],
            'original_url': item['original_url'], 'feishu_url': item['feishu_url'],
            'published_at': item['published_at'],
            'read_status': 'full_text_read' if item['fully_read'] else 'pending_full_text',
        })
    return updates


def begin(archive_path=ARCHIVE, root=ROOT):
    run_id = str(uuid.uuid4())
    report_path = Path(root) / '.task_reports' / (run_id + '.json')
    baseline_path = report_path.with_name(run_id + '.technical-report-baseline.json')
    report = {
        'run_id': run_id, 'task': '技术报告每日采集与飞书归档',
        'status': 'running', 'started_at': now(), 'finished_at': '',
        'summary': '任务进行中；完成或失败后须填写本轮实际结果。',
        'counts': {}, 'errors': [],
        'technical_report_baseline': str(baseline_path.relative_to(root)),
    }
    # Save a report even when the initial archive cannot be read.
    write_json(report_path, report)
    try:
        write_json(baseline_path, {'run_id': run_id, 'items': snapshot(load(archive_path))})
    except Exception as exc:
        report.update(status='failed', summary='开始归档快照失败；停止业务操作，仍须发送最终邮件。')
        report['errors'].append('开始快照失败：' + type(exc).__name__)
        write_json(report_path, report)
    return report_path


def finish(report_path, archive_path=ARCHIVE, root=ROOT):
    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('status') not in ('success', 'partial', 'failed', 'skipped'):
        raise ValueError('Set this run\'s actual final status and summary before finishing')
    baseline_path = (Path(root) / report['technical_report_baseline']).resolve()
    if not baseline_path.is_relative_to((Path(root) / '.task_reports').resolve()):
        raise ValueError('Baseline must be inside the local task report directory')
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    if baseline.get('run_id') != report['run_id']:
        raise ValueError('Baseline belongs to a different run')
    report['technical_report_updates'] = changes(baseline['items'], snapshot(load(archive_path)))
    entries = [item for items in report['technical_report_updates'].values() for item in items]
    report.setdefault('counts', {}).update({
        'report_added_count': sum(item['kind'] == 'new' for item in entries),
        'report_revised_count': sum(item['kind'] == 'revised' for item in entries),
        'report_reading_completed_count': sum(item['kind'] == 'reading_completed' for item in entries),
        'report_summary_updated_count': sum(item['kind'] == 'summary_updated' for item in entries),
        'report_changed_vendor_count': sum(bool(items) for items in report['technical_report_updates'].values()),
    })
    report['finished_at'] = now()
    write_json(report_path, report)
    return report


def finalize(report_path, archive_path=ARCHIVE, root=ROOT, *, sender=None, values=None,
             attempts=3, sleep_fn=time.sleep):
    """Freeze this run's result, then retry only its final email until accepted."""
    from task_email import config, send_report

    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if not report.get('email_finalized_at'):
        if report.get('status') not in ('success', 'partial', 'failed', 'skipped'):
            report.update(status='failed', summary='任务未填写最终业务结果；业务完成情况未核验。')
            report.setdefault('errors', []).append('任务提前结束，最终业务状态未填写。')
        write_json(report_path, report)
        try:
            # Previously finished reports retain their original time and archive delta.
            if not (report.get('finished_at') and 'technical_report_updates' in report):
                report = finish(report_path, archive_path, root)
        except Exception as exc:
            # Failed verification must not prevent a failure email or claim zero changes.
            report.pop('technical_report_updates', None)
            for key in ('report_added_count', 'report_revised_count',
                        'report_reading_completed_count', 'report_summary_updated_count',
                        'report_changed_vendor_count'):
                report.setdefault('counts', {}).pop(key, None)
            if report['status'] in ('success', 'skipped'):
                report['status'] = 'partial'
            report['technical_report_verification'] = 'failed'
            report.setdefault('errors', []).append('厂商变更未核验：' + type(exc).__name__)
            report['finished_at'] = now()
        report['email_finalized_at'] = now()
        write_json(report_path, report)

    sender = sender or send_report
    values = config() if values is None else values
    previous_attempts = report.get('email', {}).get('attempt_count', 0)
    for attempt in range(1, attempts + 1):
        try:
            result = sender(report, values)
            if not result.get('ok') or result.get('email') not in ('accepted_by_smtp', 'already_sent'):
                raise RuntimeError('Email was not accepted')
        except Exception as exc:
            report['email'] = {'status': 'failed', 'error_type': type(exc).__name__,
                               'attempt_count': previous_attempts + attempt,
                               'last_attempt_at': now()}
            write_json(report_path, report)
            if attempt < attempts:
                sleep_fn(5)
                continue
            return {'ok': False, 'email': 'failed', 'run_id': report['run_id'],
                    'error_type': type(exc).__name__, 'attempt_count': previous_attempts + attempt}
        report['email'] = {'status': result['email'],
                           'attempt_count': previous_attempts + attempt, 'confirmed_at': now()}
        write_json(report_path, report)
        return {**result, 'attempt_count': previous_attempts + attempt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('begin')
    end = sub.add_parser('finish')
    end.add_argument('--report', type=Path, required=True)
    final = sub.add_parser('finalize', help='Finalize the report and send its email, with retries.')
    final.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'begin':
        path = begin()
        result = {'report': str(path.relative_to(ROOT)), 'run_id': path.stem,
                  'status': json.loads(path.read_text(encoding='utf-8'))['status']}
    elif args.command == 'finish':
        report = finish(args.report)
        result = {'report': str(args.report), 'run_id': report['run_id'], 'counts': report['counts']}
    else:
        result = finalize(args.report)
    print(json.dumps(result, ensure_ascii=False))
    if result.get('ok') is False:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
