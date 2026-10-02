import json
import hashlib
import sys
import types
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from technical_reports import acquire as module
from technical_reports.common import digest


class Page:
    height = 800

    def __init__(self, text, objects=None):
        self.text = text
        self.objects = objects or {'char': [{'text': str(c), 'top': 775, 'bottom': 785} for c in text]}

    def extract_text(self):
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def fake_extractors(primary, backup, audited=False):
    class Document:
        pages = backup
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
    def reader(path):
        if isinstance(primary, Exception):
            raise primary
        reader = types.SimpleNamespace(pages=primary)
        if audited:
            reader.get_object = lambda obj: obj
        return reader
    return patch.dict(sys.modules, {
        'pypdf': types.SimpleNamespace(PdfReader=reader),
        'pdfplumber': types.SimpleNamespace(open=lambda path: Document()),
    })


class PDFRecoveryTests(unittest.TestCase):
    def test_surrogate_falls_back_without_lossy_unicode_replacement(self):
        original = 'Complete methods and results. ' * 10
        with fake_extractors([Page(original + '\ud835')], [Page(original + '𝑚')]):
            result = module._extract_pdf('fixture.pdf')
        self.assertIn(original + '𝑚', result['text'])
        self.assertEqual(result['page_extraction'][0]['extractor'], 'pdfplumber')
        result['text'].encode('utf-8', 'strict')

    def test_reader_failure_uses_all_backup_pages(self):
        originals = ['Methods ' * 10, 'Appendix limitations ' * 10]
        with fake_extractors(ValueError('broken font parser'), [Page(t) for t in originals]):
            result = module._extract_pdf('fixture.pdf')
        self.assertEqual(result['page_count'], 2)
        for original in originals:
            self.assertIn(original, result['text'])

    def test_only_a_margin_page_number_with_no_graphics_is_blank(self):
        page = Page('45')
        self.assertEqual(module._certify_blank_page(page, 45, '45')['visible_text'], '45')
        page.objects['image'] = [{'name': 'scanned-manuscript'}]
        self.assertIsNone(module._certify_blank_page(page, 45, '45'))
        centre = Page('45', {'char': [{'text': '4', 'top': 300, 'bottom': 310},
                                    {'text': '5', 'top': 300, 'bottom': 310}]})
        self.assertIsNone(module._certify_blank_page(centre, 45, '45'))
        self.assertIsNone(module._certify_blank_page(Page('Appendix'), 45, 'Appendix'))

    def test_scanned_page_never_passes_as_blank(self):
        scan = Page('', {'image': [{'name': 'full page image'}]})
        with fake_extractors([Page('')], [scan]):
            with self.assertRaisesRegex(RuntimeError, 'visual/OCR'):
                module._extract_pdf('fixture.pdf')

    def test_unrecoverable_glyphs_are_reported_instead_of_stripped(self):
        bad = 'Result contains an unmapped glyph. ' * 10 + '\ufffd'
        with fake_extractors([Page(bad)], [Page(bad)]):
            with self.assertRaisesRegex(RuntimeError, 'invalid Unicode'):
                module._extract_pdf('fixture.pdf')

    def test_pdf_provenance_hashes_original_bytes(self):
        extracted = {'text': 'Complete report body. ' * 100, 'page_count': 1,
                     'page_extraction': [], 'certified_blank_pages': []}
        result = types.SimpleNamespace(returncode=0, stdout=json.dumps(extracted), stderr='')
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module.subprocess, 'run', return_value=result):
            text, pages, proof = module.pdf_text(b'%PDF-fixture', 'fixture', with_evidence=True)
            self.assertEqual(len(proof['pdf_sha256']), 64)
            self.assertEqual(pages, 1)
            self.assertTrue((Path(folder) / '.technical_reports/pdf/fixture.extraction.json').exists())

    def test_visual_recovery_is_bound_to_exact_pdf_bytes(self):
        scan = Page('', {'image': [{'name': 'cover-logo'}]})
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'original.pdf'
            path.write_bytes(b'%PDF-specific-original')
            proof = {'pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'pages': [
                {'page': 1, 'reviewer': 'agent', 'scope': 'entire_page',
                 'read_at': '2026-10-02T23:00:00+08:00', 'text': 'DeepMind',
                 'notes': 'Reviewed entire cover: logo only; no body, table or footnote.'}]}
            path.with_suffix('.visual.json').write_text(json.dumps(proof))
            with fake_extractors([Page('')], [scan]):
                result = module._extract_pdf(path)
            self.assertEqual(result['page_extraction'][0]['extractor'], 'agent_visual_recovery')
            self.assertIn('[Agent visual reading note]', result['text'])
            proof['pdf_sha256'] = 'wrong hash'
            path.with_suffix('.visual.json').write_text(json.dumps(proof))
            with fake_extractors([Page('')], [scan]):
                with self.assertRaisesRegex(RuntimeError, 'visual/OCR'):
                    module._extract_pdf(path)

    def test_visual_transcription_retains_image_tables_even_with_valid_text_layer(self):
        prose = 'Readable model-card explanation and chart caption. ' * 20
        image_table = 'Image benchmark table: method A 81.2%; method B 76.4%.'
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'model-card.pdf'
            path.write_bytes(b'%PDF-image-benchmark-card')
            proof = {'pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'pages': [
                {'page': 1, 'reviewer': 'agent', 'scope': 'entire_page',
                 'read_at': '2026-10-03T01:00:00+08:00', 'text': prose + image_table,
                 'notes': 'Entire page reviewed, including the raster evaluation table.'}]}
            path.with_suffix('.visual.json').write_text(json.dumps(proof))
            with fake_extractors([Page(prose)], [Page(prose)]):
                result = module._extract_pdf(path)
            self.assertIn(image_table, result['text'])
            self.assertIn(prose, result['text'])
            self.assertEqual(result['page_extraction'][0]['extractor'], 'agent_visual_recovery')

    def test_spacing_glyph_repair_requires_exact_pdf_visual_verification(self):
        text = 'A\x01complete\x01document with font-encoded word spacing. ' * 10
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'font-space.pdf'
            path.write_bytes(b'%PDF-specific-space-mapping')
            with fake_extractors([Page(text)], [Page(text)]):
                with self.assertRaisesRegex(RuntimeError, 'glyphs'):
                    module._extract_pdf(path)
            proof = {'pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'reviewer': 'agent', 'read_at': '2026-10-03T01:00:00+08:00',
                     'scope': 'document_word_spacing', 'replacement': 'U+0001 to U+0020',
                     'notes': 'Verified word gaps visually and the shared document font mapping.'}
            path.with_suffix('.spacing.json').write_text(json.dumps(proof))
            with fake_extractors([Page(text)], [Page(text)]):
                result = module._extract_pdf(path)
            self.assertIn(text.replace('\x01', ' '), result['text'])
            self.assertEqual(result['normalized_spacing']['replacement_count'], 20)
            self.assertNotIn('\x01', result['text'])
            proof['pdf_sha256'] = 'other PDF bytes'
            path.with_suffix('.spacing.json').write_text(json.dumps(proof))
            with fake_extractors([Page(text)], [Page(text)]):
                with self.assertRaisesRegex(RuntimeError, 'glyphs'):
                    module._extract_pdf(path)

    def test_legal_ascii_math_mapping_error_requires_visual_recovery(self):
        body = 'Readable original body containing an ASCII X in an equation. ' * 40
        audit = {'method': 'read_only_used_embedded_math_glyph_audit', 'scanned_fonts': [],
                 'issues': [{'page': 1, 'code_hex': '58', 'expected_unicode': '∑',
                             'actual_unicode': 'X'}], 'complete': False}
        with fake_extractors([Page(body)], [Page(body)], audited=True), patch('technical_reports.pdf_glyphs.diagnose_glyph_mappings', return_value=audit):
            with self.assertRaisesRegex(RuntimeError, 'mathematical glyph mapping'):
                module._extract_pdf('fixture.pdf')

    def test_entire_page_visual_recovery_can_resolve_math_mapping_audit(self):
        body = 'Complete original with the summation equation ∑ explicitly transcribed. ' * 40
        audit = {'method': 'read_only_used_embedded_math_glyph_audit', 'scanned_fonts': [],
                 'issues': [{'page': 1, 'code_hex': '58', 'expected_unicode': '∑',
                             'actual_unicode': 'X'}], 'complete': False}
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'original.pdf'
            path.write_bytes(b'%PDF-exact-original-with-summation')
            proof = {'pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'pages': [
                {'page': 1, 'reviewer': 'agent', 'scope': 'entire_page',
                 'read_at': '2026-10-03T01:00:00+08:00', 'text': body,
                 'notes': 'Reviewed every original equation, body paragraph and footnote.'}]}
            path.with_suffix('.visual.json').write_text(json.dumps(proof))
            with fake_extractors([Page(body)], [Page(body)], audited=True), patch('technical_reports.pdf_glyphs.diagnose_glyph_mappings', return_value=audit):
                result = module._extract_pdf(path)
            self.assertTrue(result['glyph_mapping_audit']['complete'])
            self.assertEqual(len(result['glyph_mapping_audit']['issues_resolved_by_entire_page_visual']), 1)
            self.assertEqual(result['page_extraction'][0]['extractor'], 'agent_visual_recovery')


class PublicationOriginalTests(unittest.TestCase):
    def test_long_google_metadata_shell_is_rejected(self):
        html = '<html><h1>Paper</h1><h2>Abstract</h2>' + ('Navigation and abstract. ' * 1000) + '</html>'
        item = {'url': 'https://research.google/pubs/example/', 'vendor': 'Google'}
        with patch.object(module, 'fetch_bytes', return_value=(html.encode(), item['url'], 'text/html')):
            with self.assertRaisesRegex(RuntimeError, 'abstract/metadata'):
                module.acquire(item)

    def test_printable_download_url_does_not_require_pdf_suffix(self):
        url = 'https://deepmind.google/research/publications/123/'
        target = 'https://journals.plos.org/plosone/article/file?id=10.1/example&type=printable'
        html = '<a href="' + target + '"><span>Download</span></a>'
        self.assertEqual(module._publication_download(url, html), target)
        self.assertIsNone(module._publication_download(url, '<a href="other.pdf">References</a>'))

    def test_ambiguous_downloads_do_not_choose_first_pdf(self):
        html = '<a href="first.pdf">Download</a><a href="second.pdf">Download</a>'
        self.assertIsNone(module._publication_download('https://research.google/pubs/paper/', html))

    def test_html_error_shell_at_pdf_url_cannot_count_as_original(self):
        html = '<html>' + ('Long website navigation without the PDF. ' * 100) + '</html>'
        item = {'url': 'https://example.org/report/', 'report_url': 'https://example.org/report.pdf', 'vendor': 'Google'}
        with patch.object(module, 'fetch_bytes', return_value=(html.encode(), item['report_url'], 'text/html')):
            with self.assertRaisesRegex(RuntimeError, 'source returned HTML'):
                module.acquire(item)


class CompleteAttachmentTests(unittest.TestCase):
    def test_successful_retry_clears_stale_unavailable_flags(self):
        original = 'Complete original with methods and appendix. ' * 100
        url = 'https://example.org/report.txt'
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', return_value=(original.encode(), url, 'text/plain')), patch.object(module, 'acquisition_timestamp', return_value='2026-10-03T02:00:00.000123+08:00'):
            result = module.acquire({'url': url, 'vendor': 'Google',
                                     'original_unavailable': True, 'error': 'previous download failed',
                                     'original_quality_error': 'previous unmapped glyphs',
                                     'original_acquired_at': '2026-10-02T00:00:00+08:00',
                                     'attachment_errors': [{'url': url, 'error': 'previous failure'}]})
        self.assertFalse(result['original_unavailable'])
        self.assertIsNone(result['error'])
        self.assertNotIn('attachment_errors', result)
        self.assertNotIn('original_quality_error', result)
        self.assertEqual(result['original_acquired_at'], '2026-10-03T02:00:00.000123+08:00')
        self.assertEqual(result['read_status'], 'pending_full_text')

    def test_acquisition_timestamp_preserves_microseconds_and_timezone(self):
        value = module.acquisition_timestamp()
        self.assertRegex(value, r'T\d{2}:\d{2}:\d{2}\.\d{6}[+-]\d{2}:\d{2}$')
        self.assertIsNotNone(datetime.fromisoformat(value).utcoffset())

    def test_retry_retains_self_owned_originals_but_ignores_citations(self):
        source = 'https://example.org/full.txt'
        appendix = 'https://example.org/appendix.txt'
        companion = 'https://example.org/companion.txt'
        originals = {source: 'Main complete original. ' * 80,
                     appendix: 'Full supplementary appendix. ' * 70,
                     companion: 'Independent companion manuscript. ' * 60}
        def fetch(url):
            return originals[url].encode(), url, 'text/plain'
        item = {'url': source, 'vendor': 'Google', 'original_documents': [
            {'url': source, 'role': 'primary'}, {'url': appendix, 'role': 'supplementary'},
            {'url': companion, 'role': 'additional_primary'}],
            'related_urls': ['https://example.org/cited-paper.pdf']}
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', side_effect=fetch):
            result = module.acquire(item)
            self.assertEqual([doc['url'] for doc in result['original_documents']], list(originals))
            self.assertEqual(result['supplementary_urls'], [appendix])
            self.assertEqual(result['own_original_urls'], [companion])
            self.assertIn(originals[companion], (Path(folder) / result['fulltext_path']).read_text())

    def test_carriage_returns_have_the_same_hash_and_offsets_after_disk_read(self):
        text = ('Methods\r\nExperiments\rAppendix\n' * 80)
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', return_value=(text.encode(), 'https://example.org/full.txt', 'text/plain')):
            result = module.acquire({'url': 'https://example.org/full.txt', 'vendor': 'Google'})
            stored = (Path(folder) / result['fulltext_path']).read_text()
            self.assertEqual(result['content_hash'], digest(stored))
            self.assertEqual(result['char_count'], len(stored))
            self.assertEqual(result['reading_chunks'][-1]['end'], len(stored))
            self.assertNotIn('\r', stored)

    def test_supplement_and_named_own_manuscripts_are_read_but_references_are_not(self):
        source = 'https://example.org/report/'
        appendix = 'https://example.org/appendix.pdf'
        accompanying = 'https://example.org/accompanying.pdf'
        primary = 'Primary complete report. ' * 100
        texts = {b'%PDF-appendix': 'All appendix tables and notes. ' * 60,
                 b'%PDF-accompanying': 'Entire accompanying manuscript. ' * 50}
        def fetch(url):
            if url == source:
                return ('<html><article>' + primary + '</article></html>').encode(), source, 'text/html'
            if url == appendix:
                return b'%PDF-appendix', url, 'application/pdf'
            if url == accompanying:
                return b'%PDF-accompanying', url, 'application/pdf'
            raise AssertionError('Unexpected reference or unowned attachment: ' + url)
        def extract(raw, key, with_evidence):
            return texts[raw], 2, {'pdf_sha256': hashlib.sha256(raw).hexdigest(), 'page_count': 2}
        item = {'url': source, 'vendor': 'Kimi', 'supplementary_urls': [appendix, appendix + '#tables'],
                'own_original_urls': [source, accompanying], 'related_urls': ['https://example.org/reference.pdf']}
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', side_effect=fetch), patch.object(module, 'pdf_text', side_effect=extract):
            result = module.acquire(item)
            text = (Path(folder) / result['fulltext_path']).read_text()
            for original in texts.values():
                self.assertIn(original, text)
                self.assertEqual(text.count(original), 1)
            self.assertEqual(result['total_pdf_page_count'], 4)
            self.assertEqual(len(result['original_documents']), 3)
            self.assertEqual(result['supplementary_originals'][0]['role'], 'supplementary')
            self.assertEqual(result['additional_originals'][0]['role'], 'additional_primary')
            self.assertEqual(result['content_hash'], digest(text))
            chunks = result['reading_chunks']
            self.assertEqual(chunks[0]['start'], 0)
            self.assertEqual(chunks[-1]['end'], len(text))
            self.assertIn(texts[b'%PDF-accompanying'], ''.join((Path(folder) / c['path']).read_text() for c in chunks))

    def test_annex_missing_stops_complete_acquisition(self):
        source = 'https://example.org/report/'
        markup = '<html>' + ('Primary report body. ' * 100) + '</html>'
        def fetch(url):
            if url == source:
                return markup.encode(), url, 'text/html'
            raise OSError('Original annex temporarily unavailable')
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', side_effect=fetch):
            with self.assertRaisesRegex(RuntimeError, 'supplementary original unavailable'):
                module.acquire({'url': source, 'vendor': 'Google', 'supplementary_urls': ['https://example.org/missing.pdf']})
            self.assertFalse((Path(folder) / 'technical_reports/fulltext').exists())

    def test_attachment_html_does_not_resolve_its_cited_paper_as_its_own_body(self):
        source, appendix = 'https://example.org/report/', 'https://example.org/appendix/'
        body = 'Complete manuscript or independent appendix. ' * 80
        def resolve(url, markup):
            if url != source:
                raise AssertionError('A verified annex must not follow its reference PDFs')
            return {'supplementary_urls': [appendix]}
        def fetch(url):
            return ('<html>' + body + '</html>').encode(), url, 'text/html'
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', side_effect=fetch), patch('technical_reports.adapters_west.primary_original', side_effect=resolve):
            result = module.acquire({'url': source, 'vendor': 'Anthropic'})
            # An identical text annex is deduplicated by its verified content hash.
            self.assertEqual(len(result['original_documents']), 1)

    def test_short_verified_pdf_annex_is_preserved_in_full(self):
        source, appendix = 'https://example.org/report/', 'https://example.org/appendix.pdf'
        def fetch(url):
            if url == source:
                return ('<html>' + 'Report body. ' * 150 + '</html>').encode(), url, 'text/html'
            return b'%PDF-annex', url, 'application/pdf'
        recovered = '[Page 1]\nAppendix: table of all evaluated models and licenses.'
        with TemporaryDirectory() as folder, patch.object(module, 'ROOT', Path(folder)), patch.object(module, 'fetch_bytes', side_effect=fetch), patch.object(module, 'pdf_text', return_value=(recovered, 1, {'page_count': 1})):
            result = module.acquire({'url': source, 'vendor': 'Google', 'supplementary_urls': [appendix]})
            self.assertIn(recovered, (Path(folder) / result['fulltext_path']).read_text())


if __name__ == '__main__':
    unittest.main()
