"""Recover explicit undated discoveries with verified dates and reviewed scope.

The public audit contains metadata only. New scope decisions and date lookup
cache live locally; neither is a substitute for acquiring and reading originals.
"""
import argparse
import calendar
import fcntl
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .common import ROOT, VENDORS, digest, now, write_json
from .publication_dates import normalized_title, precise_date, resolve_publication_date
from .store import canonical_url, report_id


PUBLIC_AUDIT = Path(__file__).with_name('publication_date_audit.json')
LOCAL_SCOPE = ROOT / '.technical_reports' / 'publication_scope_decisions.json'
DATE_CACHE = ROOT / '.technical_reports' / 'publication_date_cache.json'
_CANDIDATE_FIELDS = {'undated_candidates', 'unverified_date_candidates'}
_PUBLIC_DATE_KINDS = {'arxiv_first_submission', 'publisher_publication',
                      'repository_publication', 'openreview_public_release'}


def _identity(record):
    return digest(':'.join((str(record['vendor']).casefold(),
                            canonical_url(record['url']), normalized_title(record['title']))))


def _read(path, fallback):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback


def record_scope_decision(vendor, title, url, scope_status, reason, *,
                          decisions_path=None, evidence_urls=None):
    """Persist an Agent's scope review for this exact vendor, URL and title.

    Date evidence remains separate. An eligible decision can only enter a later
    collection after its exact first-publication date is independently verified.
    """
    vendor = next((name for name in VENDORS if name.casefold() == str(vendor).casefold()), None)
    if vendor is None:
        raise ValueError('unknown vendor')
    if scope_status not in {'eligible', 'excluded', 'uncertain'}:
        raise ValueError('scope must be eligible, excluded or uncertain')
    if not str(title).strip() or not str(reason).strip():
        raise ValueError('scope decision requires a title and a reason')
    if urlsplit(url).scheme not in {'http', 'https'} or not urlsplit(url).hostname:
        raise ValueError('scope decision requires an HTTP(S) report landing URL')
    path = Path(decisions_path or LOCAL_SCOPE)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {'vendor': vendor, 'title': str(title).strip(), 'url': url,
              'scope_status': scope_status, 'scope_reason': str(reason).strip(),
              'scope_evidence_urls': list(dict.fromkeys(evidence_urls or [])), 'audited_at': now()}
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read(path, {'version': 1, 'items': []})
        entries = {_identity(item): item for item in state.get('items', [])}
        entries[_identity(record)] = record
        write_json(path, {'version': 1, 'updated_at': now(), 'items': list(entries.values())})
    return record


def _candidates(coverage):
    """Traverse only explicitly declared undated lists, never cited references."""
    records = {}
    def walk(node, vendor, path):
        if isinstance(node, dict):
            for field, value in node.items():
                if field in _CANDIDATE_FIELDS and isinstance(value, list):
                    for item in value:
                        if not isinstance(item, dict) or not item.get('title') or not item.get('url'):
                            continue
                        record = {**item, 'vendor': vendor}
                        key = _identity(record)
                        if key not in records:
                            records[key] = {**record, 'discovery_paths': []}
                        previous = records[key]
                        previous['report_urls'] = list(dict.fromkeys(
                            (previous.get('report_urls') or []) + (record.get('report_urls') or [])
                            + ([record['report_url']] if record.get('report_url') else [])))
                        previous['discovery_paths'].append('.'.join(path + [field]))
                else:
                    walk(value, vendor, path + [field])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, vendor, path + [str(index)])
    for vendor, state in coverage.items():
        if vendor in VENDORS:
            walk(state, vendor, [vendor])
    return list(records.values())


def _latest_possible(value):
    parsed = precise_date(value)
    if not parsed:
        return None
    parts = [int(part) for part in parsed['date'].split('-')]
    if parsed['precision'] == 'year':
        return f'{parts[0]:04d}-12-31'
    if parsed['precision'] == 'month':
        return f'{parts[0]:04d}-{parts[1]:02d}-{calendar.monthrange(*parts)[1]:02d}'
    return parsed['date']


def _outside_bound(lookup, audit, start):
    bounds = []
    explicit = audit.get('first_publication_upper_bound')
    if explicit and audit.get('date_bound_evidence'):
        upper = _latest_possible(explicit)
        if upper and upper < start:
            bounds.append({'upper_bound': upper, 'evidence': audit['date_bound_evidence']})
    for evidence in lookup.get('publication_date_evidence', []):
        if evidence.get('kind') not in _PUBLIC_DATE_KINDS:
            continue
        upper = _latest_possible(evidence.get('date'))
        if upper and upper < start:
            bounds.append({'upper_bound': upper, 'evidence': [evidence]})
    return min(bounds, key=lambda row: row['upper_bound']) if bounds else None


def _verified_day(lookup):
    parsed = precise_date(lookup.get('published_at'))
    if (lookup.get('status') != 'verified_first_publication'
            or not lookup.get('first_publication_verified')
            or not parsed or parsed['precision'] != 'day'):
        return None
    evidence = lookup.get('publication_date_evidence', [])
    if not any(row.get('date') == parsed['date'] and row.get('precision') == 'day'
               and row.get('kind') == 'arxiv_first_submission' for row in evidence):
        return None
    # An earlier publisher date is a public upper bound, not a verified first day.
    if any(row.get('kind') in _PUBLIC_DATE_KINDS and _latest_possible(row.get('date'))
           and _latest_possible(row['date']) < parsed['date'] for row in evidence):
        return None
    return parsed['date']


def _fresh(entry, checked_at, retry_after_seconds):
    try:
        age = (datetime.fromisoformat(checked_at) - datetime.fromisoformat(entry['checked_at'])).total_seconds()
        return 0 <= age < retry_after_seconds
    except (KeyError, ValueError, TypeError):
        return False


def _source_signature(record):
    return digest(json.dumps({'title': record['title'], 'paper_id': record.get('paper_id'),
                              'authors': record.get('authors') or [],
                              'report_urls': sorted(set(record.get('report_urls') or []))},
                             sort_keys=True, ensure_ascii=False))


def _item(candidate, lookup, published, scope):
    # Reuse immutable first-date evidence, but fetch the current arXiv original
    # on every collection so a cached lookup cannot pin an obsolete version.
    paper = lookup.get('paper_id') or candidate.get('paper_id')
    primary = 'https://arxiv.org/pdf/' + paper if paper else lookup.get('report_url')
    item = {key: candidate[key] for key in (
        'vendor', 'title', 'url', 'publisher', 'source', 'language', 'content_type',
        'supplementary_urls', 'own_original_urls') if key in candidate}
    item.update(source=item.get('source') or candidate['vendor'] + ' official research',
                language=item.get('language', 'en'), content_type=item.get('content_type', 'research_paper'),
                published_at=published, date_precision='day', date_source=lookup.get('date_source'),
                first_publication_verified=True, publication_date_evidence=lookup['publication_date_evidence'],
                scope_status='eligible', scope_reason=scope.get('scope_reason'),
                scope_evidence_urls=scope.get('scope_evidence_urls', []), date_recovered=True,
                related_urls=list(dict.fromkeys((candidate.get('report_urls') or []) + lookup.get('report_urls', []))))
    if primary:
        item['report_url'] = primary
    if paper:
        item['paper_id'] = paper
    item['id'] = report_id(item)
    return item


def recover_undated_discoveries(coverage, start, end, *, fetcher=None, audit_path=None,
                                scope_path=None, cache_path=None, retry_after_seconds=86400,
                                checked_at=None, known=None):
    """Return reader candidates only after both exact date and scope verification.

    Failed/negative metadata is retried after 24 hours or when sources change.
    Existing exclusions, duplicate identities and dates already proving an
    outside-window publication avoid repeated calls. Coverage remains explicit
    about unresolved dates and unreviewed scope.
    """
    date.fromisoformat(start); date.fromisoformat(end)
    if start > end:
        raise ValueError('start date must not exceed end date')
    checked_at = checked_at or now()
    audits = {_identity(row): row for row in _read(audit_path or PUBLIC_AUDIT, {}).get('items', [])}
    scopes = {_identity(row): row for row in _read(scope_path or LOCAL_SCOPE, {}).get('items', [])}
    cache_path = Path(cache_path or DATE_CACHE)
    cache = _read(cache_path, {'version': 1, 'entries': {}})
    entries = cache.setdefault('entries', {})
    items, diagnostics, reviews = [], [], []
    stats = {'candidate_count': 0, 'lookup_count': 0, 'cache_hit_count': 0,
             'recovered_count': 0, 'scope_review_count': 0, 'excluded_scope_count': 0,
             'duplicate_count': 0, 'outside_window_count': 0, 'pending_date_count': 0}
    cache_changed = False
    for candidate in _candidates(coverage):
        if known and known(candidate):
            continue
        stats['candidate_count'] += 1
        key = _identity(candidate)
        audit, scope = audits.get(key, {}), scopes.get(key, audits.get(key, {}))
        diagnostic = {key: candidate[key] for key in ('vendor', 'title', 'url', 'discovery_paths')}
        diagnostic.update(scope_status=scope.get('scope_status', 'not_reviewed'),
                          scope_reason=scope.get('scope_reason'))
        audited_lookup = audit.get('date_lookup') or {}
        audited_bound = _outside_bound(audited_lookup, audit, start)
        audited_day = _verified_day(audited_lookup)
        if audit.get('scope_status') == 'duplicate':
            diagnostic.update(status='duplicate', duplicate_of=audit.get('duplicate_of'))
            stats['duplicate_count'] += 1
        elif audited_bound or (audited_day and not start <= audited_day <= end):
            diagnostic.update(status='outside_window', date_lookup=audited_lookup,
                              lookup_source='curated_audit')
            if audited_bound:
                diagnostic['first_publication_upper_bound'] = audited_bound['upper_bound']
                diagnostic['date_bound_evidence'] = audited_bound['evidence']
            stats['outside_window_count'] += 1
        elif scope.get('scope_status') == 'excluded':
            diagnostic['status'] = 'excluded_scope'
            stats['excluded_scope_count'] += 1
        else:
            lookup = audit.get('date_lookup') or {}
            bound = _outside_bound(lookup, audit, start)
            record = {**candidate}
            record['title'] = audit.get('canonical_paper_title') or candidate['title']
            if audit.get('authors'):
                record['authors'] = audit['authors']
            record['report_urls'] = list(dict.fromkeys((candidate.get('report_urls') or [])
                + (audit.get('report_urls') or []) + lookup.get('report_urls', [])))
            signature = _source_signature(record)
            cached = entries.get(key, {})
            new_sources = set(candidate.get('report_urls') or []) - set(
                (audit.get('report_urls') or []) + lookup.get('report_urls', []))
            if bound:
                lookup_source = 'curated_audit'
            elif _verified_day(lookup):
                lookup_source = 'curated_audit'
            elif cached.get('source_signature') == signature and (
                    _verified_day(cached.get('lookup', {}))
                    or _outside_bound(cached.get('lookup', {}), {}, start)
                    or _fresh(cached, checked_at, retry_after_seconds)):
                lookup, lookup_source = cached['lookup'], 'local_cache'
                stats['cache_hit_count'] += 1
            elif lookup and not new_sources and _fresh(
                    {'checked_at': audit.get('audited_at')}, checked_at, retry_after_seconds):
                lookup_source = 'curated_audit'
            else:
                try:
                    lookup = resolve_publication_date(record, fetcher=fetcher)
                except Exception as exc:
                    lookup = {'status': 'unresolved', 'first_publication_verified': False,
                              'errors': [{'error': type(exc).__name__ + ': publication metadata lookup failed'}]}
                entries[key] = {'checked_at': checked_at, 'source_signature': signature, 'lookup': lookup}
                cache_changed = True
                lookup_source = 'live_lookup'
                stats['lookup_count'] += 1
            diagnostic.update(date_lookup=lookup, lookup_source=lookup_source)
            published = _verified_day(lookup)
            bound = _outside_bound(lookup, audit, start)
            if bound or (published and not start <= published <= end):
                diagnostic.update(status='outside_window')
                if bound:
                    diagnostic['first_publication_upper_bound'] = bound['upper_bound']
                    diagnostic['date_bound_evidence'] = bound['evidence']
                stats['outside_window_count'] += 1
            elif published and scope.get('scope_status') == 'eligible':
                items.append(_item(candidate, lookup, published, scope))
                diagnostic['status'] = 'recovered'
                stats['recovered_count'] += 1
            elif published:
                review = {**candidate, 'published_at': published, 'date_precision': 'day',
                          'first_publication_verified': True, 'publication_date_lookup': lookup,
                          'scope_status': scope.get('scope_status', 'not_reviewed'),
                          'scope_reason': scope.get('scope_reason'),
                          'review_required': 'Agent scope review and a recorded local decision before collection'}
                if lookup.get('paper_id'):
                    review['paper_id'] = lookup['paper_id']
                if lookup.get('report_url'):
                    review['report_url'] = lookup['report_url']
                review['id'] = report_id(review)
                reviews.append(review)
                diagnostic['status'] = 'pending_scope_review'
                stats['scope_review_count'] += 1
            else:
                diagnostic['status'] = 'pending_first_publication_verification' if lookup.get('published_at') else 'pending_exact_publication_date'
                stats['pending_date_count'] += 1
        diagnostics.append(diagnostic)
    if cache_changed:
        write_json(cache_path, {**cache, 'version': 1, 'updated_at': checked_at})
    return {'items': items, 'diagnostics': diagnostics,
            'new_date_verified_candidates': reviews, 'stats': stats,
            'complete_candidate_resolution': not stats['scope_review_count'] and not stats['pending_date_count']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record-scope', choices=('eligible', 'excluded', 'uncertain'), required=True)
    parser.add_argument('--vendor', choices=VENDORS, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--evidence-url', action='append', default=[])
    parser.add_argument('--decisions-path')
    args = parser.parse_args()
    record = record_scope_decision(args.vendor, args.title, args.url, args.record_scope, args.reason,
                                   decisions_path=args.decisions_path, evidence_urls=args.evidence_url)
    print(json.dumps({'scope_decision': record, 'decisions_path': str(args.decisions_path or LOCAL_SCOPE)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
