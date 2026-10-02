import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from technical_reports.pdf_glyphs import apply_verified_glyphs, diagnose_glyph_mappings, glyph_unicode


def stream(data):
    value = DecodedStreamObject()
    value.set_data(data)
    return value


def dictionary(**values):
    return DictionaryObject({NameObject('/' + name): value for name, value in values.items()})


class VerifiedGlyphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / 'report.pdf'
        self.pdf.write_bytes(b'%PDF-exact-test-original')
        image = self.root / 'equations.png'
        image.write_bytes(b'exact-visually-reviewed-page-image')
        font_bytes = b'dup 0 /parenleftbig put\ndup 13 /vextenddouble put\ndup 88 /summationdisplay put\ncurrentfile eexec\n'
        self.old_cmap = b'1 begincodespacerange\n<00> <FF>\nendcodespacerange\n3 beginbfchar\n<00> <0000>\n<0D> <000D>\n<58> <0058>\nendbfchar\nendcmap\n'
        self.font = dictionary(Subtype=NameObject('/Type1'), BaseFont=NameObject('/SUBSET+CMEX10'),
            FontDescriptor=dictionary(FontFile=stream(font_bytes)), ToUnicode=stream(self.old_cmap))
        page = dictionary(Resources=dictionary(Font=dictionary(F1=self.font)))
        self.reader = SimpleNamespace(pages=[page])
        self.record = {'pdf_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
            'reviewer': 'agent', 'scope': 'font_glyph_mapping', 'read_at': '2026-10-03T01:00:00+08:00',
            'notes': 'Exact embedded Encoding and original equation symbols visually reviewed.',
            'fonts': [{'base_font': '/SUBSET+CMEX10', 'font_file_sha256': hashlib.sha256(font_bytes).hexdigest(),
                'to_unicode_sha256': hashlib.sha256(self.old_cmap).hexdigest(), 'mappings': [
                    {'code_hex': '00', 'glyph_name': 'parenleftbig', 'unicode': '(', 'encoding_evidence': 'dup 0 /parenleftbig put'},
                    {'code_hex': '0D', 'glyph_name': 'vextenddouble', 'unicode': '∥', 'encoding_evidence': 'dup 13 /vextenddouble put'},
                    {'code_hex': '58', 'glyph_name': 'summationdisplay', 'unicode': '∑', 'encoding_evidence': 'dup 88 /summationdisplay put'}]}],
            'visual_checked_pages': [{'page': 1, 'reviewer': 'agent', 'scope': 'glyph_verification',
                'read_at': '2026-10-03T01:00:00+08:00', 'image_path': 'equations.png',
                'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                'notes': 'Reviewed parentheses, norm bars and summation in the original equations.'}]}

    def apply(self, record=None):
        self.pdf.with_suffix('.glyphs.json').write_text(json.dumps(record or self.record), encoding='utf-8')
        return apply_verified_glyphs(self.reader, self.pdf, self.root)

    def test_exact_font_mapping_repairs_controls_and_legal_ascii_in_memory(self):
        original = self.pdf.read_bytes()
        result = self.apply()
        cmap = self.font['/ToUnicode'].get_data()
        self.assertIn(b'<00> <0028>', cmap)
        self.assertIn(b'<0D> <2225>', cmap)
        self.assertIn(b'<58> <2211>', cmap)
        self.assertEqual(self.pdf.read_bytes(), original)
        self.assertEqual(result['fonts'][0]['pages'], [1])
        self.assertEqual(result['fonts'][0]['visual_checked_pages'], [1])
        self.assertEqual(result['fonts'][0]['original_to_unicode_sha256'], hashlib.sha256(self.old_cmap).hexdigest())

    def test_byte_bindings_encoding_and_semantics_fail_before_cmap_mutation(self):
        for field, replacement in (
            ('pdf_sha256', 'different-pdf'), ('font_file_sha256', 'different-font'),
            ('to_unicode_sha256', 'different-cmap'), ('image_sha256', 'different-image'),
            ('glyph_name', 'parenrightbig'), ('unicode', ' '),
            ('encoding_evidence', 'dup 0 /parenrightbig put')):
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                if field == 'pdf_sha256':
                    record[field] = replacement
                elif field in ('font_file_sha256', 'to_unicode_sha256'):
                    record['fonts'][0][field] = replacement
                elif field == 'image_sha256':
                    record['visual_checked_pages'][0][field] = replacement
                else:
                    record['fonts'][0]['mappings'][0][field] = replacement
                with self.assertRaises(RuntimeError):
                    self.apply(record)
                self.assertEqual(self.font['/ToUnicode'].get_data(), self.old_cmap)

    def test_one_invalid_font_prevents_partial_repairs(self):
        record = copy.deepcopy(self.record)
        second = copy.deepcopy(record['fonts'][0])
        second['base_font'] = '/UNKNOWN+CMEX10'
        record['fonts'].append(second)
        with self.assertRaisesRegex(RuntimeError, 'font identity'):
            self.apply(record)
        self.assertEqual(self.font['/ToUnicode'].get_data(), self.old_cmap)

    def test_visual_proof_must_be_for_a_page_containing_the_font(self):
        self.reader.pages.append(dictionary(Resources=dictionary()))
        self.record['visual_checked_pages'][0]['page'] = 2
        with self.assertRaisesRegex(RuntimeError, 'representative page'):
            self.apply()

    def test_duplicate_font_record_is_rejected_before_partial_mutation(self):
        self.record['fonts'].append(copy.deepcopy(self.record['fonts'][0]))
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            self.apply()
        self.assertEqual(self.font['/ToUnicode'].get_data(), self.old_cmap)

    def test_unknown_or_control_semantics_never_receive_a_replacement(self):
        for name in ('unverifiedglyph', 'uni0000', 'uniFFFD', 'uD800', 'a24',
                     'bracehtipdownleft', 'uniED17'):
            with self.subTest(name=name):
                self.assertIsNone(glyph_unicode(name))
        self.assertEqual(glyph_unicode('parenleftbig'), '(')
        self.assertEqual(glyph_unicode('summationdisplay'), '∑')
        self.assertEqual(glyph_unicode('slashbig'), '/')

    def visual_fixture(self, gallery_content=b'BT /Glyph 100 Tf 150 150 Td <04> Tj ET'):
        font_bytes = b'dup 4 /a4 put\ncurrentfile eexec\n'
        self.font[NameObject('/BaseFont')] = NameObject('/SUBSET+LCIRCLE10')
        self.font['/FontDescriptor'][NameObject('/FontFile')] = stream(font_bytes)
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject('/Resources')] = dictionary(Font=dictionary(Glyph=self.font))
        page[NameObject('/Contents')] = stream(gallery_content)
        gallery = self.root / 'exact-font-gallery.pdf'
        writer.write(gallery)
        mapping = {'code_hex': '04', 'glyph_name': 'a4', 'unicode': '╮',
            'encoding_evidence': 'dup 4 /a4 put', 'visual_glyph_evidence': {
                'reviewer': 'agent', 'scope': 'isolated_embedded_glyph',
                'read_at': '2026-10-03T01:00:00+08:00',
                'source_pdf_sha256': self.record['pdf_sha256'],
                'font_file_sha256': hashlib.sha256(font_bytes).hexdigest(),
                'gallery_pdf_path': gallery.name,
                'gallery_pdf_sha256': hashlib.sha256(gallery.read_bytes()).hexdigest(),
                'gallery_page': 1, 'image_path': 'equations.png',
                'image_sha256': self.record['visual_checked_pages'][0]['image_sha256'],
                'notes': 'A rounded top-right corner, confirmed from the isolated exact font glyph.'}}
        self.record['fonts'] = [{'base_font': '/SUBSET+LCIRCLE10',
            'font_file_sha256': hashlib.sha256(font_bytes).hexdigest(),
            'to_unicode_sha256': hashlib.sha256(self.old_cmap).hexdigest(), 'mappings': [mapping]}]

    def test_arbitrary_glyph_name_requires_exact_family_and_isolated_source_font(self):
        self.visual_fixture()
        self.apply()
        self.assertIn(b'<04> <256E>', self.font['/ToUnicode'].get_data())
        self.assertIsNone(glyph_unicode('a4'))

    def test_arbitrary_glyph_gallery_drawing_a_different_code_is_rejected(self):
        self.visual_fixture(b'BT /Glyph 100 Tf 150 150 Td <05> Tj ET')
        with self.assertRaisesRegex(RuntimeError, 'verified source byte'):
            self.apply()
        self.assertEqual(self.font['/ToUnicode'].get_data(), self.old_cmap)

    def test_visual_glyph_gallery_bindings_and_shape_semantics_cannot_be_changed(self):
        self.visual_fixture()
        for field, replacement in (('source_pdf_sha256', 'other-pdf'),
            ('font_file_sha256', 'other-font'), ('gallery_pdf_sha256', 'other-gallery'),
            ('image_sha256', 'other-image'), ('gallery_page', 2), ('scope', 'report_reading')):
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                record['fonts'][0]['mappings'][0]['visual_glyph_evidence'][field] = replacement
                with self.assertRaises(RuntimeError):
                    self.apply(record)
                self.assertEqual(self.font['/ToUnicode'].get_data(), self.old_cmap)
        record = copy.deepcopy(self.record)
        record['fonts'][0]['mappings'][0]['unicode'] = '╯'
        with self.assertRaisesRegex(RuntimeError, 'visual semantics'):
            self.apply(record)

    def drawn_reader(self, content, form=None):
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        resources = dictionary(Font=dictionary(F1=self.font))
        if form is not None:
            resources[NameObject('/XObject')] = dictionary(Form=form)
        page[NameObject('/Resources')] = resources
        page[NameObject('/Contents')] = stream(content)
        path = self.root / 'drawn-source.pdf'
        writer.write(path)
        return PdfReader(path)

    def test_read_only_audit_finds_legal_ascii_and_layout_control_math_errors(self):
        reader = self.drawn_reader(b'BT /F1 12 Tf <580D> Tj ET')
        font = reader.pages[0]['/Resources']['/Font']['/F1']
        before = font['/ToUnicode'].get_data()
        result = diagnose_glyph_mappings(reader)
        issues = {issue['code_hex']: issue for issue in result['issues']}
        self.assertEqual(issues['58']['actual_unicode'], 'X')
        self.assertEqual(issues['58']['expected_unicode'], '∑')
        self.assertEqual(issues['0D']['actual_unicode'], '\r')
        self.assertEqual(issues['0D']['expected_unicode'], '∥')
        self.assertEqual(font['/ToUnicode'].get_data(), before)
        self.assertFalse(result['complete'])

    def test_audit_ignores_unused_unmapped_font_codes(self):
        self.font[NameObject('/ToUnicode')] = stream(self.old_cmap.replace(b'<00> <0000>', b'<00> <0028>'))
        result = diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <00> Tj ET'))
        self.assertEqual(result['scanned_fonts'][0]['used_code_hex'], ['00'])
        self.assertEqual(result['issues'], [])
        self.assertTrue(result['complete'])

    def test_audit_preserves_known_mathematical_unicode_variation_sequence(self):
        cmap = self.old_cmap.replace(b'<58> <0058>', b'<58> <2211FE01>')
        self.font[NameObject('/ToUnicode')] = stream(cmap)
        reader = self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET')
        result = diagnose_glyph_mappings(reader)
        self.assertTrue(result['complete'])
        self.assertIn('∑\ufe01', reader.pages[0].extract_text())
        self.assertEqual(reader.pages[0]['/Resources']['/Font']['/F1']['/ToUnicode'].get_data(), cmap)

    def test_variation_selector_does_not_mask_an_incorrect_base_glyph(self):
        self.font[NameObject('/ToUnicode')] = stream(self.old_cmap.replace(b'<58> <0058>', b'<58> <222BFE01>'))
        result = diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET'))
        self.assertEqual(result['issues'][0]['actual_unicode'], '∫\ufe01')
        self.assertEqual(result['issues'][0]['expected_unicode'], '∑')

    def test_known_combining_norm_bar_mapping_is_preserved_without_replacement(self):
        cmap = self.old_cmap.replace(b'<0D> <000D>', b'<0D> <20E6>')
        self.font[NameObject('/ToUnicode')] = stream(cmap)
        reader = self.drawn_reader(b'BT /F1 12 Tf <0D> Tj ET')
        self.assertTrue(diagnose_glyph_mappings(reader)['complete'])
        self.assertEqual(reader.pages[0]['/Resources']['/Font']['/F1']['/ToUnicode'].get_data(), cmap)

    def test_known_wide_accent_variation_does_not_allow_ascii_b(self):
        font_bytes = b'dup 88 /hatwide put\ncurrentfile eexec\n'
        self.font['/FontDescriptor'][NameObject('/FontFile')] = stream(font_bytes)
        self.font[NameObject('/ToUnicode')] = stream(self.old_cmap.replace(b'<58> <0058>', b'<58> <02C6FE01>'))
        self.assertTrue(diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET'))['complete'])
        self.font[NameObject('/ToUnicode')] = stream(self.old_cmap.replace(b'<58> <0058>', b'<58> <0062>'))
        self.assertEqual(diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET'))['issues'][0]['actual_unicode'], 'b')

    def test_assembled_parenthesis_extender_uses_portable_unicode_semantics(self):
        self.assertEqual(glyph_unicode('parenleftex'), '⎜')
        self.assertEqual(glyph_unicode('parenrightex'), '⎟')
        self.assertEqual(glyph_unicode('circleplustext'), '⨁')
        self.assertEqual(glyph_unicode('unionsqdisplay'), '⨆')

    def test_audit_traverses_form_text_arrays_using_the_selected_font(self):
        form = stream(b'BT /F1 12 Tf [<58> -10 <0D>] TJ ET')
        form[NameObject('/Subtype')] = NameObject('/Form')
        form[NameObject('/Resources')] = dictionary(Font=dictionary(F1=self.font))
        result = diagnose_glyph_mappings(self.drawn_reader(b'/Form Do', form))
        self.assertEqual({issue['code_hex'] for issue in result['issues']}, {'58', '0D'})

    def test_used_math_font_without_to_unicode_is_pending(self):
        del self.font['/ToUnicode']
        result = diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET'))
        self.assertFalse(result['complete'])
        self.assertIn('ToUnicode', result['issues'][0]['error'])

    def test_unsubset_math_font_is_also_audited(self):
        self.font[NameObject('/BaseFont')] = NameObject('/CMEX10')
        result = diagnose_glyph_mappings(self.drawn_reader(b'BT /F1 12 Tf <58> Tj ET'))
        self.assertEqual(result['issues'][0]['actual_unicode'], 'X')
        self.assertEqual(result['issues'][0]['expected_unicode'], '∑')


if __name__ == '__main__':
    unittest.main()
