import argparse
import fcntl
import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from .common import ROOT, VENDORS, now, write_json
from .discovery import KnownReports
from .primary_refresh import refresh_primary_original
from .publication_recovery import recover_undated_discoveries
from .store import ARCHIVE, canonical_url, export_search, is_fully_read, load, merge, report_id


def _inherit_originals(item, old):
    item = dict(item)
    declared = {'supplementary_urls': [], 'own_original_urls': []}
    # Older manifests recorded the report's own attachments directly as
    # original_documents. Preserve these declarations on later rechecks.
    for document in old.get('original_documents', [])[1:]:
        url = document.get('url')
        if url:
            field = 'supplementary_urls' if document.get('role') == 'supplementary' else 'own_original_urls'
            declared[field].append(url)
    for field in ('supplementary_urls', 'own_original_urls'):
        urls = list(dict.fromkeys((old.get(field) or []) + declared[field] + (item.get(field) or [])))
        if urls:
            item[field] = urls
    return item


def _paper_identity(item):
    return bool(item.get('paper_id') or re.search(
        r'arxiv\.org/(?:abs|pdf|html)/\d{4}\.\d{4,5}',
        item.get('report_url', '') + ' ' + item['url']))


def _pending(item, error=None):
    item = {**item, 'read_status': 'pending_full_text', 'unchanged': False}
    for field in ('overview', 'summary', 'reading_evidence'):
        item.pop(field, None)
    if error is not None:
        item['error'] = error
    return item


def collect(start, end, vendors, output, *, recheck_archived=False):
    from .acquire import acquire
    archive = load()
    known = KnownReports(archive)
    modules = {'Anthropic': 'adapters_west', 'OpenAI': 'adapters_west',
               'DeepSeek': 'adapters_china', 'Kimi': 'adapters_china', 'GLM': 'adapters_china',
               'Google': 'adapters_google_seed', 'Seed': 'adapters_google_seed'}
    items, coverage, errors = [], {}, []
    for vendor in vendors:
        try:
            adapter = importlib.import_module('technical_reports.' + modules[vendor])
            result = (adapter.discover(vendor, start, end) if recheck_archived
                      else adapter.discover(vendor, start, end, known))
            coverage[vendor] = result.get('coverage', {})
            errors.extend({'vendor': vendor, 'error': str(e)} for e in result.get('errors', []))
            items.extend(result.get('items', []))
        except Exception as exc:
            errors.append({'vendor': vendor, 'error': type(exc).__name__ + ': ' + str(exc)[:250]})
    try:
        date_recovery = (recover_undated_discoveries(coverage, start, end) if recheck_archived
                         else recover_undated_discoveries(coverage, start, end, known=known))
        items.extend(date_recovery['items'])
    except Exception as exc:
        errors.append({'source': 'publication_date_recovery',
                       'error': type(exc).__name__ + ': publication date recovery failed'})
        date_recovery = {'diagnostics': [], 'new_date_verified_candidates': [], 'stats': {},
                         'complete_candidate_resolution': False}
    existing = {i['id']: i for i in archive['items']}
    retired = set(archive.get('aliases', {})) | set(archive.get('excluded_reports', {}))
    by_url = {}
    for uid, item in existing.items():
        if uid in retired:
            continue
        for field in ('canonical_url', 'report_url', 'url', 'original_url'):
            if item.get(field):
                key = (item['vendor'], canonical_url(item[field]))
                by_url.setdefault(key, set()).add(uid)

    def previous(item):
        uid = report_id(item)
        if uid in existing and uid not in retired:
            return existing[uid]
        for field in ('canonical_url', 'report_url', 'url', 'original_url'):
            if not item.get(field):
                continue
            ids = by_url.get((item['vendor'], canonical_url(item[field])), set())
            # A shared announcement can contain several distinct papers.
            if len(ids) != 1:
                continue
            old = existing[next(iter(ids))]
            if _paper_identity(item) and _paper_identity(old) and uid != report_id(old):
                continue
            return old
        return {}

    unique, archived_rechecks = {}, set()
    for item in items:
        uid = report_id(item)
        if not recheck_archived and known(item):
            continue
        if uid not in retired and start <= item.get('published_at', '')[:10] <= end:
            old = previous(item)
            uid = old.get('id', uid)
            unique[uid] = _inherit_originals(unique[uid], item) if uid in unique else item
    # Retry unresolved originals and detect revisions of already known reports,
    # including those whose initial publication predates the overlap window.
    for uid, item in existing.items():
        if recheck_archived and item['vendor'] in vendors and uid not in retired and item['published_at'][:10] <= end:
            if uid not in unique:
                unique[uid] = item
                archived_rechecks.add(uid)
    def prepare(item):
        old = previous(item)
        item = _inherit_originals(item, old)
        try:
            acquired = acquire(item)
            uid = report_id(acquired)
            if uid in retired or (not recheck_archived and known(acquired)):
                return None
            old = previous(acquired)
            complete = _inherit_originals(acquired, old)
            if any(complete.get(field) != acquired.get(field)
                   for field in ('supplementary_urls', 'own_original_urls')):
                # Identity may become known only after resolving a landing page.
                # Fetch inherited attachments before comparing the combined hash.
                acquired = acquire(complete)
                uid = report_id(acquired)
                if uid in retired:
                    return None
                old = previous(acquired)
            acquired['id'] = uid
            if (old.get('content_hash') == acquired['content_hash'] and is_fully_read(old)
                    and is_fully_read({**acquired, 'read_status': 'full_text_read'})):
                # Discovery may correct title/date/links without changing text.
                keep = {key: old[key] for key in ('overview', 'summary', 'reading_evidence', 'versions',
                         'first_seen_at', 'feishu_url', 'feishu_node_token', 'feishu_document_id') if key in old}
                return {**old, **acquired, **keep, 'read_status': 'full_text_read', 'error': None, 'unchanged': True}
            return _pending(acquired)
        except Exception as exc:
            failed = {**item, 'id': old.get('id', report_id(item))}
            return _pending(failed, type(exc).__name__ + ': ' + str(exc)[:250])

    def prepare_work(work):
        uid, item = work
        refresh = {'item': item, 'source_refresh_error': None, 'new_primary_candidate': None}
        if uid in archived_rechecks:
            shared = len(by_url.get((item['vendor'], canonical_url(item['url'])), set())) > 1
            refresh = refresh_primary_original(item, shared_landing=shared)
        return prepare(refresh['item']), refresh['source_refresh_error'], refresh['new_primary_candidate']

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(prepare_work, unique.items()))
    for index, (item, error, candidate) in enumerate(outcomes):
        if candidate and (candidate['id'] in retired or candidate['id'] in existing):
            state = 'retired' if candidate['id'] in retired else 'already_archived'
            error = {**error, 'new_primary_candidate_id': candidate['id'], 'new_identity_state': state,
                     'error': 'official landing now links to a ' + state + ' identity; confirm the current original for this historical report'}
            outcomes[index] = item, error, None
    source_refresh_errors = [error for _, error, _ in outcomes if error]
    new_primary_candidates = list({candidate['id']: candidate for _, _, candidate in outcomes if candidate}.values())
    resolved = {}
    for item, _, _ in outcomes:
        if item is None:
            continue
        uid = item['id']
        # Discovery precedes archived rechecks. An old unchanged edition must
        # not hide a fresh candidate's failed revision or newly required appendix.
        resolved.setdefault(uid, item)
    records = list(resolved.values())
    fetch_errors = [{'id': i['id'], 'vendor': i['vendor'], 'title': i['title'], 'error': i['error']}
                    for i in records if i.get('error')]
    instruction = 'Recover EVERY source_refresh_error through normal browser or official alternate full text. Verify each new_primary_candidate identity, title and first publication date before adding it to items; landing_title/landing_published_at are provenance, not the new paper metadata. Review EVERY new_date_verified_candidate against the technical-report scope using its official metadata and original. Record the scope decision with python -m technical_reports.publication_recovery --record-scope eligible|excluded|uncertain --vendor VENDOR --title TITLE --url URL --reason REASON --evidence-url SOURCE_URL, then repeat collect with the same start_date/end_date, vendors and output so eligible date-verified reports acquire their originals. Resolve pending publication_date_recovery diagnostics without inventing dates or treating publisher-only days as verified first release. Read EVERY reading chunk of EVERY new/changed report, including appendices. Never summarize only the abstract. Write overview, pyramid summary and exact sha256/char_count/read_chars/reader=agent/read_at evidence. Unresolved reports remain pending_full_text. Then ingest and sync.'
    if not recheck_archived:
        instruction = ('New reports only: inspect official indexes and pagination, and deduplicate against the archive before acquiring originals. '
                       'Never refresh, download, reread or repair archived reports, including archived pending records. '
                       'Review new_date_verified_candidates for scope using official evidence and record the decision with '
                       'python -m technical_reports.publication_recovery --record-scope eligible|excluded|uncertain '
                       '--vendor VENDOR --title TITLE --url URL --reason REASON --evidence-url SOURCE_URL. '
                       'Only newly published, eligible reports require complete original and appendix reading, receipts and summaries. '
                       'If items and new_date_verified_candidates are empty, skip reader, ingest, Feishu sync, GitHub and Render; '
                       'record source errors or unresolved new candidates honestly, then finish this run report and its final email.')
    payload = {'collected_at': now(), 'start_date': start, 'end_date': end, 'vendors': list(vendors), 'coverage': coverage,
               'mode': 'recheck_archived' if recheck_archived else 'new_only',
               'new_report_count': sum(i['id'] not in existing for i in records),
               'errors': errors, 'fetch_errors': fetch_errors, 'source_refresh_errors': source_refresh_errors,
               'publication_date_recovery': {key: value for key, value in date_recovery.items()
                                             if key not in ('items', 'new_date_verified_candidates')},
               'new_date_verified_candidates': date_recovery['new_date_verified_candidates'],
               'new_primary_candidates': new_primary_candidates, 'items': records,
               'pending_count': sum(not i.get('unchanged') for i in records),
               'agent_instruction': instruction}
    write_json(output, payload)
    return {'output': str(output), 'discovered': len(records), 'pending': payload['pending_count'], 'errors': errors,
            'fetch_failed_count': len(fetch_errors), 'source_refresh_errors': source_refresh_errors,
            'publication_date_recovery': date_recovery['stats'],
            'new_date_verified_candidate_count': len(date_recovery['new_date_verified_candidates']),
            'new_primary_candidate_count': len(new_primary_candidates)}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('collect')
    c.add_argument('--since', default=(date.fromisoformat(now()[:10]) - timedelta(days=7)).isoformat())
    c.add_argument('--until', default=now()[:10])
    c.add_argument('--vendors', nargs='+', choices=VENDORS, default=list(VENDORS))
    c.add_argument('--output', default=str(ROOT / '.technical_reports' / 'candidates.json'))
    c.add_argument('--recheck-archived', action='store_true',
                   help='Explicit maintenance: reacquire archived originals; never used by the daily new-report task')
    i = sub.add_parser('ingest')
    i.add_argument('--input', required=True)
    b = sub.add_parser('bootstrap')
    b.add_argument('--directory', default=str(ROOT / 'technical_reports' / 'bootstrap'))
    s = sub.add_parser('sync')
    s.add_argument('--no-today', action='store_true')
    s.add_argument('--catalogues-only', action='store_true', help='Update summary directories while preserving report child pages')
    sub.add_parser('stats')
    args = parser.parse_args()
    lock_path = ROOT / '.technical_reports' / 'pipeline.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'collect':
            date.fromisoformat(args.since); date.fromisoformat(args.until)
            if args.since > args.until:
                raise ValueError('start date must not exceed end date')
            result = collect(args.since, args.until, args.vendors, args.output, recheck_archived=args.recheck_archived)
        elif args.command in ('ingest', 'bootstrap'):
            paths = [Path(args.input)] if args.command == 'ingest' else sorted(Path(args.directory).glob('*.json'))
            if not paths:
                raise ValueError('no report input files')
            records, coverage = [], {}
            for path in paths:
                payload = json.loads(path.read_text(encoding='utf-8'))
                records.extend(payload if isinstance(payload, list) else payload.get('items', []))
                if isinstance(payload, dict):
                    coverage[str(path.name)] = payload.get('coverage', {})
            result = merge(records, coverage=coverage)
            result['search_days'] = export_search()
        elif args.command == 'sync':
            from .feishu import sync
            result = sync(update_today=not args.no_today, update_reports=not args.catalogues_only)
        else:
            items = load()['items']
            result = {'total': len(items), 'fully_read': sum(is_fully_read(i) for i in items),
                      'vendors': {v: sum(i['vendor'] == v for i in items) for v in VENDORS}}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
