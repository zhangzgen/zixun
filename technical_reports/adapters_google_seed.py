"""Public, paginated research discovery for Google DeepMind and ByteDance Seed.

Index discovery uses the standard library; undated PDF directories reuse the
system's verified full-text extraction to obtain printed version dates.
Discovery never treats an abstract as a full report or a site modification
time as the report's publication date.
"""
from __future__ import annotations

import datetime as dt
import gzip
import html
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser


# Author-public manuscripts verified against title, author list, and DOI.
# Keep these alongside the official landing page so normal scheduled discovery
# does not depend on ACM's automated-client challenge for an open manuscript.
_GOOGLE_AUTHOR_ORIGINALS = {
    'https://deepmind.google/research/publications/224397/': {
        'paper_id': '2605.19190',
        'author_index': 'https://minsuk.com/publications.html',
        'publisher_url': 'https://dl.acm.org/doi/10.1145/3805689.3812296',
    },
    'https://deepmind.google/research/publications/224297/': {
        'paper_id': '2605.14097',
        'publisher_url': 'https://dl.acm.org/doi/10.1145/3805689.3812253',
    },
}

_GOOGLE_PUBLISHER_ORIGINALS = {
    'https://deepmind.google/research/publications/149262/': {
        'report_url': 'https://dl.acm.org/doi/pdf/10.1145/3805689.3812210',
        'publisher_url': 'https://dl.acm.org/doi/10.1145/3805689.3812210',
        'published_at': '2026-06-25',
        'published_at_raw': '25 June 2026',
    },
}

_GOOGLE_REPORT_TITLES = {
    'gdm-ai-control-roadmap.pdf': 'GDM AI Control Roadmap',
    'three-layers-of-agent-security.pdf': 'The Three Layers of Agent Security: A Framework for Policymakers',
}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self.current = None
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = [dict(attrs).get('href', ''), '']
    def handle_data(self, data):
        if self.current is not None: self.current[1] += data
    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(tuple(self.current)); self.current = None


def _fetch(url, headers=None):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 research-index-monitor', **(headers or {})})
            with urllib.request.urlopen(req, timeout=45) as response: data = response.read()
            if data[:2] == b'\x1f\x8b': data = gzip.decompress(data)
            return data.decode('utf-8', errors='replace')
        except Exception as exc:
            last = exc
            if getattr(exc, 'code', None) == 308 and not url.endswith('/'):
                url += '/'
    raise last


def _text(markup):
    markup = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', markup, flags=re.S | re.I)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', markup))).strip()


def _date(value):
    value = html.unescape(value).strip()
    for fmt in ('%d %B %Y', '%B %d, %Y', '%b %d, %Y', '%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d'):
        try: return dt.datetime.strptime(value[:10] if fmt == '%Y-%m-%d' else value, fmt).date().isoformat()
        except ValueError: pass
    match = re.search(r'\b(\d{4}-\d{2}-\d{2})', value)
    return match[1] if match else ''


def _paper_id(url):
    m = re.search(r'arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})', url)
    return m[1] if m else ''


def _report_links(markup, base):
    p = _Links(); p.feed(markup)
    result = []
    for href, label in p.links:
        u = urllib.parse.urljoin(base, html.unescape(href))
        if '.pdf' in u.lower() or ('arxiv.org/' in u and any(x in u for x in ('/abs/', '/pdf/', '/html/'))) or re.fullmatch(r'(?:Download(?:\s+PDF)?|PDF)', label.strip(), re.I):
            result.append(u)
    return sorted(set(result), key=lambda x: (not ('/pdf/' in x or '.pdf' in x.lower()), x))


def _verify_google_dates(records, start_date, end_date, coverage, errors):
    """Do not treat later conference/index appearance as first publication."""
    def verify(record):
        paper = record.get('paper_id')
        if not paper or record.get('date_source', '').startswith('https://arxiv.org/abs/'):
            return record
        u = 'https://arxiv.org/abs/' + paper
        try:
            abstract = _fetch(u)
            dm = re.search(r'<meta\s+name="citation_date"\s+content="([^"]+)"', abstract)
            exact = _date(dm[1]) if dm else ''
            if not exact:
                coverage.setdefault('unverified_date_candidates', []).append({'title': record['title'], 'url': record['url'], 'paper_id': paper, 'reason': 'No exact first-submission date'}); return None
            record['official_index_date'] = record['published_at']; record['arxiv_first_submitted_at'] = exact
            if exact < record['published_at']:
                record.update(published_at=exact, date_source=u, published_at_raw=dm[1])
            else: record['date_source'] = record['url']
            if not start_date <= record['published_at'] <= end_date:
                coverage['excluded'].append({'title': record['title'], 'url': record['url'], 'paper_id': paper, 'reason': 'First publication outside interval; later official index appearance is not a new report'}); return None
            return record
        except Exception as exc:
            errors.append({'url': u, 'error': str(exc)}); return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [item for item in pool.map(verify, records) if item]


def _seed(start_date, end_date):
    items = {}; errors = []; coverage = {'indexes': [], 'raw_total': {}, 'returned_visible_total': {}, 'pages': {}, 'excluded': [], 'date_timezone': 'Asia/Shanghai'}
    global_seen = set()
    for article_type, locale in ((1, 'US'), (2, 'US'), (2, 'CN')):
        coverage_key = str(article_type) if locale == 'US' else str(article_type) + '_CN'
        offset = 0; seen = set(); pages = 0
        while True:
            params = urllib.parse.urlencode({'article_type': article_type, 'page_token': offset, 'count': 20, 'order_desc': 'true'})
            url = 'https://seed.bytedance.com/api/get_article_list_v2?' + params
            try: data = json.loads(_fetch(url, {'x-tt-locale': locale}))
            except Exception as exc:
                errors.append({'url': url, 'error': str(exc)}); break
            pages += 1; coverage['indexes'].append({'url': url, 'locale': locale}); coverage['raw_total'][coverage_key] = data.get('total')
            for entry in data.get('sub_article_list', []):
                meta = entry.get('ArticleMeta', {}); en = entry.get('ArticleSubContentEn', {}); zh = entry.get('ArticleSubContentZh', {})
                if meta.get('ID') in seen: continue
                seen.add(meta.get('ID'))
                if meta.get('ID') in global_seen: continue
                global_seen.add(meta.get('ID'))
                stamp = meta.get('PublishDate')
                if not stamp:
                    errors.append({'id': meta.get('ID'), 'error': 'Missing report publication date'}); continue
                published = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone(dt.timedelta(hours=8))).date().isoformat()
                if not start_date <= published <= end_date: continue
                active = en if locale == 'US' else zh
                title = active.get('Title') or en.get('Title') or zh.get('Title') or ''
                teams = [v.get('Name') for v in meta.get('WorkingTeam', [])]
                if article_type == 1 and teams == ['AI for Science'] and not re.search(r'agent|language|foundation|multimodal', title, re.I):
                    coverage['excluded'].append({'title': title, 'reason': 'Non-large-model chemical/physical science'}); continue
                if article_type == 2 and re.search(r'Preview Model Release on Arena|科学家|招聘|获奖|合作|Seed STEM', title, re.I):
                    coverage['excluded'].append({'title': title, 'reason': 'Announcement without independent technical method'}); continue
                slug = active.get('TitleKey') or en.get('TitleKey') or zh.get('TitleKey')
                if not slug: continue
                canonical = 'https://seed.bytedance.com/' + ('en/' if locale == 'US' else 'zh/') + ('public_papers/' if article_type == 1 else 'blog/') + slug
                external = [v.get('Link', '') for v in meta.get('ExternalLinks', [])]
                report = next((u for u in external if 'arxiv.org/' in u or '.pdf' in u), '')
                kind = 'model_card' if 'model card' in title.lower() else ('research_paper' if article_type == 1 else 'technical_article')
                record = {'title': title, 'vendor': 'Seed', 'publisher': 'ByteDance Seed', 'source': 'ByteDance Seed 官网', 'published_at': published,
                          'url': canonical, 'report_url': report, 'content_type': kind, 'language': 'en' if locale == 'US' else 'zh', 'paper_id': _paper_id(report),
                          'abstract': active.get('Abstract') or en.get('Abstract') or zh.get('Abstract') or '', 'related_urls': external,
                          'published_at_raw': stamp, 'date_precision': 'day'}
                key = record['paper_id'] or canonical
                # Prefer a paper to its launch blog when both carry the same arXiv identifier.
                if key not in items or article_type == 1: items[key] = record
            if not data.get('has_more'): break
            offset += 20
            if pages >= 100:
                errors.append({'url': url, 'error': 'Pagination safety limit reached'}); break
        coverage['pages'][coverage_key] = pages
        coverage['returned_visible_total'][coverage_key] = len(seen)
    coverage['complete_public_index_scan'] = not errors
    # A launch article which reiterates an already-indexed report is a related
    # carrier, not a second report in the newest-15 list.
    aliases = [('EdgeBench: Measuring', 'EdgeBench: Unveiling'), ('Seed3D 2.0 Released', 'Seed3D 2.0: Advancing')]
    for alias, primary in aliases:
        blog_key = next((k for k, r in items.items() if r['title'].startswith(alias)), None)
        paper_key = next((k for k, r in items.items() if r['title'].startswith(primary)), None)
        if blog_key is not None and paper_key is not None:
            blog = items.pop(blog_key); paper = items[paper_key]
            from .store import report_id
            coverage.setdefault('id_aliases', []).append({
                'old_id': report_id(blog), 'primary_id': report_id(paper),
                'reason': 'Launch article repeats the formally indexed report', 'title': paper['title']})
            paper['related_urls'] = list(dict.fromkeys(paper.get('related_urls', []) + [blog['url']]))
            paper.setdefault('related_publications', []).append({k: blog[k] for k in ('title', 'url', 'published_at', 'content_type')})
            if primary.startswith('EdgeBench:'):
                paper['related_urls'].append('https://edge-bench.org/paper.pdf')
    # Product pages can publish a substantial card without putting that PDF
    # link in the announcement or the public-papers feed.
    card_projects = [('Seed2.1', 'https://seed.bytedance.com/en/seed2_1')]
    coverage['model_card_projects'] = {'checked': [], 'undated_candidates': []}
    for model, project in card_projects:
        try:
            page = _fetch(project); parser = _Links(); parser.feed(page)
            card = next((urllib.parse.urljoin(project, href) for href, label in parser.links
                         if re.fullmatch(r'Model card', label.strip(), re.I) and '.pdf' in href.lower()), '')
            coverage['model_card_projects']['checked'].append({'url': project, 'report_url': card})
            if not card: continue
            original_key = next((key for key, record in items.items()
                                 if record['title'].startswith(model + ' Officially Released')), None)
            if original_key is None:
                coverage['model_card_projects']['undated_candidates'].append({'title': model + ' Model Card', 'url': project,
                                                                             'report_url': card, 'reason': 'No precisely dated corresponding official release in this interval'})
                continue
            release = items.pop(original_key)
            record = {key: value for key, value in release.items() if key not in ('abstract', 'paper_id')}
            record.update(title=model + ' Model Card: Agentic Intelligence for Productivity', url=project,
                          canonical_url=card, report_url=card, content_type='model_card',
                          source='ByteDance Seed 官方模型页', date_precision='inferred_day',
                          date_source=release['url'], date_basis='Dated official release linked from the same model project page',
                          date_note='报告未单独标注首发日；使用同模型官网发布文章的日期，未虚构独立报告首发日期',
                          related_urls=list(dict.fromkeys(release.get('related_urls', []) + [release['url']])),
                          related_publications=[{key: release[key] for key in ('title', 'url', 'published_at', 'content_type')}])
            from .store import report_id
            coverage.setdefault('id_aliases', []).append({'old_id': report_id(release), 'primary_id': report_id(record),
                                                          'reason': 'Official release article is a related carrier of the full model card', 'title': record['title']})
            items[card] = record
        except Exception as exc:
            errors.append({'url': project, 'error': str(exc)})
    transparency = _seed_transparency(start_date, end_date)
    for record in transparency['items']:
        items.setdefault(record['canonical_url'], record)
    errors.extend(transparency['errors']); coverage['transparency_reports'] = transparency['coverage']
    coverage['complete_public_index_scan'] = not errors
    return {'items': sorted(items.values(), key=lambda x: x['published_at'], reverse=True), 'errors': errors, 'coverage': coverage}


def _seed_transparency(start_date, end_date):
    """The document's printed update day, never its storage-folder date."""
    index = 'https://seed.bytedance.com/transparency'
    errors = []; coverage = {'index': index, 'date_policy': 'Printed Last update day in the original PDF', 'documents': []}
    try: markup = _fetch(index)
    except Exception as exc:
        return {'items': [], 'errors': [{'url': index, 'error': str(exc)}], 'coverage': coverage}
    parser = _Links(); parser.feed(markup)
    targets = sorted(set(urllib.parse.urljoin(index, href) for href, label in parser.links
                         if '.pdf' in href.lower() and 'pdf' in label.lower()))
    def qualify(target):
        try:
            # The public directory is undated; its complete PDF supplies the
            # explicit version day and is also retained for exhaustive reading.
            from .acquire import acquire
            from .common import ROOT
            title = urllib.parse.unquote(urllib.parse.urlsplit(target).path.rsplit('/', 1)[-1]).removesuffix('.pdf').strip()
            record = acquire({'title': title, 'vendor': 'Seed', 'publisher': 'ByteDance Nexus AI / ByteDance Seed',
                              'source': 'ByteDance Seed 官方训练数据透明度目录', 'url': target,
                              'canonical_url': target, 'report_url': target, 'content_type': 'training_data_disclosure',
                              'language': 'en', 'related_urls': [index]})
            body = (ROOT / record['fulltext_path']).read_text(encoding='utf-8')
            match = re.search(r'Last\s+update:\s*(\d{1,2}\s+\w+\s+\d{4})', body, re.I)
            published = _date(match[1]) if match else ''
            version = re.search(r'Version\s+of\s+the\s+Summary:\s*([\d.]+)', body, re.I)
            coverage['documents'].append({'title': title, 'url': target, 'published_at': published, 'page_count': record.get('page_count')})
            if not published:
                errors.append({'url': target, 'error': 'No explicit printed Last update date; not inferred from storage-folder name'}); return None
            if not start_date <= published <= end_date: return None
            record.update(published_at=published, version_published_at=published, publication_event='revision',
                          date_source=target, date_precision='day', published_at_raw='Last update: ' + match[1],
                          document_version=version[1] if version else None,
                          date_note='正式报告版本日期；PDF未另行注明更早首发日，不使用模型上市日或存储目录日期')
            return record
        except Exception as exc:
            errors.append({'url': target, 'error': str(exc)}); return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = [record for record in pool.map(qualify, targets) if record]
    coverage['complete_official_transparency_index_scan'] = not errors
    return {'items': items, 'errors': errors, 'coverage': coverage}


def _google(start_date, end_date):
    items = {}; errors = []; coverage = {'indexes': [], 'excluded': [], 'undated_candidates': [], 'date_policy': 'Explicit official day or arXiv first submission; never website lastmod'}
    index = 'https://deepmind.google/research/publications/'
    for page in range(1, 100):
        url = index if page == 1 else index + 'page/' + str(page) + '/'
        try: body = _fetch(url)
        except Exception as exc:
            errors.append({'url': url, 'error': str(exc)}); break
        coverage['indexes'].append(url)
        entries = re.findall(r'<li class=list-group__item>(.*?)</li>', body, re.S)
        dates = []
        for entry in entries:
            p = _Links(); p.feed(entry)
            href = next((u for u, _ in p.links if '/research/publications/' in u), '')
            tm = re.search(r'<span class=list-group__date>\s*(.*?)\s*</span>', entry, re.S)
            title = re.search(r'<span class=list-group__description>(.*?)</span>', entry, re.S)
            if not href or not tm or not title: continue
            published = _date(_text(tm[1])); dates.append(published)
            if not published or not start_date <= published <= end_date: continue
            name = _text(title[1])
            if re.search(r'Economic Policy|Globally Beneficial|Politics of AI Consciousness|From AGI to ASI|superintelligence.*cooperative|Worker Retraining', name, re.I):
                coverage['excluded'].append({'title': name, 'reason': 'Policy/philosophy without model technical method or evaluation'}); continue
            u = urllib.parse.urljoin(index, href)
            items[u] = {'title': name, 'vendor': 'Google', 'publisher': 'Google DeepMind', 'source': 'Google DeepMind 官网', 'published_at': published,
                        'url': u, 'content_type': 'research_paper', 'language': 'en', 'date_precision': 'day'}
        if not entries or (dates and all(d and d < start_date for d in dates)): break
    def enrich(record):
        try:
            publisher_original = _GOOGLE_PUBLISHER_ORIGINALS.get(record['url'])
            if publisher_original:
                record.update(official_index_date=record['published_at'],
                              date_source=publisher_original['publisher_url'],
                              published_at=publisher_original['published_at'],
                              published_at_raw=publisher_original['published_at_raw'],
                              report_url=publisher_original['report_url'],
                              canonical_url=publisher_original['report_url'],
                              related_urls=[publisher_original['publisher_url']],
                              discovery_warning='Publisher is open access; HTTP automation may require normal browser download')
                return record
            author_original = _GOOGLE_AUTHOR_ORIGINALS.get(record['url'])
            if author_original:
                paper = author_original['paper_id']
                record.update(report_url='https://arxiv.org/pdf/' + paper, paper_id=paper,
                              related_urls=[u for k, u in author_original.items() if k != 'paper_id'])
                return record
            markup = _fetch(record['url']); links = _report_links(markup, record['url'])
            if links: record.update(report_url=links[0], paper_id=_paper_id(links[0]), related_urls=links)
            else:
                parser = _Links(); parser.feed(markup)
                publication = next((urllib.parse.urljoin(record['url'], href) for href, label in parser.links if 'View publication' in label), '')
                if publication:
                    if 'ojs.aaai.org/' in publication:
                        publisher = _fetch(publication); downloads = _report_links(publisher, publication)
                        if downloads: publication = downloads[0].replace('/article/view/', '/article/download/')
                    if 'dl.acm.org/doi/' in publication and '/doi/pdf/' not in publication and '/doi/epdf/' not in publication:
                        publication = publication.replace('/doi/', '/doi/pdf/')
                    record.update(report_url=publication, related_urls=[publication], discovery_warning='External publisher link requires complete-original verification')
                else: record['discovery_warning'] = 'Official abstract page has no detected full report link'
        except Exception as exc: errors.append({'url': record['url'], 'error': str(exc)})
        return record
    with ThreadPoolExecutor(max_workers=6) as pool:
        enriched = list(pool.map(enrich, items.values()))
    items = {r.get('paper_id') or r['url']: r for r in enriched}
    # DeepMind's paginated blog includes papers/cards absent from the publications index.
    for page in range(1, 100):
        url = 'https://deepmind.google/blog/' if page == 1 else 'https://deepmind.google/blog/page/' + str(page) + '/'
        try: body = _fetch(url)
        except Exception as exc:
            errors.append({'url': url, 'error': str(exc)}); break
        coverage['indexes'].append(url)
        months = []
        candidates = {}
        for block in re.findall(r'<article\b.*?</article>', body, re.S):
            tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
            title = re.search(r'<h3[^>]*card__title[^>]*>(.*?)</h3>', block, re.S)
            if not tm or not title: continue
            try: month = dt.datetime.strptime(tm[1], '%B %Y').strftime('%Y-%m')
            except ValueError: continue
            months.append(month)
            if month < start_date[:7] or month > end_date[:7]: continue
            p = _Links(); p.feed(block)
            href = next((u for u, _ in p.links if ('/blog/' in u or 'blog.google/' in u) and '/page/' not in u), '')
            name = _text(title[1])
            if not href or not re.search(r'Gemini|Gemma|Veo|Lyria|Genie|agent|evaluation|safety|SynthID|sign language', name, re.I): continue
            if re.search(r'^Investing in multi-agent AI safety research$', name, re.I):
                coverage['excluded'].append({'title': name, 'url': urllib.parse.urljoin(url, href),
                                            'reason': 'Funding call without an independent method, experiment, or technical report; cited 2025 research is not a new report'})
                continue
            u = urllib.parse.urljoin(url, href); u = urllib.parse.urlunsplit(urllib.parse.urlsplit(u)._replace(query='', fragment=''))
            candidates[u] = name
        for u, name in candidates.items():
            try: markup = _fetch(u)
            except Exception as exc:
                errors.append({'url': u, 'error': str(exc)}); continue
            reports = _report_links(markup, u)
            parser = _Links(); parser.feed(markup)
            primary_reports = [urllib.parse.urljoin(u, href) for href, label in parser.links
                               if re.search(r'technical report|model card|system card|safety report|impact report|white paper|our paper|read the paper|roadmap|three layers', label, re.I)
                               and ('.pdf' in href or 'arxiv.org/' in href)]
            # References cited by a new blog are not themselves newly published reports.
            if not primary_reports and not reports:
                coverage['excluded'].append({'title': name, 'url': u, 'reason': 'Blog without formal report/card link'}); continue
            dm = re.search(r'"datePublished"\s*:\s*"([^"]+)"', markup)
            if not dm: dm = re.search(r'<time[^>]*datetime=["\']([^"\']+)', markup)
            published = _date(dm[1]) if dm else ''
            if not published:
                coverage['undated_candidates'].append({'title': name, 'url': u, 'report_urls': reports}); continue
            if not start_date <= published <= end_date: continue
            for report in (list(dict.fromkeys(primary_reports)) or ['']):
                key = (_paper_id(report) or report) if report else u
                if key in items: continue
                report_name = _GOOGLE_REPORT_TITLES.get(urllib.parse.urlsplit(report).path.rsplit('/', 1)[-1], name)
                items[key] = {'title': report_name, 'carrier_title': name, 'vendor': 'Google', 'publisher': 'Google DeepMind', 'source': 'Google DeepMind 官网', 'published_at': published,
                              'url': u, 'canonical_url': report or u, 'report_url': report, 'paper_id': _paper_id(report), 'related_urls': reports,
                              'content_type': ('model_card' if re.search(r'card|safety', report, re.I) else 'technical_report') if report else 'technical_article', 'language': 'en', 'date_precision': 'day'}
        if not months or all(m < start_date[:7] for m in months): break
    research = _google_research(start_date, end_date)
    for record in research['items']:
        key = record.get('paper_id') or record['url']
        if key not in items: items[key] = record
    errors.extend(research['errors']); coverage['google_research_supplement'] = research['coverage']
    verified = _verify_google_dates(list(items.values()), start_date, end_date, coverage, errors)
    items = {record.get('paper_id') or record.get('canonical_url') or record['url']: record for record in verified}
    cards = _google_model_cards(start_date, end_date)
    for record in cards['items']:
        items.setdefault(record['canonical_url'], record)
    errors.extend(cards['errors']); coverage['model_cards'] = cards['coverage']
    # The 3.5 launch post repeats the Flash card's headline benchmarks and
    # release information; retain its source link without occupying two slots.
    launch_key = next((key for key, record in items.items()
                       if record['title'] == 'Gemini 3.5: frontier intelligence with action'), None)
    card_key = next((key for key, record in items.items()
                     if record['title'] == 'Gemini 3.5 Flash Model Card'), None)
    if launch_key is not None and card_key is not None:
        launch = items.pop(launch_key); card = items[card_key]
        card['related_urls'] = list(dict.fromkeys(card.get('related_urls', []) + [launch['url']]))
        card.setdefault('related_publications', []).append({key: launch[key] for key in ('title', 'url', 'published_at', 'content_type')})
        from .store import report_id
        coverage.setdefault('id_aliases', []).append({'old_id': report_id(launch), 'primary_id': report_id(card),
                                                      'reason': 'Launch blog repeats the full model card headline capability results', 'title': card['title']})
    for record in items.values():
        _canonical_google_original(record, coverage)
        author = _GOOGLE_AUTHOR_ORIGINALS.get(record['url'])
        if author and record.get('paper_id'):
            record['title'] = record['title'].replace('forText-to-Image', 'for Text-to-Image')
            from .store import report_id
            old = dict(record); old.pop('paper_id', None)
            old['report_url'] = author['publisher_url'].replace('/doi/', '/doi/pdf/' if '3812296' in author['publisher_url'] else '/doi/epdf/')
            old['canonical_url'] = old['report_url']
            coverage.setdefault('id_aliases', []).append({'old_id': report_id(old), 'primary_id': report_id(record),
                                                          'reason': 'Verified public author manuscript for same DOI and official publication', 'title': record['title']})
    coverage['complete_public_index_scan'] = not errors
    return {'items': sorted(items.values(), key=lambda x: x['published_at'], reverse=True), 'errors': errors, 'coverage': coverage}


def _canonical_google_original(record, coverage):
    """Preserve one identity for a publisher PDF and its official landing page."""
    if record.get('report_url') and not record.get('paper_id') and not record.get('canonical_url'):
        from .store import report_id
        old_id = report_id(record)
        record['canonical_url'] = record['report_url']
        primary_id = report_id(record)
        if old_id != primary_id:
            coverage.setdefault('id_aliases', []).append({
                'old_id': old_id, 'primary_id': primary_id,
                'reason': 'Official publication landing page resolves to the same publisher full original',
                'title': record['title']})


def _google_research(start_date, end_date):
    """Scan year indexes, then verify first arXiv submission dates individually.

    A conference year is not a release date. Undated records remain in coverage
    diagnostics until a precise official date can be established.
    """
    items = []; errors = []; coverage = {'indexes': [], 'candidate_count': 0, 'undated_candidates': [], 'excluded': []}
    candidates = {}; pattern = r'LLM|language model|multimodal|generative|Gemini|Gemma|reasoning|agent|text.to.image|text.to.video|world model|transformer|diffusion|speech|reinforcement learning|vision.language|AI safety|alignment|benchmark'
    for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
        initial = 'https://research.google/pubs/?category=' + str(year)
        try: markup = _fetch(initial)
        except Exception as exc:
            errors.append({'url': initial, 'error': str(exc)}); continue
        last = re.search(r'data-max-pages="(\d+)"', markup); page_count = int(last[1]) if last else 1
        urls = [initial + '&page=' + str(n) for n in range(1, min(page_count, 100) + 1)]
        def get_page(url):
            try: return url, markup if url.endswith('page=1') else _fetch(url)
            except Exception as exc:
                errors.append({'url': url, 'error': str(exc)}); return url, ''
        with ThreadPoolExecutor(max_workers=6) as pool:
            pages = list(pool.map(get_page, urls))
        for url, body in pages:
            coverage['indexes'].append(url)
            for m in re.finditer(r'<a class="row-card__heading glue-link" href=([^ >]+)[^>]*>(.*?)</a>', body, re.S):
                title = _text(m[2]); u = m[1].strip('"\'')
                if re.search(pattern, title, re.I): candidates[u] = title
    coverage['candidate_count'] = len(candidates)
    def qualify(pair):
        u, title = pair
        try:
            markup = _fetch(u); links = _report_links(markup, u)
            paper_url = next((link for link in links if _paper_id(link)), '')
            paper_id = _paper_id(paper_url)
            if not paper_id:
                coverage['undated_candidates'].append({'title': title, 'url': u, 'report_urls': links, 'reason': 'Official page only provides publication year; no exact initial date'}); return None
            # arXiv IDs encode the first submission month, making old references cheap to exclude.
            month = '20' + paper_id[:2] + '-' + paper_id[2:4]
            if month < start_date[:7] or month > end_date[:7]:
                coverage['excluded'].append({'title': title, 'url': u, 'paper_id': paper_id, 'reason': 'Initial arXiv submission outside interval'}); return None
            abstract_url = 'https://arxiv.org/abs/' + paper_id
            abstract = _fetch(abstract_url)
            dm = re.search(r'<meta\s+name="citation_date"\s+content="([^"]+)"', abstract)
            if not dm:
                dm = re.search(r'\[Submitted on\s+(\d+\s+\w+\s+\d{4})', _text(abstract))
            published = _date(dm[1]) if dm else ''
            if not published:
                coverage['undated_candidates'].append({'title': title, 'url': u, 'paper_id': paper_id, 'reason': 'Unable to verify exact first-submission date'}); return None
            if not start_date <= published <= end_date: return None
            if re.search(r'Psychological Perspective|Humanlikeness|Enterprise Generative AI Systems|Employee Support|People Operations|Toward a Theory of Value in AI Alignment', title, re.I):
                coverage['excluded'].append({'title': title, 'url': u, 'reason': 'Workplace/product commentary without large-model technical method'}); return None
            return {'title': title, 'vendor': 'Google', 'publisher': 'Google Research', 'source': 'Google Research 官网', 'published_at': published,
                    'published_at_raw': dm[1], 'date_precision': 'day', 'date_source': abstract_url,
                    'url': u, 'report_url': 'https://arxiv.org/pdf/' + paper_id, 'paper_id': paper_id, 'related_urls': links,
                    'content_type': 'research_paper', 'language': 'en'}
        except Exception as exc:
            errors.append({'url': u, 'error': str(exc)}); return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        items = [record for record in pool.map(qualify, candidates.items()) if record]
    coverage['complete_year_index_scan'] = not any('/pubs/?' in e['url'] for e in errors)
    coverage['all_candidates_date_resolved'] = not coverage['undated_candidates'] and not errors
    return {'items': items, 'errors': errors, 'coverage': coverage}


def _google_model_cards(start_date, end_date):
    """Discover card publications/revisions from the official fixed directory.

    A row's explicit Updated date is a version event, not proof of first
    publication. Detail pages may expose a separate Published day.
    """
    index = 'https://deepmind.google/models/model-cards/'
    errors = []; coverage = {'index': index, 'candidates': [], 'excluded': [],
                            'date_policy': 'Official indexed publication/update event; preserve independently stated first publication'}
    try: markup = _fetch(index)
    except Exception as exc:
        return {'items': [], 'errors': [{'url': index, 'error': str(exc)}], 'coverage': coverage}
    candidates = []
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', markup, re.S):
        title = re.search(r'<th\b[^>]*scope=(?:"row"|row)[^>]*>(.*?)</th>', row, re.S)
        date_text = re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)
        links = _Links(); links.feed(row)
        target = next((urllib.parse.urljoin(index, href) for href, label in links.links if 'View model card' in label), '')
        raw = _text(date_text[1]) if date_text else ''
        event = _date(re.sub(r'^(?:Updated|Published)\s+', '', raw))
        if not title or not target or not event:
            if title: coverage['excluded'].append({'title': _text(title[1]), 'url': target, 'reason': 'No precise official publication/update day'})
            continue
        if not start_date <= event <= end_date: continue
        candidates.append((_text(title[1]), event, raw, target))
    coverage['candidate_count'] = len(candidates)
    def qualify(candidate):
        title, event, raw, target = candidate
        target = urllib.parse.urlunsplit(urllib.parse.urlsplit(target)._replace(query='', fragment=''))
        record = {'title': title + ' Model Card', 'vendor': 'Google', 'publisher': 'Google DeepMind',
                  'source': 'Google DeepMind 官方模型卡目录', 'published_at': event,
                  'version_published_at': event, 'publication_event': 'revision' if raw.startswith('Updated') else 'initial',
                  'date_source': index, 'published_at_raw': raw, 'date_precision': 'day',
                  'url': target, 'canonical_url': target, 'report_url': target,
                  'content_type': 'model_card', 'language': 'en', 'related_urls': [index]}
        try:
            if '.pdf' not in urllib.parse.urlsplit(target).path.lower():
                page = _fetch(target); parser = _Links(); parser.feed(page)
                primary = next((urllib.parse.urljoin(target, href) for href, label in parser.links
                                if re.search(r'View PDF version|Download PDF', label, re.I) and '.pdf' in href.lower()), '')
                if primary: record.update(report_url=primary, canonical_url=primary)
                stated = re.search(r'<span[^>]*cover__text--eyebrow[^>]*>\s*Published\s+([^<]+)', page, re.I)
                first = _date(_text(stated[1])) if stated else ''
                if first:
                    record.update(first_published_at=first, first_publication_source=target)
                    if first == event: record['publication_event'] = 'initial'
                else:
                    record['date_note'] = 'Official card update day is known; first document publication day is not independently stated'
            coverage['candidates'].append({'title': record['title'], 'url': target,
                                            'report_url': record['report_url'], 'published_at': event,
                                            'first_published_at': record.get('first_published_at')})
            return record
        except Exception as exc:
            errors.append({'url': target, 'error': str(exc)}); return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        items = [item for item in pool.map(qualify, candidates) if item]
    coverage['complete_official_card_index_scan'] = not errors
    return {'items': items, 'errors': errors, 'coverage': coverage}


def discover(vendor, start_date, end_date):
    """Return items/errors/coverage for a closed ISO-date interval."""
    dt.date.fromisoformat(start_date); dt.date.fromisoformat(end_date)
    if start_date > end_date: raise ValueError('start_date must precede end_date')
    if vendor == 'Seed': return _seed(start_date, end_date)
    if vendor == 'Google': return _google(start_date, end_date)
    raise ValueError('vendor must be Google or Seed')
