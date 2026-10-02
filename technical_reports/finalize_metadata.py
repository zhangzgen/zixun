"""Audit metadata without opening report originals; apply under pipeline.lock.

The default command writes a reviewable plan only. ``--apply`` obtains the
pipeline lock and rebuilds that plan from the latest archive and source metadata
before changing anything. Reading status, summaries, originals, evidence and
content hashes are never overwritten. No Feishu API is called.
"""
import argparse
import copy
import fcntl
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .common import ROOT, now, digest, write_json
from .store import ARCHIVE, canonical_url, export_search, load, report_id

PLAN = ROOT / '.technical_reports/final-metadata-corrections.json'
STATE = ROOT / '.technical_reports/feishu_state.json'
OFF_SWITCH_ALIAS = {'R-6e19c6c02cc2213b6657': 'R-f0d1a41520189784686b'}
SECURITY_REPORTS = {
    'R-d38cf33d32ec2b3b0057': 'The Three Layers of Agent Security: A Framework for Policymakers',
    'R-9e652d2facf3a276765c': 'GDM AI Control Roadmap',
}
METADATA_FIELDS = {
    'title', 'source', 'published_at', 'published_at_raw', 'date_precision', 'url',
    'report_url', 'paper_id', 'canonical_url', 'content_type', 'language',
    'first_published_at', 'version_published_at', 'model_released_at', 'revised_at',
    'document_header_date', 'official_index_date', 'date_basis', 'date_source_url',
    'date_source_page', 'date_source', 'date_note', 'date_evidence', 'publication_date_evidence',
    'pdf_creation_date', 'publisher', 'original_scope_note',
    'original_publication_metadata', 'official_download_source',
    'supplementary_urls', 'own_original_urls', 'additional_original_evidence',
    'related_publications', 'additional_originals',
    'not_auto_attached_reference_urls', 'primary_resolution_evidence', 'primary_url',
}
PROTECTED_FIELDS = {
    'id', 'vendor', 'read_status', 'content_hash', 'reading_evidence', 'fulltext_path',
    'overview', 'summary', 'reading_chunks', 'original_documents', 'extraction_evidence',
}


def _payload(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    return {'items': value} if isinstance(value, list) else value


def _inputs(root):
    bootstrap = root / 'technical_reports/bootstrap'
    order = ['west.json', 'west-extra.json', 'west-ready.json', 'west-extra-ready.json']
    paths = [bootstrap / name for name in order]
    paths += sorted((root / '.technical_reports').glob('google-ready*.json'))
    paths += sorted((root / '.technical_reports').glob('seed-ready*.json'))
    paths += [bootstrap / 'china.json', bootstrap / 'google_seed.json']
    paths += sorted((root / '.technical_reports').glob('browser-recovery*.json'))
    paths += [root / '.technical_reports/west-recovery.json']
    return list(dict.fromkeys(path for path in paths if path.exists()))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _resolve(uid, aliases):
    seen = set()
    while uid in aliases:
        if uid in seen:
            raise ValueError('report alias cycle: ' + uid)
        seen.add(uid)
        uid = aliases[uid]
    return uid


def _wiki(item, cached=None):
    cached = cached or {}
    return {'document': item.get('feishu_document_id') or cached.get('document'),
            'node_token': item.get('feishu_node_token'), 'url': item.get('feishu_url')}


def build_plan(archive=None, state=None, root=ROOT):
    root = Path(root)
    archive = archive if archive is not None else load(root / 'technical_reports/archive.json')
    state_path = root / '.technical_reports/feishu_state.json'
    state = state if state is not None else (_payload(state_path) if state_path.exists() else {})
    payloads, issues, source_files = [], [], []
    aliases = {**archive.get('aliases', {}), **OFF_SWITCH_ALIAS}
    exclusions = {}
    for path in _inputs(root):
        source = str(path.relative_to(root))
        try:
            payload = _payload(path)
        except (OSError, ValueError) as exc:
            issues.append({'kind': 'metadata_input_unreadable', 'source': source, 'error': str(exc)})
            continue
        payloads.append((source, payload))
        source_files.append(source)
        for entry in _walk(payload.get('coverage', {})):
            for row in entry.get('id_aliases', []):
                aliases[row['old_id']] = row['primary_id']
            for row in entry.get('excluded_reports', []):
                if isinstance(row, dict) and row.get('id'):
                    exclusions[row['id']] = {**row, 'source_file': source}
    for uid in list(aliases):
        aliases[uid] = _resolve(uid, aliases)
    protected = set(SECURITY_REPORTS)
    if any(old in protected or target in protected for old, target in aliases.items()):
        raise ValueError('the two independent Google security reports must never be aliased')
    rows = archive['items']
    ids = Counter(i['id'] for i in rows)
    for uid, count in ids.items():
        if count > 1:
            issues.append({'kind': 'duplicate_id', 'id': uid, 'count': count})
    candidates, provenance = defaultdict(dict), defaultdict(list)
    for source, payload in payloads:
        for item in payload.get('items', []):
            try:
                uid = _resolve(item.get('id') or report_id(item), aliases)
            except (KeyError, ValueError):
                continue
            for key in METADATA_FIELDS:
                value = item.get(key)
                if value not in (None, '', []) or key in ('supplementary_urls', 'own_original_urls') and key in item:
                    candidates[uid][key] = copy.deepcopy(value)
            provenance[uid].append(source)
    current = {i['id']: i for i in rows}
    by_url = defaultdict(list)
    for item in rows:
        by_url[canonical_url(item['url'])].append(item['id'])
    for source, payload in payloads:
        for correction in payload.get('metadata_updates', []) + payload.get('metadata_corrections', []):
            for uid in by_url.get(canonical_url(correction['url']), []):
                uid = _resolve(uid, aliases)
                for key in METADATA_FIELDS:
                    if key in correction and key != 'url':
                        candidates[uid][key] = copy.deepcopy(correction[key])
                removal = set(correction.get('remove_supplementary_urls', []))
                if removal:
                    previous = candidates[uid].get('supplementary_urls', current.get(uid, {}).get('supplementary_urls', []))
                    candidates[uid]['supplementary_urls'] = [url for url in previous or [] if url not in removal]
                provenance[uid].append(source)
    # Explicitly verified independent papers retain their formal titles.
    for uid, title in SECURITY_REPORTS.items():
        if uid in current:
            candidates[uid]['title'] = title
    updates, same_original = [], defaultdict(list)
    for item in rows:
        uid = item['id']
        if uid in aliases or uid in exclusions:
            continue
        fields = {k: v for k, v in candidates.get(uid, {}).items() if item.get(k) != v}
        effective = {**item, **fields}
        primary = effective.get('report_url') or effective.get('original_url') or effective.get('url')
        if effective.get('primary_url') != primary:
            fields['primary_url'] = primary
        for key in ('vendor', 'title', 'source', 'published_at', 'url'):
            if not effective.get(key):
                issues.append({'kind': 'missing_required_metadata', 'id': uid, 'field': key})
        try:
            date.fromisoformat(effective['published_at'][:10])
        except (KeyError, TypeError, ValueError):
            issues.append({'kind': 'invalid_publication_date', 'id': uid})
        if not primary or not re.match(r'^https?://', primary):
            issues.append({'kind': 'invalid_primary_url', 'id': uid})
        try:
            same_original[(item['vendor'], report_id(effective))].append(uid)
        except (KeyError, ValueError):
            pass
        guard = None
        if item.get('read_status') == 'full_text_read':
            evidence = item.get('reading_evidence') or {}
            if not isinstance(item.get('overview'), str) or not item['overview'].strip() or not isinstance(item.get('summary'), str) or not item['summary'].strip():
                issues.append({'kind': 'fully_read_without_summary', 'id': uid})
            if evidence.get('sha256') != item.get('content_hash') or not item.get('content_hash'):
                issues.append({'kind': 'reading_hash_metadata_mismatch', 'id': uid})
            elif item.get('char_count') != evidence.get('char_count'):
                if isinstance(evidence.get('char_count'), int) and evidence['char_count'] > 0:
                    fields['char_count'] = evidence['char_count']
                    guard = evidence['sha256']
            count = evidence.get('char_count', 0)
            if evidence.get('reader') != 'agent' or not evidence.get('read_at') or evidence.get('read_chars', 0) < count:
                issues.append({'kind': 'incomplete_reading_attestation', 'id': uid})
            cursor = 0
            receipts = evidence.get('chunks') or []
            for receipt in receipts:
                if receipt.get('start') != cursor or not isinstance(receipt.get('end'), int) or receipt['end'] <= cursor:
                    issues.append({'kind': 'reading_chunk_gap_or_overlap', 'id': uid})
                    break
                cursor = receipt['end']
            if receipts and cursor != count:
                issues.append({'kind': 'reading_chunks_incomplete', 'id': uid})
            original_path = (root / item.get('fulltext_path', '')).resolve()
            if not original_path.is_relative_to(root.resolve()) or not original_path.is_file():
                issues.append({'kind': 'missing_original_file', 'id': uid})
        if fields:
            updates.append({'id': uid, 'vendor': item['vendor'], 'fields': fields,
                            'char_count_evidence_sha': guard, 'sources': list(dict.fromkeys(provenance.get(uid, [])))})
    for (_, entity), group in same_original.items():
        if len(group) > 1:
            issues.append({'kind': 'duplicate_formal_original', 'entity': entity, 'ids': group})
    migrations, conflicts = [], []
    for old, primary in aliases.items():
        old_item = current.get(old) or archive.get('merged_reports', {}).get(old, {})
        target = current.get(primary, {})
        before = _wiki(old_item, state.get('reports', {}).get(old))
        after = _wiki(target, state.get('reports', {}).get(primary))
        if before['document'] and not after['document']:
            migrations.append({'old_id': old, 'primary_id': primary, 'old_document': before,
                               'condition': 'primary exists and has no independent Feishu document'})
        elif before['document'] and after['document'] and before['document'] != after['document']:
            conflicts.append({'old_id': old, 'primary_id': primary, 'old_document': before,
                              'primary_document': after, 'action': 'root must label duplicate; never delete automatically'})
    feishu_actions = []
    for uid, excluded in exclusions.items():
        item = current.get(uid) or archive.get('excluded_reports', {}).get(uid, {})
        wiki = _wiki(item, state.get('reports', {}).get(uid) or state.get('excluded_reports', {}).get(uid))
        if wiki['document']:
            feishu_actions.append({'action': 'label_excluded_document', 'id': uid, **wiki,
                'title': '【非技术报告·已排除】' + excluded.get('title', item.get('title', uid)),
                'notice': '该文档为研究资助公告，不包含独立技术方法或实验，已从技术报告目录和每日最新报告列表排除。',
                'reason': excluded.get('reason'), 'status': 'requires_root_feishu_sync'})
    return {'version': 1, 'generated_at': now(), 'source_files': source_files,
            'audit': {'catalogue_count': len(rows), 'fully_read': sum(i.get('read_status') == 'full_text_read' for i in rows),
                      'checks': 'metadata, summary presence, reading receipts, file existence; original bodies never opened',
                      'issues': issues}, 'updates': updates, 'aliases': aliases,
            'excluded_reports': list(exclusions.values()), 'protected_distinct_reports': SECURITY_REPORTS,
            'feishu_migrations': migrations, 'feishu_duplicate_documents': conflicts,
            'feishu_actions': feishu_actions}


def apply_plan(archive, state, plan):
    """Pure transformation; the caller holds pipeline.lock before saving."""
    archive, state = copy.deepcopy(archive), copy.deepcopy(state)
    items = {i['id']: i for i in archive['items']}
    changes, deferred = [], []
    for row in plan['updates']:
        item = items.get(row['id'])
        if item is None:
            deferred.append({'id': row['id'], 'reason': 'target not yet ingested'})
            continue
        for key, value in row['fields'].items():
            if key in PROTECTED_FIELDS or key not in METADATA_FIELDS | {'char_count'}:
                raise ValueError('unsafe metadata field: ' + key)
            if key == 'char_count':
                evidence = item.get('reading_evidence') or {}
                if (evidence.get('sha256') != row.get('char_count_evidence_sha')
                        or item.get('content_hash') != evidence.get('sha256')
                        or evidence.get('char_count') != value):
                    deferred.append({'id': row['id'], 'reason': 'reading evidence changed; char_count correction deferred'})
                    continue
            if item.get(key) != value:
                item[key] = copy.deepcopy(value)
                changes.append({'id': row['id'], 'field': key})
    for old, primary in plan['aliases'].items():
        if old in SECURITY_REPORTS or primary in SECURITY_REPORTS:
            raise ValueError('independent Google security reports cannot be merged')
        target, old_item = items.get(primary), items.get(old)
        if not target:
            if archive.get('aliases', {}).get(old) != primary:
                deferred.append({'id': old, 'reason': 'alias primary not yet ingested', 'primary_id': primary})
            continue
        if old_item and old_item.get('read_status') == 'full_text_read' and target.get('read_status') != 'full_text_read':
            deferred.append({'id': old, 'reason': 'retain read edition until primary completes reading', 'primary_id': primary})
            continue
        if archive.setdefault('aliases', {}).get(old) != primary:
            archive['aliases'][old] = primary
            changes.append({'id': old, 'alias': primary})
        old_item = old_item or archive.get('merged_reports', {}).get(old, {})
        if old in items:
            archive.setdefault('merged_reports', {})[old] = {**items.pop(old), 'merged_into': primary}
            changes.append({'id': old, 'retired_into': primary})
        for url in (old_item.get('url'), old_item.get('report_url')):
            if url and url not in target.get('related_urls', []):
                target.setdefault('related_urls', []).append(url)
                changes.append({'id': primary, 'related_url': url})
        old_cached = state.get('reports', {}).get(old, {})
        new_cached = state.get('reports', {}).get(primary, {})
        before, after = _wiki(old_item, old_cached), _wiki(target, new_cached)
        if before['document'] and not after['document']:
            for key in ('feishu_url', 'feishu_node_token', 'feishu_document_id'):
                if old_item.get(key):
                    target[key] = old_item[key]
            if not target.get('feishu_document_id'):
                target['feishu_document_id'] = before['document']
            migrated = {**old_cached, 'document': before['document'], 'migrated_from': old}
            migrated.pop('render_hash', None)
            state.setdefault('reports', {})[primary] = migrated
            state.setdefault('retired_reports', {})[old] = {**old_cached, 'merged_into': primary}
            state['reports'].pop(old, None)
            changes.append({'id': primary, 'feishu_mapping_migrated_from': old})
        elif before['document'] and after['document'] and before['document'] != after['document']:
            deferred.append({'id': old, 'primary_id': primary, 'reason': 'both editions have independent Feishu documents',
                             'old_document': before, 'primary_document': after})
    for excluded in plan['excluded_reports']:
        uid = excluded['id']
        if uid in items:
            archive.setdefault('excluded_reports', {})[uid] = {**items.pop(uid), 'excluded_reason': excluded.get('reason'),
                                                             'exclusion_evidence': excluded}
            if uid in state.get('reports', {}):
                state.setdefault('excluded_reports', {})[uid] = {**state['reports'].pop(uid),
                    'exclusion_reason': excluded.get('reason'), 'needs_label': True}
            changes.append({'id': uid, 'excluded_from_catalogue': True})
    archive['items'] = sorted(items.values(), key=lambda i: (i['published_at'], i['id']), reverse=True)
    if changes:
        archive['updated_at'] = now()
        archive.setdefault('runs', []).append({'at': now(), 'kind': 'verified_metadata_correction',
                                               'change_count': len(changes), 'deferred_count': len(deferred)})
    return archive, state, {'changes': changes, 'deferred': deferred,
                            'feishu_actions': plan['feishu_actions'], 'issues': plan['audit']['issues']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='lock, regenerate plan, and apply metadata only')
    parser.add_argument('--audit', action='store_true', help='write plan only (default)')
    args = parser.parse_args()
    if not args.apply:
        plan = build_plan()
        write_json(PLAN, plan)
        print(json.dumps({'plan': str(PLAN), 'updates': len(plan['updates']), 'issues': plan['audit']['issues'],
                          'feishu_duplicates': plan['feishu_duplicate_documents'], 'feishu_actions': plan['feishu_actions']}, ensure_ascii=False))
        return
    lock_path = ROOT / '.technical_reports/pipeline.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = load()
        state = _payload(STATE) if STATE.exists() else {'reports': {}}
        plan = build_plan(archive, state)
        write_json(PLAN, plan)
        updated, updated_state, result = apply_plan(archive, state, plan)
        if result['changes']:
            backup = ROOT / '.technical_reports/metadata-backups'
            tag = digest(json.dumps(archive, ensure_ascii=False, sort_keys=True))[:16]
            write_json(backup / (tag + '-archive.json'), archive)
            write_json(backup / (tag + '-feishu-state.json'), state)
            write_json(ARCHIVE, updated)
            if updated_state != state:
                write_json(STATE, updated_state)
            export_search()
        write_json(ROOT / '.technical_reports/metadata-apply-result.json', result)
        print(json.dumps({'changed_fields': len(result['changes']), 'deferred': result['deferred'],
                          'feishu_actions': result['feishu_actions'], 'issues': result['issues']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
