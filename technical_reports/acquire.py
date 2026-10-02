"""Download entire originals and prepare exhaustive reading chunks for the Agent."""
import json
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from html.parser import HTMLParser

from .common import ROOT, digest, fetch_bytes, HTMLText, strip_html


def _valid_pdf_text(text):
    if not isinstance(text, str):
        return False
    try:
        text.encode('utf-8', 'strict')
    except UnicodeError:
        return False
    return ('\ufffd' not in text and not re.search(r'\(cid:\d+\)', text)
            and not re.search(r'[\x00-\x08\x0b\x0e-\x1f]', text))


def acquisition_timestamp():
    # Separate acquisitions can finish in the same second. Keep precision
    # here without changing the general archive's human-readable timestamps.
    return datetime.now().astimezone().isoformat(timespec='microseconds')


def _certify_blank_page(page, page_number, text):
    """A missing extraction is never blank evidence; inspect PDF page objects."""
    label = text.strip()
    if label not in ('', str(page_number)):
        return None
    objects = page.objects
    # pdfplumber/pdfminer traverses form XObjects. Reject images and any drawn
    # geometry, including white backgrounds, conservatively rather than guessing.
    counts = {name: len(entries) for name, entries in objects.items()}
    if any(count for name, count in counts.items() if name != 'char'):
        return None
    chars = objects.get('char', [])
    actual = ''.join(char.get('text', '') for char in chars).strip()
    if actual not in ('', str(page_number)) or not _valid_pdf_text(actual):
        return None
    # No unrecognized character objects may hide behind an empty extracted body.
    if any(not char.get('text') for char in chars):
        return None
    if chars:
        height = getattr(page, 'height', 0)
        if not height or not all(isinstance(char.get('top'), (int, float))
                                 and isinstance(char.get('bottom'), (int, float))
                                 and (char['bottom'] <= height * 0.1 or char['top'] >= height * 0.9)
                                 for char in chars):
            return None
    return {'page': page_number, 'method': 'pdfplumber_object_inspection',
            'visible_text': actual, 'object_counts': counts,
            'reason': 'no images or drawing objects; text is empty or physical page number in page margin only'}


def _extract_pdf(path):
    """Run in the bundled runtime; validate every page and retain provenance."""
    from pypdf import PdfReader
    import pdfplumber
    path = Path(path)
    spacing, spacing_pages = None, []
    spacing_path = path.with_suffix('.spacing.json')
    if spacing_path.exists():
        proof = json.loads(spacing_path.read_text(encoding='utf-8'))
        try:
            datetime.fromisoformat(proof.get('read_at', '').replace('Z', '+00:00'))
            verified_time = True
        except (ValueError, TypeError):
            verified_time = False
        if (proof.get('pdf_sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
                and proof.get('reviewer') == 'agent' and proof.get('scope') == 'document_word_spacing'
                and proof.get('replacement') == 'U+0001 to U+0020' and verified_time
                and isinstance(proof.get('notes'), str) and proof['notes'].strip()):
            spacing = proof
    visual = {}
    sidecar = path.with_suffix('.visual.json')
    if sidecar.exists():
        record = json.loads(sidecar.read_text(encoding='utf-8'))
        if record.get('pdf_sha256') == hashlib.sha256(path.read_bytes()).hexdigest():
            for page in record.get('pages', []):
                try:
                    datetime.fromisoformat(page.get('read_at', '').replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    continue
                if (page.get('reviewer') == 'agent' and page.get('scope') == 'entire_page'
                        and isinstance(page.get('page'), int) and isinstance(page.get('text'), str)
                        and _valid_pdf_text(page['text'].replace('\x01', ' ') if spacing else page['text'])
                        and isinstance(page.get('notes'), str) and page['notes'].strip()):
                    visual[page['page']] = page
    primary, reader_error, glyph_proof, glyph_audit = None, None, None, None
    try:
        primary = PdfReader(path)
    except Exception as exc:
        reader_error = type(exc).__name__ + ': ' + str(exc)
    if primary is not None:
        from .pdf_glyphs import apply_verified_glyphs, diagnose_glyph_mappings
        glyph_proof = apply_verified_glyphs(primary, path, ROOT)
        # An ASCII/control fallback from a TeX math font can look like legal
        # Unicode. Inspect drawn source bytes before claiming full-page text.
        if hasattr(primary, 'get_object'):
            glyph_audit = diagnose_glyph_mappings(primary)
    pages, evidence, blank_pages, failures = [], [], [], []
    with pdfplumber.open(path) as alternate:
        count = len(alternate.pages)
        if primary is not None and len(primary.pages) != count:
            reader_error = 'extractors disagree on page count'
            primary = None
            glyph_proof = None
        for index, backup_page in enumerate(alternate.pages):
            page_number = index + 1
            font_issues = [issue for issue in (glyph_audit or {}).get('issues', [])
                           if issue['page'] == page_number]
            if font_issues and page_number not in visual:
                failures.append({'page': page_number, 'error': 'mathematical glyph mapping needs verified visual recovery',
                                 'glyph_issues': font_issues})
            text, cause, method = '', reader_error, 'pypdf'
            if primary is not None:
                try:
                    text = primary.pages[index].extract_text() or ''
                    if not _valid_pdf_text(text):
                        cause = 'pypdf produced invalid Unicode or unmapped glyphs'
                    elif len(text.strip()) < 30:
                        cause = 'pypdf produced too little text; validate against page objects'
                except Exception as exc:
                    cause = type(exc).__name__ + ': ' + str(exc)
            if primary is None or cause:
                method = 'pdfplumber'
                try:
                    text = backup_page.extract_text() or ''
                except Exception as exc:
                    if page_number in visual:
                        text = visual[page_number]['text']
                        method = 'agent_visual_recovery'
                    else:
                        failures.append({'page': page_number, 'error': 'both extractors failed: ' + str(exc)})
                        pages.append('')
                        continue
            # A readable text layer can still omit raster tables and charts.
            # A SHA-bound entire-page Agent transcription includes those details
            # and must also be honored when the automated text layer is valid.
            if page_number in visual:
                text = visual[page_number]['text']
                method = 'agent_visual_recovery'
            if spacing and '\x01' in text:
                spacing_pages.append({'page': page_number, 'replacement_count': text.count('\x01')})
                text = text.replace('\x01', ' ')
            if not _valid_pdf_text(text):
                if page_number in visual:
                    text = visual[page_number]['text']
                    method = 'agent_visual_recovery'
                else:
                    failures.append({'page': page_number, 'error': 'invalid Unicode or unmapped glyphs after fallback'})
            elif len(text.strip()) < 30:
                proof = _certify_blank_page(backup_page, page_number, text)
                if proof is None:
                    if page_number in visual:
                        text = visual[page_number]['text']
                        method = 'agent_visual_recovery'
                    else:
                        failures.append({'page': page_number, 'error': 'short extraction is not a certified blank page; visual/OCR reading required'})
                else:
                    blank_pages.append(proof)
            if method == 'pypdf' and glyph_proof:
                font_proofs = [font for font in glyph_proof['fonts'] if page_number in font['pages']]
                if font_proofs:
                    method = 'pypdf_verified_font_glyphs'
            if method == 'agent_visual_recovery':
                # A labeled note preserves the difference between source text
                # and the Agent's visual description of graphics/decorations.
                text += '\n[Agent visual reading note] ' + visual[page_number]['notes']
            pages.append(text)
            evidence.append({'page': page_number, 'extractor': method, 'char_count': len(text),
                             'fallback_reason': cause,
                             'visual_review': visual.get(page_number) if method == 'agent_visual_recovery' else None})
    if failures:
        raise RuntimeError('PDF pages need visual/OCR recovery: ' + json.dumps(failures, ensure_ascii=False))
    result = {'text': '\n\n'.join('[Page ' + str(i + 1) + ']\n' + text for i, text in enumerate(pages)),
              'page_count': count, 'page_extraction': evidence, 'certified_blank_pages': blank_pages}
    if spacing:
        result['normalized_spacing'] = {'replacement': 'U+0001 to U+0020', 'pages': spacing_pages,
            'replacement_count': sum(page['replacement_count'] for page in spacing_pages),
            'sidecar': str(spacing_path), 'verification': spacing}
    if glyph_proof:
        result['verified_font_glyphs'] = glyph_proof
    if glyph_audit:
        result['glyph_mapping_audit'] = dict(glyph_audit,
            issues_resolved_by_entire_page_visual=[issue for issue in glyph_audit['issues'] if issue['page'] in visual],
            complete=all(issue['page'] in visual for issue in glyph_audit['issues']))
    return result


def pdf_text(data, key, with_evidence=False):
    folder = ROOT / '.technical_reports' / 'pdf'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.pdf')
    path.write_bytes(data)
    immutable = folder / (key + '-' + hashlib.sha256(data).hexdigest()[:16] + '.pdf')
    if not immutable.exists():
        immutable.write_bytes(data)
    bundled = Path('/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3')
    runtime = os.getenv('REPORT_PYTHON') or (str(bundled) if bundled.exists() else sys.executable)
    program = """import json,sys
sys.path.insert(0, sys.argv[2])
from technical_reports.acquire import _extract_pdf
print(json.dumps(_extract_pdf(sys.argv[1]), ensure_ascii=False))
"""
    result = subprocess.run([runtime, '-c', program, str(path), str(ROOT)], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError('PDF text extraction failed; original preserved for visual/OCR recovery: ' + result.stderr[-1800:])
    extracted = json.loads(result.stdout)
    proof = {name: value for name, value in extracted.items() if name != 'text'}
    proof['pdf_sha256'] = hashlib.sha256(data).hexdigest()
    proof['pdf_path'] = str(immutable.relative_to(ROOT))
    path.with_suffix('.extraction.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    if with_evidence:
        return extracted['text'], extracted['page_count'], proof
    return extracted['text'], extracted['page_count']


class _DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.parts = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href, self.parts = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.links.append((self.href, ' '.join(''.join(self.parts).split())))
            self.href, self.parts = None, []


def _publication_landing(url):
    parsed = urlparse(url)
    return ((parsed.hostname == 'research.google' and parsed.path.startswith('/pubs/'))
            or (parsed.hostname == 'deepmind.google' and parsed.path.startswith('/research/publications/'))
            or (parsed.hostname == 'ojs.aaai.org' and re.search(r'/article/(?:view|download)/\d+(?:/\d+)?/?$', parsed.path))
            or (parsed.hostname == 'dl.acm.org' and parsed.path.startswith(('/doi/abs/', '/doi/epdf/', '/doi/pdf/'))))


def _expects_pdf(url):
    parsed = urlparse(url)
    return (parsed.path.lower().endswith('.pdf')
            or (parsed.hostname == 'arxiv.org' and parsed.path.startswith('/pdf/'))
            or (parsed.hostname == 'journals.plos.org' and '/article/file' in parsed.path and 'type=printable' in parsed.query))


def _publication_download(url, markup):
    parser = _DownloadLinks()
    parser.feed(markup)
    accepted = {'download', 'download paper', 'download pdf', 'view pdf', 'pdf'}
    candidates = list(dict.fromkeys(urljoin(url, href) for href, label in parser.links
                                    if label.lower() in accepted))
    # Multiple manuscript links require a source-specific resolver; never pick a
    # random PDF that could be a reference, supplement or a different paper.
    if len(candidates) != 1:
        return None
    return candidates[0]


def _original_key(url):
    parsed = urlsplit(url)
    path = parsed.path.rstrip('/')
    if parsed.hostname in ('arxiv.org', 'www.arxiv.org'):
        path = re.sub(r'^/(?:abs|html)/', '/pdf/', path)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ''))


def acquire(item, _seen=None, _supplement=False):
    item = dict(item)
    # Previously acquired self-owned originals remain part of the report even
    # when a discovery adapter only repeats the main download URL.
    if not _supplement:
        for original in (item.get('original_documents') or [])[1:]:
            url = original.get('url')
            if not url:
                continue
            field = 'supplementary_urls' if original.get('role') == 'supplementary' else 'own_original_urls'
            item[field] = list(dict.fromkeys((item.get(field) or []) + [url]))
    item.pop('extraction_evidence', None)
    original_url = item.get('report_url') or item['url']
    seen = set(_seen or ())
    if original_url in seen:
        raise RuntimeError('primary original resolution loop; complete report unavailable')
    seen.add(original_url)
    raw, final_url, content_type = fetch_bytes(original_url)
    page_count = None
    if raw.startswith(b'%PDF') or 'application/pdf' in content_type:
        text, page_count, evidence = pdf_text(raw, digest(original_url)[:16], with_evidence=True)
        item['extraction_evidence'] = evidence
        kind = 'pdf'
    else:
        value = raw.decode('utf-8', 'replace')
        if 'html' in content_type or '<html' in value[:1000].lower():
            if re.search(r'arxiv\.org/abs/', final_url):
                item['report_url'] = re.sub(r'/abs/', '/pdf/', final_url)
                return acquire(item, seen, _supplement)
            if not _supplement and item.get('vendor') in ('Anthropic', 'OpenAI'):
                from . import adapters_west
                resolver = getattr(adapters_west, 'primary_original', None)
                if resolver:
                    resolved = resolver(final_url, value)
                    for field in ('supplementary_urls', 'own_original_urls', 'related_urls'):
                        item[field] = list(dict.fromkeys(item.get(field, []) + resolved.get(field, [])))
                    item['primary_resolution_evidence'] = resolved.get('evidence', [])
                    target = resolved.get('report_url')
                    if target and target != original_url:
                        item['report_url'] = target
                        return acquire(item, seen, _supplement)
            if _publication_landing(final_url):
                target = _publication_download(final_url, value)
                if target and target not in seen:
                    item['report_url'] = target
                    item.setdefault('primary_resolution_evidence', []).append(
                        {'url': final_url, 'download_url': target, 'basis': 'explicit publication download label'})
                    return acquire(item, seen, _supplement)
                raise RuntimeError('publication page contains only abstract/metadata; no unambiguous primary full-text download')
            if _expects_pdf(original_url):
                raise RuntimeError('expected a complete PDF original but the source returned HTML')
            embedded = None
            if item.get('vendor') == 'GLM':
                from .adapters_china import extract_fulltext
                embedded = extract_fulltext(final_url, value)
            text = embedded or strip_html(value)
            kind = 'html'
        else:
            text, kind = value, 'text'
    # Text-mode reads use universal newlines. Hash and chunk the same bytes of
    # text that the reading Agent will see, including recovered PDF carriage returns.
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    minimum = 1 if _supplement and kind == 'pdf' else 1000
    if len(text.strip()) < minimum or re.search(r'Just a moment|Checking your browser|Access Denied', text[:1000], re.I):
        raise RuntimeError('complete original unavailable; page is too short or blocked')
    primary_hash = digest(text)
    primary = {'url': original_url, 'final_url': final_url, 'role': 'primary',
               'kind': kind, 'page_count': page_count, 'char_count': len(text),
               'content_hash': primary_hash}
    if item.get('extraction_evidence'):
        primary['extraction_evidence'] = item['extraction_evidence']
    documents = [primary]
    item['primary_char_count'] = len(text)
    item['primary_content_hash'] = primary_hash
    supplementary_originals = []
    additional_originals = []
    if not _supplement:
        # Only declared own attachments are part of the report. related_urls
        # can be ordinary citations and must never be read as report appendices.
        handled_urls = {_original_key(original_url), _original_key(final_url)}
        handled_hashes = {primary_hash}
        own_documents = [('supplementary', url) for url in item.get('supplementary_urls') or []]
        own_documents.extend(('additional_primary', url) for url in item.get('own_original_urls') or [])
        for role, supplement_url in own_documents:
            if urlsplit(supplement_url).scheme not in ('http', 'https'):
                raise RuntimeError('supplementary original must use HTTP(S)')
            if _original_key(supplement_url) in handled_urls:
                continue
            try:
                supplement = acquire({'url': supplement_url, 'vendor': item.get('vendor')}, seen, True)
            except Exception as exc:
                raise RuntimeError('complete supplementary original unavailable: ' + supplement_url + ': ' + str(exc)) from exc
            handled_urls.add(_original_key(supplement_url))
            handled_urls.add(_original_key(supplement['original_url']))
            if supplement['content_hash'] in handled_hashes:
                continue
            handled_hashes.add(supplement['content_hash'])
            original = (ROOT / supplement['fulltext_path']).read_text(encoding='utf-8')
            label = 'Full supplementary original' if role == 'supplementary' else 'Additional full original'
            text += '\n\n[' + label + ']\nSource: ' + supplement_url + '\n\n' + original
            proof = {'url': supplement_url, 'final_url': supplement['original_url'],
                     'role': role, 'kind': supplement['original_kind'],
                     'page_count': supplement.get('page_count'), 'char_count': len(original),
                     'content_hash': supplement['content_hash'],
                     'fulltext_path': supplement['fulltext_path']}
            if supplement.get('extraction_evidence'):
                proof['extraction_evidence'] = supplement['extraction_evidence']
            documents.append(proof)
            (supplementary_originals if role == 'supplementary' else additional_originals).append(proof)
    item['original_documents'] = documents
    item['supplementary_originals'] = supplementary_originals
    item['additional_originals'] = additional_originals
    item['total_pdf_page_count'] = sum(document.get('page_count') or 0 for document in documents)
    key = digest(item['url'])[:16] + '-' + digest(text)[:12]
    folder = ROOT / 'technical_reports' / 'fulltext'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.txt')
    path.write_text(text, encoding='utf-8')
    item.update(fulltext_path=str(path.relative_to(ROOT)), content_hash=digest(text),
                original_url=final_url, original_kind=kind, char_count=len(text), page_count=page_count,
                read_status='pending_full_text', original_unavailable=False, error=None,
                original_acquired_at=acquisition_timestamp())
    item.pop('attachment_errors', None)
    item.pop('original_quality_error', None)
    chunk_folder = ROOT / '.technical_reports' / 'reading' / key
    chunk_folder.mkdir(parents=True, exist_ok=True)
    chunks = []
    for index, start in enumerate(range(0, len(text), 14000), 1):
        chunk = text[start:start + 14000]
        chunk_path = chunk_folder / f'{index:04d}.txt'
        chunk_path.write_text(chunk, encoding='utf-8')
        chunks.append({'path': str(chunk_path.relative_to(ROOT)), 'start': start, 'end': start + len(chunk), 'sha256': digest(chunk)})
    item['reading_chunks'] = chunks
    return item
