"""Official RSS/Atom, HTML directories and document revisions; no model API."""
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from technical_reports.common import TZ, digest, fetch_bytes, strip_html


class Element:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, attrs or {}, parent
        self.children = []

    def text(self, clean=True):
        if clean and self.tag in ('script', 'style', 'nav', 'header', 'footer', 'noscript'):
            return ''
        return ' '.join(c.text(clean) if isinstance(c, Element) else c for c in self.children)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()


class Tree(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Element()
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs), self.current)
        self.current.children.append(element)
        if tag not in self.VOID:
            self.current = element

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, data):
        self.current.children.append(data)


def normalized_url(url):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query)
    query = [(k, v) for k, v in query if not k.startswith('utm_') and k not in ('ref', 'source')]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/'), urllib.parse.urlencode(query), ''))


def timestamp(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec='seconds')


def date_in_text(text):
    patterns = [r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+20\d{2}\b',
                r'\b20\d{2}[-/]\d{2}[-/]\d{2}\b']
    for pattern in patterns:
        found = re.search(pattern, text, re.I)
        if found:
            val = found.group()
            for fmt in ('%b %d, %Y', '%B %d, %Y', '%b %d %Y', '%B %d %Y', '%Y-%m-%d', '%Y/%m/%d'):
                try:
                    return datetime.strptime(val, fmt).replace(tzinfo=TZ).isoformat(timespec='seconds')
                except ValueError:
                    pass
    return None


def fetch(url):
    raw, final, content_type = fetch_bytes(url, timeout=30)
    return raw.decode('utf-8', 'replace'), final, content_type


def feed_entries(raw):
    root = ET.fromstring(raw)
    if root.tag.split('}')[-1] not in ('rss', 'feed', 'RDF'):
        raise ValueError('Source did not return an RSS/Atom feed')
    entries = []
    for node in root.iter():
        if node.tag.split('}')[-1] not in ('item', 'entry'):
            continue
        fields, url, body = {}, '', ''
        for child in node:
            tag = child.tag.split('}')[-1]
            fields[tag] = child.text or ''
            if tag == 'link' and child.attrib.get('rel', 'alternate') == 'alternate':
                url = child.attrib.get('href') or child.text or url
            if tag in ('content', 'encoded'):
                body = child.text or body
            if tag == 'description' and not body:
                body = child.text or ''
        if not url or not fields.get('title'):
            continue
        published = timestamp(fields.get('published') or fields.get('pubDate'))
        updated = timestamp(fields.get('updated')) or published
        entries.append({'url': normalized_url(url), 'title': html.unescape(fields['title']),
                        'published_at': published, 'updated_at': updated,
                        'event_at': updated or published, 'embedded_body': body,
                        'version_hash': digest(body), 'date_basis': 'feed_updated' if not published else 'feed_published'})
    if not entries:
        raise ValueError('Feed contains no usable entries; do not treat as no updates')
    return entries


def article_metadata(tree):
    metadata = {}
    for node in tree.root.walk():
        attrs = node.attrs
        if node.tag == 'meta':
            key = attrs.get('property') or attrs.get('name')
            if key:
                metadata[key] = attrs.get('content', '')
        if node.tag == 'script' and attrs.get('type') == 'application/ld+json':
            try:
                value = json.loads(node.text(clean=False))
            except (ValueError, TypeError):
                continue
            def visit(v):
                if isinstance(v, dict):
                    types = v.get('@type', [])
                    types = [types] if isinstance(types, str) else types
                    if any(t in ('Article', 'BlogPosting', 'TechArticle', 'NewsArticle') for t in types):
                        for key in ('datePublished', 'dateModified', 'headline'):
                            if v.get(key):
                                metadata[key] = v[key]
                    for child in v.values():
                        visit(child)
                elif isinstance(v, list):
                    for child in v:
                        visit(child)
            visit(value)
    return metadata


def main_text(tree):
    nodes = list(tree.root.walk())
    # Prefer an explicitly marked article body, then semantic content regions.
    exact = [n for n in nodes if n.attrs.get('itemprop') == 'articleBody' or
             re.search(r'(^|\s)(entry-content|post-content|blog-content|markdown-body|prose)(\s|$)', n.attrs.get('class', ''))]
    articles = [n for n in nodes if n.tag == 'article']
    mains = [n for n in nodes if n.tag == 'main']
    selected = max(exact or articles or mains or [tree.root], key=lambda n: len(n.text()))
    return re.sub(r'\s+', ' ', html.unescape(selected.text())).strip()


def directory_entries(raw, base_url, source):
    tree = Tree(raw)
    entries = {}
    usable_links = 0
    for node in tree.root.walk():
        if node.tag != 'a' or not node.attrs.get('href'):
            continue
        url = normalized_url(urllib.parse.urljoin(base_url, node.attrs['href']))
        parts = urllib.parse.urlsplit(url)
        path = source['path']
        if parts.netloc != urllib.parse.urlsplit(base_url).netloc or not parts.path.startswith(path):
            continue
        if not parts.path[len(path):] or parts.query or '/page/' in parts.path or any(x in parts.path for x in source.get('exclude', [])):
            continue
        if re.fullmatch(r'20\d{2}(?:/\d{1,2})?', parts.path[len(path):].strip('/')):
            continue
        if parts.path[len(path):].strip('/') in ('feed', 'rss', 'rss.xml'):
            continue
        usable_links += 1
        context = node
        # A whole clickable card often contains its own date. Climbing past it
        # would borrow a neighbouring article's date from the surrounding grid.
        for _ in range(7):
            if date_in_text(context.text(clean=False)):
                break
            if context.parent is None:
                break
            cl = context.attrs.get('class', '').lower()
            if context.tag in ('article', 'li') or 'post' in cl.split() or any(k in cl for k in ('w-dyn-item', 'postcard', 'post-card', 'blog-card', 'blog-list-item')):
                break
            related = {normalized_url(urllib.parse.urljoin(base_url, n.attrs['href'])) for n in context.parent.walk()
                       if n.tag == 'a' and n.attrs.get('href') and urllib.parse.urlsplit(urllib.parse.urljoin(base_url, n.attrs['href'])).path.startswith(path)}
            if len(related) > 1 or len(context.parent.text(clean=False)) > 4000:
                break
            context = context.parent
        text = context.text(clean=False)
        if source.get('title_filter') and not re.search(source['title_filter'], node.text() + ' ' + url, re.I):
            continue
        date = next((timestamp(n.attrs['datetime']) for n in context.walk()
                     if n.tag == 'time' and n.attrs.get('datetime')), None) or date_in_text(text)
        row = {'url': url, 'title': node.text().strip() or url.rsplit('/', 1)[-1],
               'published_at': date, 'event_at': date, 'date_basis': 'directory_date', 'version_hash': ''}
        if url not in entries or (not entries[url]['published_at'] and date):
            entries[url] = row
    # LlamaIndex embeds a complete article list, including entries absent from
    # its rendered first page. Read observed data, never guess a private API.
    for node in tree.root.walk():
        if node.tag != 'script' or node.attrs.get('type') != 'application/json':
            continue
        try:
            records = json.loads(node.text(clean=False))
        except ValueError:
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not record.get('title'):
                continue
            slug = record.get('slug')
            slug = slug.get('current') if isinstance(slug, dict) else slug
            if not slug:
                continue
            url = normalized_url(urllib.parse.urljoin(base_url, source['path'] + slug))
            if url not in entries and record.get('publishedDate'):
                dt = timestamp(record.get('publishedDate'))
                entries[url] = {'url': url, 'title': record['title'], 'published_at': dt,
                                'event_at': dt, 'date_basis': 'directory_structured_date', 'version_hash': ''}
    if not entries and not (source.get('title_filter') and usable_links):
        raise ValueError('No article links parsed; source may have changed layout')
    return list(entries.values())


def acquire_entry(entry):
    raw, final, _ = fetch(entry['url'])
    tree = Tree(raw)
    metadata = article_metadata(tree)
    h1 = next((n.text().strip() for n in tree.root.walk() if n.tag == 'h1'), '')
    body = main_text(tree)
    if len(body) < 120:
        raise ValueError('Full article body missing or too short')
    published = timestamp(metadata.get('datePublished') or metadata.get('article:published_time'))
    updated = timestamp(metadata.get('dateModified') or metadata.get('article:modified_time'))
    return {**entry, 'title': metadata.get('headline') or h1 or entry['title'], 'resolved_url': final,
            'published_at': published or entry.get('published_at'),
            'event_at': entry.get('event_at') or published or date_in_text(body[:1500]),
            'updated_at': updated or entry.get('updated_at'), 'fulltext': body,
            'content_hash': digest(body), 'date_basis': 'article_metadata' if published else entry.get('date_basis')}


def discover(source, previous, cutoff, upper):
    raw, final, _ = fetch(source.get('collect_url') or source['url'])
    if source['kind'] in ('rss', 'atom'):
        entries = feed_entries(raw)
    elif source['kind'] == 'snapshot':
        tree = Tree(raw)
        body = main_text(tree)
        if len(body) < 120:
            raise ValueError('Snapshot body missing or too short')
        # First visit establishes a baseline and never backfills a historical page.
        old_hash = previous.get('snapshot_hash')
        row = {'url': normalized_url(source['url']), 'title': source['name'], 'event_at': upper,
               'published_at': None, 'date_basis': 'observed_revision', 'fulltext': body,
               'content_hash': digest(body), 'version_hash': digest(body)}
        changed = bool(old_hash and old_hash != row['content_hash'])
        return ([row] if changed else []), {'snapshot_hash': digest(body), 'final_url': final, 'seen': previous.get('seen', {})}, []
    else:
        entries = directory_entries(raw, final, source)
    seen = dict(previous.get('seen', {}))
    candidates, failures = [], []
    undated_baseline = []
    if source['kind'] == 'atom':
        dates = [r['event_at'] for r in entries if r.get('event_at')]
        since_last = previous.get('checked_at') or cutoff
        if len(entries) >= 10 and dates and min(dates) >= since_last:
            failures.append({'url': source['url'], 'error': 'Atom window may be truncated; verify GitHub release pagination before declaring complete coverage'})
    def inspect(row):
        uid = digest(row['url'])[:24]
        old = seen.get(uid)
        dt = row.get('event_at')
        if not dt and not previous.get('baseline_completed') and source['kind'] == 'index':
            # The user explicitly requested no history. Undated existing posts
            # become a disclosed initial baseline, never "published today".
            return {**row, 'initial_undated_baseline': True}, None
        # A new URL since monitoring began can be a delayed/backdated post. An
        # existing directory URL is not repeatedly fetched as historical content.
        if old and source['kind'] == 'index':
            return row, None
        if dt and dt > upper:
            return row, None
        if dt and dt < cutoff:
            return row, None
        if old and old.get('version_hash') == row.get('version_hash'):
            return row, None
        if source['kind'] == 'atom':
            # Atom content is the publisher's complete release note, not a snippet.
            body = strip_html(row.get('embedded_body', ''))
            if not body.strip():
                raise ValueError('Release entry contains no release notes')
            row = {**row, 'fulltext': body, 'content_hash': digest(body)}
        else:
            row = acquire_entry(row)
        dt = row.get('event_at')
        if not dt:
            # A newly appearing undated article is a discovery event, not a
            # verified first-publication timestamp. Keep published_at null.
            row['event_at'] = dt = upper
            row['date_basis'] = 'first_observed_undated'
        if dt > upper or dt < cutoff:
            return row, None
        row['publication_event'] = 'revision' if old else ('release_log_update' if row['date_basis'] == 'feed_updated' else 'new')
        return row, True

    def safe(row):
        try:
            return inspect(row)
        except Exception as exc:
            return row, {'url': row['url'], 'error': str(exc)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(safe, entries))
    for row, eligible in outcomes:
        uid = digest(row['url'])[:24]
        if isinstance(eligible, dict):
            failures.append(eligible)
            # Leave failed identities un-seen so the next run can retry them.
            continue
        if row.get('event_at') and row['event_at'] > upper:
            continue
        seen[uid] = {'url': row['url'], 'version_hash': row.get('version_hash', ''), 'event_at': row.get('event_at')}
        if row.get('initial_undated_baseline'):
            undated_baseline.append(row['url'])
        if eligible is True:
            candidates.append(row)
    return candidates, {'seen': seen, 'final_url': final, 'initial_undated_baseline': undated_baseline,
                        'baseline_completed': not failures}, failures
