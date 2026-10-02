"""Recheck an archived paper's explicit download link on its official page."""
import html
import re
import urllib.error
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .adapters_west import _ReportLinks, _document_url, primary_original
from .common import fetch_bytes, now
from .store import canonical_url, report_id


LANDING_DOMAINS = {
    'Anthropic': ('anthropic.com', 'transformer-circuits.pub'),
    'OpenAI': ('openai.com',),
}
DOCUMENT_DOMAINS = {
    'Anthropic': ('anthropic.com', 'transformer-circuits.pub', 'cdn.sanity.io'),
    'OpenAI': ('openai.com', 'oaistatic.com'),
}
PAPER_DOMAINS = ('arxiv.org', 'biorxiv.org', 'medrxiv.org')


def _host_matches(host, domains):
    return any(host == domain or host.endswith('.' + domain) for domain in domains)


def _safe_url(url):
    try:
        parts = urlsplit(url)
        return parts.scheme == 'https' and not parts.username and not parts.password and parts.port in (None, 443)
    except ValueError:
        return False


def needs_primary_refresh(item):
    vendor = item.get('vendor')
    landing = item.get('url', '')
    original = item.get('report_url', '')
    return bool(vendor in LANDING_DOMAINS and original and
                canonical_url(landing) != canonical_url(original) and
                _safe_url(landing) and
                _host_matches((urlsplit(landing).hostname or '').lower(), LANDING_DOMAINS[vendor]) and
                not re.search(r'\.pdf(?:$|[?#])', landing, re.I))


def _paper_id(url):
    parts = urlsplit(url)
    if not _host_matches((parts.hostname or '').lower(), ('arxiv.org',)):
        return None
    match = re.match(r'/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$', parts.path.rstrip('/'))
    return match.group(1) if match else None


def _stored_paper_id(item):
    return re.sub(r'v\d+$', '', item['paper_id']) if item.get('paper_id') else _paper_id(item.get('report_url', ''))


def _title_key(value):
    return ''.join(re.findall(r'\w+', html.unescape(value or '').casefold()))


class _ScopedLinks(_ReportLinks):
    def __init__(self):
        super().__init__()
        self.headings = {}

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'a' and self.anchor is not None:
            self.anchor['scope'] = tuple(self.headings[level] for level in sorted(self.headings))

    def handle_endtag(self, tag):
        heading = ' '.join(''.join(self.heading).split()) if tag in {'h1', 'h2', 'h3', 'h4'} and self.heading is not None else None
        super().handle_endtag(tag)
        if heading is not None:
            level = int(tag[1])
            self.headings = {key: value for key, value in self.headings.items() if key < level}
            self.headings[level] = heading


class _OnlyTarget(HTMLParser):
    """Keep page context while letting the existing resolver inspect one target."""
    def __init__(self, base, target):
        super().__init__(convert_charrefs=False)
        self.base, self.target, self.parts = base, target, []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta' and attributes.get('name', attributes.get('property')) == 'citation_pdf_url':
            return
        if tag == 'a' and _document_url(self.base, attributes.get('href', '')) != self.target:
            attrs = [(key, value) for key, value in attrs if key != 'href']
            suffix = ''.join(' ' + key + ('="' + html.escape(value, quote=True) + '"' if value is not None else '') for key, value in attrs)
            self.parts.append('<' + tag + suffix + '>')
        else:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append('&' + name + ';')

    def handle_charref(self, name):
        self.parts.append('&#' + name + ';')

    def handle_comment(self, data):
        self.parts.append('<!--' + data + '-->')

    def handle_decl(self, decl):
        self.parts.append('<!' + decl + '>')


def _candidates(base, page, parser, item):
    targets = list(dict.fromkeys(_document_url(base, link['href']) for link in parser.links))
    explicit = []
    for target in filter(None, targets):
        masked = _OnlyTarget(base, target)
        masked.feed(page)
        resolved = primary_original(base, ''.join(masked.parts))
        if resolved.get('report_url') == target:
            explicit.append(target)
    if explicit:
        # An audited additional original is not a competing main paper.
        additional = {canonical_url(url) for url in item.get('own_original_urls', [])}
        mains = [url for url in explicit if canonical_url(url) not in additional]
        return mains or explicit
    resolved = primary_original(base, page)
    target = resolved.get('report_url')
    metadata_title = parser.metadata.get('citation_title')
    same_paper = target and _paper_id(target) and _paper_id(target) == _stored_paper_id(item)
    if target and metadata_title and (same_paper or _title_key(metadata_title) == _title_key(item.get('title'))):
        return [target]
    return []


def _trusted_document(url, item):
    if not _safe_url(url):
        return False
    host = (urlsplit(url).hostname or '').lower()
    if _host_matches(host, DOCUMENT_DOMAINS[item['vendor']] + PAPER_DOMAINS):
        return True
    # Previously audited project-hosted originals may be checked again, but do
    # not expand trust to arbitrary new domains linked by a public article.
    audited = [item.get('report_url', ''), *(item.get('own_original_urls') or []), *(item.get('supplementary_urls') or [])]
    return canonical_url(url) in {canonical_url(value) for value in audited if value}


def _choose_target(base, targets, item, parser, shared_landing):
    paper = _stored_paper_id(item)
    matches = [url for url in targets if paper and _paper_id(url) == paper]
    if len(matches) == 1:
        return matches[0]
    known = {canonical_url(item[field]) for field in ('canonical_url', 'report_url', 'original_url') if item.get(field)}
    matches = [url for url in targets if canonical_url(url) in known]
    if len(matches) == 1:
        return matches[0]
    title = _title_key(item.get('title'))
    matches = [url for url in targets if title and any(
        _document_url(base, link['href']) == url and not link['references'] and
        any(_title_key(heading) == title for heading in link.get('scope', ()))
        for link in parser.links)]
    if len(matches) == 1:
        return matches[0]
    if len(targets) == 1 and (not shared_landing or _paper_id(targets[0])):
        return targets[0]
    return None


def _supplements(base, page, parser, targets, selected, item):
    declared = primary_original(base, page).get('supplementary_urls', [])
    if len(targets) == 1:
        return declared, False
    selected_scopes = [link.get('scope', ()) for link in parser.links
                       if _document_url(base, link['href']) == selected and not link['references']]
    other_scopes = [link.get('scope', ()) for link in parser.links
                    if _document_url(base, link['href']) in targets and _document_url(base, link['href']) != selected and not link['references']]
    prefixes = [scope[:length] for scope in selected_scopes for length in range(1, len(scope) + 1)
                if not any(other[:length] == scope[:length] for other in other_scopes)]
    other_prefixes = [scope[:length] for scope in other_scopes for length in range(1, len(scope) + 1)
                      if not any(selected_scope[:length] == scope[:length] for selected_scope in selected_scopes)]
    accepted, ambiguous = [], False
    audited = {canonical_url(url) for url in item.get('supplementary_urls', [])}
    for url in declared:
        scopes = [link.get('scope', ()) for link in parser.links if _document_url(base, link['href']) == url and not link['references']]
        if canonical_url(url) in audited or any(scope[:len(prefix)] == prefix for scope in scopes for prefix in prefixes):
            accepted.append(url)
        elif not any(scope[:len(prefix)] == prefix for scope in scopes for prefix in other_prefixes):
            ambiguous = True
    return accepted, ambiguous


def refresh_primary_original(item, *, shared_landing=False):
    """Return a checked copy, separate metadata candidates, and actionable errors."""
    item = dict(item)
    outcome = {'item': item, 'source_refresh_error': None, 'new_primary_candidate': None}
    if not needs_primary_refresh(item):
        return outcome
    landing = item['url']
    evidence = {'landing_url': landing, 'previous_report_url': item['report_url']}

    def finish(status, error=None):
        item.update(primary_link_check_status=status, primary_link_checked_at=now(),
                    primary_link_check_evidence=evidence, primary_link_check_error=error)
        if error:
            outcome['source_refresh_error'] = {
                'id': item.get('id') or report_id(item), 'vendor': item['vendor'],
                'title': item.get('title', ''), 'url': landing, 'report_url': evidence['previous_report_url'],
                'status': status, 'error': error, **evidence,
            }
        return outcome

    try:
        raw, final_url, content_type = fetch_bytes(landing)
    except urllib.error.HTTPError as exc:
        return finish('blocked' if exc.code in (401, 403, 429) else 'unavailable', f'HTTP {exc.code}: official landing page could not be checked')
    except Exception as exc:
        return finish('unavailable', type(exc).__name__ + ': official landing page fetch failed')
    if not _safe_url(final_url) or not _host_matches((urlsplit(final_url).hostname or '').lower(), LANDING_DOMAINS[item['vendor']]):
        return finish('unresolved', 'official landing redirected outside trusted publication domains')
    page = raw.decode('utf-8', 'replace')
    if 'html' not in content_type.lower() and not re.search(r'<(?:!doctype html|html)\b', page[:1000], re.I):
        return finish('unresolved', 'official landing returned a non-HTML response')
    if re.search(r'Just a moment|Checking your browser|Access Denied', page[:1000], re.I):
        return finish('blocked', 'official landing returned an access challenge')
    parser = _ScopedLinks()
    try:
        parser.feed(page)
        targets = _candidates(final_url, page, parser, item)
    except (ValueError, TypeError) as exc:
        return finish('unresolved', type(exc).__name__ + ': official landing download links could not be resolved')
    evidence.update(final_landing_url=final_url, candidate_urls=targets)
    selected = _choose_target(final_url, targets, item, parser, shared_landing)
    if not selected:
        return finish('unresolved', 'no unique explicit primary download matches this archived report')
    if not _trusted_document(selected, item):
        return finish('unresolved', 'explicit primary download points outside trusted original domains')
    evidence['selected_report_url'] = selected
    evidence['selected_link_evidence'] = [
        {'label': link['text'], 'headings': list(link.get('scope', ())), 'method': 'official_explicit_report_link'}
        for link in parser.links if not link['references'] and _document_url(final_url, link['href']) == selected]
    if not evidence['selected_link_evidence']:
        evidence['selected_link_evidence'] = [{'method': 'official_citation_metadata', 'title': parser.metadata.get('citation_title', '')}]
    old_paper, new_paper = _stored_paper_id(item), _paper_id(selected)
    if new_paper and new_paper != old_paper:
        candidate = {
            'vendor': item['vendor'], 'url': landing, 'report_url': selected, 'paper_id': new_paper,
            'source': item.get('source', ''), 'parent_report_id': item.get('id') or report_id(item),
            'landing_title': item.get('title', ''), 'landing_published_at': item.get('published_at', ''),
            'read_status': 'pending_metadata', 'metadata_needs_review': True,
            'primary_link_check_evidence': evidence,
        }
        candidate['id'] = report_id(candidate)
        outcome['new_primary_candidate'] = candidate
        return finish('new_entity', 'new arXiv identity requires independent title/publication-date verification before ingest')
    supplements, ambiguous = _supplements(final_url, page, parser, targets, selected, item)
    if ambiguous:
        return finish('unresolved', 'shared landing has supplementary downloads that cannot be uniquely assigned to this report')
    if any(not _trusted_document(url, item) for url in supplements):
        return finish('unresolved', 'explicit supplementary download points outside trusted original domains')
    old_supplements = item.get('supplementary_urls') or []
    item['supplementary_urls'] = list(dict.fromkeys(old_supplements + supplements))
    changed = canonical_url(selected) != canonical_url(item['report_url']) or item['supplementary_urls'] != old_supplements
    item['report_url'] = selected
    return finish('refreshed' if changed else 'unchanged')
