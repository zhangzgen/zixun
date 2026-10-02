"""Reproducible publication-date lookup; index creation is never paper release.

The lookup keeps publisher-only dates separate from verified first arXiv
publication. A caller must review publisher-only records for prior manuscripts
before declaring complete first-publication coverage.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import html
from html.parser import HTMLParser
import json
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from .common import fetch_bytes, now, strip_html, write_json

_arxiv_request_lock = threading.Lock()
_arxiv_next_request = 0.0
_arxiv_blocked_until = 0.0


def normalized_title(value):
    value = html.unescape(re.sub(r'<[^>]+>', '', str(value or '')))
    return ''.join(char for char in unicodedata.normalize('NFKD', value).casefold() if char.isalnum())


def _check_title(actual, expected):
    if not actual or normalized_title(actual) != normalized_title(expected):
        raise ValueError('publication metadata title does not match the official research record')


def precise_date(value):
    """Return an explicit calendar value and precision without filling missing days."""
    if isinstance(value, list):
        if not 1 <= len(value) <= 3 or not all(isinstance(part, int) for part in value):
            return None
        value = '-'.join(f'{part:04d}' if index == 0 else f'{part:02d}' for index, part in enumerate(value))
    value = str(value or '').strip().replace('/', '-')
    match = re.match(r'^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:$|[T\s])', value)
    if not match:
        return None
    parts = [part for part in match.groups() if part is not None]
    try:
        date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
    except ValueError:
        return None
    normalized = '-'.join(f'{int(part):04d}' if index == 0 else f'{int(part):02d}' for index, part in enumerate(parts))
    return {'date': normalized, 'precision': ('year', 'month', 'day')[len(parts) - 1], 'raw': value}


def _evidence(source, field, value, kind):
    parsed = precise_date(value)
    return {**parsed, 'source': source, 'field': field, 'kind': kind} if parsed else None


def doi_from_url(url):
    parts = urlsplit(url)
    if (parts.hostname or '').lower() == 'doi.acm.org':
        identifier = parse_qs(parts.query).get('doi', [''])[0]
        return identifier if identifier.startswith('10.') else ('10.1145/' + identifier if identifier else None)
    match = re.search(r'(10\.\d{4,9}/[^?#\s]+)', unquote(parts.path), re.I)
    return match[1].removesuffix('.pdf').rstrip('/') if match else None


def paper_id_from_url(url):
    match = re.search(r'(?:arxiv\.org/(?:abs|pdf|html)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5})', url, re.I)
    return match[1] if match else None


def crossref_evidence(payload, expected_title, source):
    message = payload.get('message', payload)
    _check_title((message.get('title') or [''])[0], expected_title)
    evidence = []
    for field in ('published-online', 'published-print', 'published', 'issued'):
        parts = message.get(field, {}).get('date-parts', [])
        item = _evidence(source, field, parts[0] if parts else None, 'publisher_publication')
        if item:
            evidence.append(item)
    return evidence


def arxiv_atom_matches(xml, expected_title, source, *, paper_id=None, authors=None):
    ns = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
    matches = []
    expected_authors = {normalized_title(name) for name in authors or []}
    for entry in ET.fromstring(xml).findall('atom:entry', ns):
        title = entry.findtext('atom:title', default='', namespaces=ns)
        if normalized_title(title) != normalized_title(expected_title):
            continue
        identifier = paper_id_from_url(entry.findtext('atom:id', default='', namespaces=ns))
        if not identifier or (paper_id and identifier != re.sub(r'v\d+$', '', paper_id)):
            continue
        names = [node.text or '' for node in entry.findall('atom:author/atom:name', ns)]
        if expected_authors and not expected_authors.intersection(normalized_title(name) for name in names):
            continue
        published = entry.findtext('atom:published', default='', namespaces=ns)
        evidence = _evidence(source, 'entry.published', published, 'arxiv_first_submission')
        if not evidence or evidence['precision'] != 'day':
            continue
        pdf = next((link.get('href') for link in entry.findall('atom:link', ns) if link.get('type') == 'application/pdf'), 'https://arxiv.org/pdf/' + identifier)
        matches.append({'paper_id': identifier, 'title': ' '.join(title.split()), 'authors': names,
                        'report_url': pdf.replace('http://', 'https://', 1),
                        'doi': entry.findtext('arxiv:doi', default='', namespaces=ns),
                        'updated_at': entry.findtext('atom:updated', default='', namespaces=ns),
                        'evidence': {**evidence, 'title_match': 'exact_normalized', 'authors': names}})
    return matches


def zenodo_evidence(payload, expected_title, source):
    metadata = payload['metadata']
    _check_title(metadata.get('title'), expected_title)
    evidence = _evidence(source, 'metadata.publication_date', metadata.get('publication_date'), 'repository_publication')
    return [evidence] if evidence else []


def openreview_evidence(payload, expected_title, source):
    evidence = []
    for note in payload.get('notes', []):
        content_title = note.get('content', {}).get('title', '')
        title = content_title.get('value', '') if isinstance(content_title, dict) else content_title
        if normalized_title(title) != normalized_title(expected_title):
            continue
        # cdate/tcdate may precede public visibility; mdate is only modification.
        if note.get('pdate') and 'everyone' in note.get('readers', []):
            value = datetime.fromtimestamp(note['pdate'] / 1000, timezone.utc).date().isoformat()
            evidence.append(_evidence(source, 'pdate', value, 'openreview_public_release'))
    return evidence


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata, self.links = {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = attrs.get('name', attrs.get('property', '')).lower()
            self.metadata[name] = attrs.get('content', '')
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])


def publisher_html_evidence(markup, expected_title, source):
    host = (urlsplit(source).hostname or '').lower()
    if host in {'icml.cc', 'neurips.cc', 'nips.cc', 'iclr.cc'}:
        return []  # Conference poster/page dates are site-entry metadata.
    parser = _Metadata()
    parser.feed(markup)
    _check_title(parser.metadata.get('citation_title') or parser.metadata.get('dc.title') or parser.metadata.get('og:title'), expected_title)
    evidence = []
    for field in ('citation_publication_date', 'citation_online_date', 'citation_date',
                  'dc.date', 'dc.date.issued', 'dcterms.issued', 'article:published_time'):
        item = _evidence(source, field, parser.metadata.get(field), 'publisher_publication')
        if item:
            evidence.append(item)
    if host in {'tdcommons.org', 'www.tdcommons.org'}:
        # Digital Commons pages expose the disclosure date in the visible
        # publication field or recommended citation, rather than meta tags.
        text = strip_html(markup)
        published = re.search(r'Publication Date\s+(\d{4}-\d{2}-\d{2})', text)
        if published:
            item = _evidence(source, 'Publication Date', published[1], 'repository_publication')
        else:
            citation = re.search(r'Technical Disclosure Commons,\s*\(([A-Z][a-z]+ \d{1,2}, \d{4})\)', text)
            try:
                value = datetime.strptime(citation[1], '%B %d, %Y').date().isoformat() if citation else None
            except ValueError:
                value = None
            item = _evidence(source, 'Recommended Citation publication date', value, 'repository_publication')
        if item:
            evidence.append(item)
    return evidence


def _fetch_text(url):
    global _arxiv_next_request, _arxiv_blocked_until
    is_arxiv_api = urlsplit(url).hostname == 'export.arxiv.org' and urlsplit(url).path.startswith('/api/')
    if is_arxiv_api:
        with _arxiv_request_lock:
            current = time.monotonic()
            if current < _arxiv_blocked_until:
                raise OSError('arXiv API rate limited; publication lookup deferred')
            if current < _arxiv_next_request:
                time.sleep(_arxiv_next_request - current)
            _arxiv_next_request = time.monotonic() + 3.0
    try:
        return fetch_bytes(url)[0].decode('utf-8', 'replace')
    except HTTPError as exc:
        if is_arxiv_api and exc.code == 429:
            # Do not send another request for every queued title while blocked.
            # A later run can retry; publisher lookups remain independent.
            retry_after = str(exc.headers.get('Retry-After', '') if exc.headers else '')
            cooldown = max(60, int(retry_after)) if retry_after.isdigit() else 60
            with _arxiv_request_lock:
                _arxiv_blocked_until = time.monotonic() + cooldown
        raise


def resolve_publication_date(record, *, fetcher=None, search_arxiv=True):
    """Look up exact identity and date evidence; perform no archive mutations.

    fetcher(url) -> text is injectable for offline tests or a caller's cache.
    publisher_date_only is intentionally distinct from verified_first_publication.
    """
    fetcher = fetcher or _fetch_text
    title = record['title']
    sources = list(dict.fromkeys([*(record.get('report_urls') or []), record.get('report_url', '')]))
    sources = [source for source in sources if source]
    evidence, errors, report_urls, checked, arxiv_matches = [], [], list(sources), [], []
    authors = list(record.get('authors') or [])
    paper = record.get('paper_id') or next(filter(None, (paper_id_from_url(source) for source in sources)), None)
    dois = list(dict.fromkeys(filter(None, (doi_from_url(source) for source in sources))))
    dois = [doi for doi in dois if not doi.lower().startswith('10.48550/arxiv.')]

    def fetch(source):
        checked.append(source)
        try:
            return fetcher(source)
        except Exception as exc:
            error = {'source': source, 'error': type(exc).__name__ + ': publication metadata fetch failed'}
            if isinstance(getattr(exc, 'code', None), int):
                error['status_code'] = exc.code
            errors.append(error)
            return None

    for doi in dois:
        source = 'https://api.crossref.org/works/' + quote(doi, safe='')
        text = fetch(source)
        if text is None:
            continue
        try:
            data = json.loads(text)
            evidence.extend(crossref_evidence(data, title, source))
            message = data.get('message', data)
            if not authors:
                authors = [' '.join(filter(None, (author.get('given'), author.get('family')))) for author in message.get('author', [])]
            report_urls.extend(link['URL'] for link in message.get('link', []) if link.get('URL') and (link.get('content-type') == 'application/pdf' or '/pdf/' in link['URL']))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append({'source': source, 'error': str(exc)})

    pending = list(sources)
    visited = set()
    while pending:
        source = pending.pop(0)
        if source in visited or doi_from_url(source) or paper_id_from_url(source) or re.search(r'\.pdf(?:$|[?#])', source, re.I):
            continue
        visited.add(source)
        host = (urlsplit(source).hostname or '').lower()
        if host.endswith('zenodo.org') and re.search(r'/records/\d+', source):
            identifier = re.search(r'/records/(\d+)', source)[1]
            target = 'https://zenodo.org/api/records/' + identifier
            text = fetch(target)
            if text is not None:
                try:
                    data = json.loads(text)
                    evidence.extend(zenodo_evidence(data, title, target))
                    report_urls.extend(file['links']['self'] for file in data.get('files', []) if file.get('key', '').lower().endswith('.pdf'))
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({'source': target, 'error': str(exc)})
        elif host == 'openreview.net':
            identifier = parse_qs(urlsplit(source).query).get('id', [''])[0]
            if not identifier:
                continue
            target = 'https://api2.openreview.net/notes?id=' + quote(identifier)
            text = fetch(target)
            if text is not None:
                try:
                    data = json.loads(text)
                    found = openreview_evidence(data, title, target)
                    evidence.extend(found)
                    if found:
                        report_urls.append('https://openreview.net/pdf?id=' + quote(identifier))
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({'source': target, 'error': str(exc)})
        else:
            text = fetch(source)
            if text is not None:
                try:
                    evidence.extend(publisher_html_evidence(text, title, source))
                    parser = _Metadata(); parser.feed(text)
                    if host in {'icml.cc', 'neurips.cc', 'nips.cc', 'iclr.cc'}:
                        pending.extend(urljoin(source, link) for link in parser.links if urlsplit(urljoin(source, link)).hostname == 'openreview.net')
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({'source': source, 'error': str(exc)})

    arxiv_search_status = 'not_requested'
    if paper or search_arxiv:
        params = {'id_list': re.sub(r'v\d+$', '', paper)} if paper else {'search_query': 'ti:"' + title + '"', 'start': 0, 'max_results': 5}
        source = 'https://export.arxiv.org/api/query?' + urlencode(params)
        text = fetch(source)
        if text is None:
            arxiv_search_status = 'unavailable'
        else:
            try:
                arxiv_matches = arxiv_atom_matches(text, title, source, paper_id=paper, authors=authors)
                arxiv_search_status = 'exact_match' if arxiv_matches else 'no_exact_match'
            except (ET.ParseError, ValueError, TypeError) as exc:
                arxiv_search_status = 'invalid_response'
                errors.append({'source': source, 'error': type(exc).__name__ + ': arXiv date response could not be verified'})
    distinct = {match['paper_id']: match for match in arxiv_matches}
    if len(distinct) == 1:
        match = next(iter(distinct.values()))
        evidence.append(match['evidence'])
        report_urls.append(match['report_url'])
    evidence = list({(item['source'], item['field'], item['date']): item for item in evidence}.values())
    days = [item for item in evidence if item['precision'] == 'day']
    earliest = min(days, key=lambda item: item['date']) if days else None
    verified = len(distinct) == 1
    result = {'status': 'verified_first_publication' if verified else ('ambiguous_arxiv_identity' if len(distinct) > 1 else 'publisher_date_only' if earliest else 'unresolved'),
              'published_at': earliest['date'] if earliest else None, 'date_precision': 'day' if earliest else None,
              'date_source': earliest['source'] if earliest else None, 'first_publication_verified': verified,
              'publication_date_evidence': evidence, 'arxiv_matches': arxiv_matches, 'arxiv_search_status': arxiv_search_status,
              'report_urls': list(dict.fromkeys(report_urls)), 'checked_sources': checked, 'errors': errors}
    if verified:
        match = next(iter(distinct.values()))
        result.update(paper_id=match['paper_id'], report_url=match['report_url'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    payload = json.loads(open(args.input, encoding='utf-8').read())
    records = payload if isinstance(payload, list) else payload.get('items', [])
    def verify(record):
        try:
            return {**record, 'publication_date_lookup': resolve_publication_date(record)}
        except Exception as exc:
            return {**record, 'publication_date_lookup': {'status': 'unresolved', 'errors': [{'error': type(exc).__name__ + ': metadata lookup failed'}]}}
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as pool:
        results = list(pool.map(verify, records))
    write_json(args.output, {'checked_at': now(), 'date_probes': results})
    print(json.dumps({'output': args.output, 'checked': len(results), 'verified_first_publication': sum(row['publication_date_lookup']['status'] == 'verified_first_publication' for row in results)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
