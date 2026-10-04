"""Versioned JSON archive with mandatory full-original reading evidence."""
import fcntl
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .common import ROOT, VENDORS, digest, now, write_json

ARCHIVE = ROOT / 'technical_reports' / 'archive.json'


def canonical_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith('utm_') and k not in ('view_from', 'tab')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), urlencode(query), ''))


def report_id(item):
    paper = item.get('paper_id') or ''
    if not paper:
        match = re.search(r'arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})', item.get('report_url', '') + ' ' + item['url'])
        paper = match.group(1) if match else ''
    key = re.sub(r'v\d+$', '', paper) or canonical_url(item.get('canonical_url') or item['url'])
    return 'R-' + digest(item['vendor'] + ':' + key)[:20]


def load(path=ARCHIVE):
    if not Path(path).exists():
        return {'version': 1, 'updated_at': None, 'items': [], 'runs': []}
    return json.loads(Path(path).read_text(encoding='utf-8'))


def omitted_pdf_pages(item):
    missing = []
    for document in item.get('original_documents', []):
        evidence = document.get('extraction_evidence') or {}
        covered = {p.get('page') for p in evidence.get('certified_blank_pages', [])}
        covered.update(p.get('page') for p in evidence.get('page_extraction', [])
                       if p.get('extractor') == 'agent_visual_recovery' and p.get('visual_review'))
        missing.extend(p for p in document.get('empty_pages', []) if p not in covered)
    return missing


def is_fully_read(item):
    return item.get('read_status') == 'full_text_read' and not omitted_pdf_pages(item) and not item.get('original_quality_error')


def _older_original(item, old):
    acquired = old.get('original_acquired_at')
    if not acquired:
        return False
    incoming = item.get('original_acquired_at')
    if not incoming:
        return True
    incoming_at, acquired_at = datetime.fromisoformat(incoming), datetime.fromisoformat(acquired)
    if incoming_at == acquired_at:
        # Legacy acquisition timestamps have second precision. If snapshots
        # conflict within that second, keep the current original rather than
        # letting a queued reader erase a recovered chart or attachment.
        return bool(item.get('content_hash') and old.get('content_hash')
                    and item['content_hash'] != old['content_hash'])
    return incoming_at < acquired_at


def validate(item, root=ROOT):
    item = dict(item)
    for key in ('vendor', 'title', 'source', 'published_at', 'url'):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError('missing report field: ' + key)
    if item['vendor'] not in VENDORS:
        raise ValueError('unknown vendor: ' + item['vendor'])
    date.fromisoformat(item['published_at'][:10])
    if item.get('original_acquired_at'):
        timestamp = datetime.fromisoformat(item['original_acquired_at'])
        if timestamp.tzinfo is None:
            raise ValueError('original acquisition timestamp requires a timezone')
    if urlsplit(item['url']).scheme not in ('https', 'http'):
        raise ValueError('report URL must be HTTP(S)')
    item['id'] = item.get('id') or report_id(item)
    if item.get('read_status') == 'full_text_read':
        if omitted_pdf_pages(item):
            raise ValueError('PDF contains uncertified omitted pages requiring visual recovery')
        if not item.get('overview') or not item.get('summary'):
            raise ValueError('a fully read report requires overview and pyramid summary')
        path = (Path(root) / item.get('fulltext_path', '')).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
            raise ValueError('missing local report original')
        original = path.read_text(encoding='utf-8')
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]', original):
            raise ValueError('original contains unmapped/control glyphs requiring recovery')
        item.pop('original_quality_error', None)
        evidence = item.get('reading_evidence') or {}
        if not original.strip() or evidence.get('sha256') != digest(original):
            raise ValueError('original hash does not match reading evidence')
        if evidence.get('read_chars', 0) < len(original) or evidence.get('char_count') != len(original):
            raise ValueError('incomplete full-original reading evidence')
        if evidence.get('reader') != 'agent' or not evidence.get('read_at'):
            raise ValueError('Agent reading attestation is required')
        item['content_hash'] = digest(original)
        receipts = evidence.get('chunks')
        if receipts:
            cursor = 0
            for receipt in receipts:
                if receipt.get('start') != cursor or not cursor < receipt.get('end', 0) <= len(original):
                    raise ValueError('reading chunks leave a gap or overlap')
                if receipt.get('sha256') != digest(original[cursor:receipt['end']]):
                    raise ValueError('reading chunk hash does not match the original')
                cursor = receipt['end']
            if cursor != len(original):
                raise ValueError('reading chunks do not cover the entire original')
        # Preserve immutable original versions even when a source adapter used a
        # URL-based filename that it may overwrite on the next collection.
        immutable = Path(root) / 'technical_reports' / 'fulltext' / (item['id'] + '-' + item['content_hash'][:16] + '.txt')
        immutable.parent.mkdir(parents=True, exist_ok=True)
        if not immutable.exists():
            immutable.write_text(original, encoding='utf-8')
        item['fulltext_path'] = str(immutable.relative_to(Path(root)))
    else:
        item['read_status'] = 'pending_full_text'
        item.pop('summary', None)
        item.pop('overview', None)
        item.pop('chat_overview', None)
    return item


def merge(items, path=ARCHIVE, root=ROOT, coverage=None):
    archive = load(path)
    existing = {item['id']: item for item in archive['items']}
    added = updated = stale_ignored = 0
    for raw in items:
        item = validate(raw, root)
        # Keep an audited exclusion out of the catalogue even if an older
        # discovery/reading process finishes after that exclusion was applied.
        if item['id'] in archive.get('excluded_reports', {}):
            continue
        primary = archive.get('aliases', {}).get(item['id'])
        if primary:
            retired = archive.setdefault('merged_reports', {})
            previous = retired.get(item['id'], {})
            if _older_original(item, previous):
                previous['last_stale_result_at'] = now()
                stale_ignored += 1
                continue
            if item['read_status'] == 'full_text_read' or previous.get('read_status') != 'full_text_read':
                retired[item['id']] = {**item, 'merged_into': primary}
            continue
        old = existing.get(item['id'])
        if old:
            # A queued reading job can finish after a later acquisition has
            # restored a missing chart/attachment or fetched a revised source.
            # Its older snapshot must not replace that newer original evidence.
            if _older_original(item, old):
                old['last_stale_result_at'] = now()
                stale_ignored += 1
                continue
            # A temporary fetch failure must not erase a previously verified version.
            if old.get('read_status') == 'full_text_read' and item['read_status'] != 'full_text_read':
                incoming_path = (Path(root) / item.get('fulltext_path', '')).resolve()
                same_original = (not item.get('error') and not item.get('original_unavailable')
                                 and incoming_path.is_relative_to(Path(root).resolve()) and incoming_path.is_file()
                                 and digest(incoming_path.read_text(encoding='utf-8')) == old.get('content_hash'))
                if not same_original:
                    old['last_checked_at'] = now()
                    old['last_fetch_error'] = item.get('error', 'full original unavailable')
                    continue
                preserved = {key: old[key] for key in ('overview', 'summary', 'chat_overview', 'reading_evidence', 'fulltext_path', 'content_hash') if key in old}
                item = {**old, **item, **preserved, 'read_status': 'full_text_read', 'error': None}
            item['first_seen_at'] = old.get('first_seen_at', now())
            versions = list(old.get('versions', []))
            if old.get('content_hash') and old.get('content_hash') != item.get('content_hash'):
                versions.append({k: old.get(k) for k in ('content_hash', 'overview', 'summary', 'chat_overview', 'reading_evidence', 'fulltext_path', 'updated_at',
                                'published_at', 'first_published_at', 'version_published_at', 'original_documents', 'extraction_evidence',
                                'original_acquired_at')})
            item['versions'] = versions
            for key in ('feishu_url', 'feishu_node_token', 'feishu_document_id'):
                if old.get(key):
                    item[key] = old[key]
            updated += old.get('content_hash') != item.get('content_hash') or old.get('summary') != item.get('summary')
        else:
            item['first_seen_at'] = now()
            added += 1
        item['updated_at'] = now()
        existing[item['id']] = item
    archive['items'] = sorted(existing.values(), key=lambda i: (i['published_at'], i['id']), reverse=True)
    archive['updated_at'] = now()
    archive['runs'].append({'at': now(), 'input_count': len(items), 'added': added, 'updated': updated,
                            'stale_ignored': stale_ignored, 'coverage': coverage or {}})
    write_json(path, archive)
    return {'total': len(existing), 'added': added, 'updated': updated,
            'stale_ignored': stale_ignored,
            'fully_read': sum(is_fully_read(i) for i in existing.values()),
            'pending': sum(not is_fully_read(i) for i in existing.values())}


def collapse_aliases(mapping, path=ARCHIVE):
    """Retain related editions and reading evidence without duplicate catalogue rows."""
    archive = load(path)
    existing = {item['id']: item for item in archive['items']}
    for duplicate, primary in mapping.items():
        if duplicate == primary or primary not in existing:
            raise ValueError('a duplicate must refer to an existing distinct primary report')
        archive.setdefault('aliases', {})[duplicate] = primary
        if duplicate in existing:
            item = existing.pop(duplicate)
            archive.setdefault('merged_reports', {})[duplicate] = {**item, 'merged_into': primary}
            related = existing[primary].setdefault('related_urls', [])
            for url in (item.get('url'), item.get('report_url')):
                if url and url not in related:
                    related.append(url)
    archive['items'] = sorted(existing.values(), key=lambda i: (i['published_at'], i['id']), reverse=True)
    archive['updated_at'] = now()
    write_json(path, archive)
    return {'merged': len(mapping), 'total': len(existing)}


def latest(limit=15, path=ARCHIVE):
    return [i for i in load(path)['items'] if is_fully_read(i)][:limit]


def export_search(path=ARCHIVE, output=ROOT / 'technical_report_daily'):
    # Readers can finish in separate processes. Serialize before loading the
    # archive so an older export cannot overwrite newer summaries or delete a
    # day added by the other reader. Keep this independent of pipeline.lock:
    # ingest already holds that lock when it invokes this function.
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.export.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _export_search(path, output)


def _export_search(path, output):
    days = {}
    for item in load(path)['items']:
        if not is_fully_read(item):
            continue
        day = item['published_at'][:10]
        days.setdefault(day, []).append({'archive_id': item['id'], '资讯标题': item['title'],
            '发布时间': day + ' 00:00:00', 'date_precision': item.get('date_precision', 'day'),
            '来源': item['vendor'] + ' 技术报告', '超链接': item['url'],
            'desc': item['overview'], 'content': item['summary'], 'content_kind': '技术报告总结',
            'report_id': item['id'], 'fulltext_path': item['fulltext_path'],
            'source': item['source'], 'report_url': item.get('report_url', ''), 'feishu_url': item.get('feishu_url', '')})
    for day, items in days.items():
        write_json(Path(output) / (day + '.json'), {'version': 1, 'date': day, 'generated_by': 'technical_reports.export_search', 'items': items})
    for old in Path(output).glob('*.json'):
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', old.stem) and old.stem not in days:
            data = json.loads(old.read_text(encoding='utf-8'))
            if data.get('generated_by') == 'technical_reports.export_search' or all(i.get('content_kind') == '技术报告总结' and i.get('report_id') for i in data.get('items', [])):
                old.unlink()
    return len(days)
