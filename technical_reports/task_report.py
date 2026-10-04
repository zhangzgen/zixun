"""Record this collection run's archive changes for the final email."""
import argparse
import json
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
    write_json(baseline_path, {'run_id': run_id, 'items': snapshot(load(archive_path))})
    write_json(report_path, {
        'run_id': run_id, 'task': '技术报告每日采集与飞书归档',
        'status': 'running', 'started_at': now(), 'finished_at': '',
        'summary': '任务进行中；完成或失败后须填写本轮实际结果。',
        'counts': {}, 'errors': [],
        'technical_report_baseline': str(baseline_path.relative_to(root)),
    })
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('begin')
    end = sub.add_parser('finish')
    end.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'begin':
        path = begin()
        result = {'report': str(path.relative_to(ROOT)), 'run_id': path.stem}
    else:
        report = finish(args.report)
        result = {'report': str(args.report), 'run_id': report['run_id'], 'counts': report['counts']}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
