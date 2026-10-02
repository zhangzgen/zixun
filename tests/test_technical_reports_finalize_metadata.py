import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from technical_reports.finalize_metadata import build_plan, apply_plan, SECURITY_REPORTS


def report(uid, title='Research', status='full_text_read', **fields):
    return {'id': uid, 'vendor': 'Google', 'title': title, 'source': 'Official Research',
            'published_at': '2026-06-01', 'url': 'https://example.org/' + uid,
            'content_hash': 'a' * 64, 'char_count': 100, 'fulltext_path': 'original.txt',
            'overview': 'A precise overview', 'summary': 'Core finding, methods, evidence and limitations',
            'read_status': status, 'reading_evidence': {'sha256': 'a' * 64, 'char_count': 100,
                'read_chars': 100, 'reader': 'agent', 'read_at': '2026-10-03T00:00:00+08:00',
                'chunks': [{'start': 0, 'end': 100, 'sha256': 'a' * 64}]}, **fields}


class FinalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'technical_reports/bootstrap').mkdir(parents=True)
        (self.root / '.technical_reports').mkdir()
        (self.root / 'original.txt').write_text('This fixture is never opened by the metadata audit.')

    def source(self, payload, name='google_seed.json'):
        (self.root / 'technical_reports/bootstrap' / name).write_text(json.dumps(payload))

    def archive(self, items):
        return {'version': 1, 'items': items, 'runs': [], 'updated_at': 'unchanged'}

    def test_apply_is_idempotent_and_preserves_reading_state_and_original(self):
        old = report('R-target', char_count=102)
        archive = self.archive([old])
        self.source({'items': [{**old, 'title': 'Formal report title'}]})
        plan = build_plan(archive, {}, self.root)
        self.assertEqual(plan['audit']['issues'], [])
        updated, state, result = apply_plan(archive, {}, plan)
        item = updated['items'][0]
        self.assertEqual(item['title'], 'Formal report title')
        self.assertEqual(item['char_count'], 100)
        for key in ('content_hash', 'read_status', 'overview', 'summary', 'reading_evidence', 'fulltext_path'):
            self.assertEqual(item[key], old[key])
        again_plan = build_plan(updated, state, self.root)
        again, again_state, result2 = apply_plan(updated, state, again_plan)
        self.assertEqual(result2['changes'], [])
        self.assertEqual(again, updated)
        self.assertEqual(again_state, state)
        self.assertEqual(archive['items'][0]['title'], 'Research')

    def test_alias_migrates_the_only_feishu_document_and_preserves_old_edition(self):
        old = report('R-old', feishu_document_id='doc-old', feishu_node_token='node-old', feishu_url='https://example.org/wiki/node-old')
        target = report('R-new', content_hash='b' * 64,
                        reading_evidence={**old['reading_evidence'], 'sha256': 'b' * 64})
        self.source({'items': [target], 'coverage': {'Google': {'id_aliases': [{'old_id': 'R-old', 'primary_id': 'R-new'}]}}})
        archive = self.archive([old, target])
        state = {'reports': {'R-old': {'document': 'doc-old', 'render_hash': 'old-layout'}}}
        plan = build_plan(archive, state, self.root)
        updated, migrated, result = apply_plan(archive, state, plan)
        self.assertEqual([i['id'] for i in updated['items']], ['R-new'])
        self.assertEqual(updated['items'][0]['content_hash'], 'b' * 64)
        self.assertEqual(updated['merged_reports']['R-old']['content_hash'], 'a' * 64)
        self.assertEqual(updated['items'][0]['feishu_document_id'], 'doc-old')
        self.assertEqual(migrated['reports']['R-new']['document'], 'doc-old')
        self.assertNotIn('render_hash', migrated['reports']['R-new'])
        self.assertNotIn('R-old', migrated['reports'])

    def test_pending_primary_does_not_erase_the_read_edition_or_make_it_read(self):
        old, target = report('R-old'), report('R-new', status='pending_full_text')
        self.source({'items': [target], 'coverage': {'id_aliases': [{'old_id': 'R-old', 'primary_id': 'R-new'}]}})
        archive = self.archive([old, target])
        plan = build_plan(archive, {}, self.root)
        updated, _, result = apply_plan(archive, {}, plan)
        self.assertEqual({i['id'] for i in updated['items']}, {'R-old', 'R-new'})
        self.assertEqual(next(i for i in updated['items'] if i['id'] == 'R-new')['read_status'], 'pending_full_text')
        self.assertTrue(any('completes reading' in d['reason'] for d in result['deferred']))

    def test_two_existing_feishu_documents_are_reported_and_never_overwritten(self):
        old, target = report('R-old', feishu_document_id='doc-old'), report('R-new', feishu_document_id='doc-new')
        self.source({'items': [target], 'coverage': {'id_aliases': [{'old_id': 'R-old', 'primary_id': 'R-new'}]}})
        state = {'reports': {'R-old': {'document': 'doc-old'}, 'R-new': {'document': 'doc-new'}}}
        plan = build_plan(self.archive([old, target]), state, self.root)
        self.assertEqual(len(plan['feishu_duplicate_documents']), 1)
        updated, preserved, result = apply_plan(self.archive([old, target]), state, plan)
        self.assertEqual(preserved, state)
        self.assertEqual(updated['items'][0]['feishu_document_id'], 'doc-new')
        self.assertTrue(any('independent Feishu' in row['reason'] for row in result['deferred']))

    def test_excluded_announcement_remains_traceable_with_a_feishu_label_action(self):
        old = report('R-funding', title='Funding announcement', feishu_document_id='doc-fund', feishu_node_token='node-fund')
        self.source({'items': [], 'coverage': {'excluded_reports': [{'id': 'R-funding', 'title': old['title'], 'reason': 'No method or experiment'}]}})
        state = {'reports': {'R-funding': {'document': 'doc-fund', 'render_hash': 'old'}}}
        plan = build_plan(self.archive([old]), state, self.root)
        self.assertEqual(plan['feishu_actions'][0]['action'], 'label_excluded_document')
        updated, preserved, result = apply_plan(self.archive([old]), state, plan)
        self.assertEqual(updated['items'], [])
        self.assertEqual(updated['excluded_reports']['R-funding']['reading_evidence'], old['reading_evidence'])
        self.assertTrue(preserved['excluded_reports']['R-funding']['needs_label'])

    def test_changed_reader_receipt_defers_char_count_without_changing_hash(self):
        item = report('R-target', char_count=102)
        archive = self.archive([item])
        plan = build_plan(archive, {}, self.root)
        concurrent = copy.deepcopy(archive)
        concurrent['items'][0]['content_hash'] = 'b' * 64
        concurrent['items'][0]['reading_evidence']['sha256'] = 'b' * 64
        concurrent['items'][0]['reading_evidence']['char_count'] = 105
        updated, _, result = apply_plan(concurrent, {}, plan)
        self.assertEqual(updated['items'][0]['char_count'], 102)
        self.assertEqual(updated['items'][0]['content_hash'], 'b' * 64)
        self.assertTrue(any('evidence changed' in row['reason'] for row in result['deferred']))

    def test_protected_security_reports_keep_distinct_formal_titles(self):
        items = [report(uid, title='Shared blog headline', canonical_url='https://example.org/' + uid + '.pdf') for uid in SECURITY_REPORTS]
        self.source({'items': items})
        plan = build_plan(self.archive(items), {}, self.root)
        updated, _, _ = apply_plan(self.archive(items), {}, plan)
        self.assertEqual({i['id']: i['title'] for i in updated['items']}, SECURITY_REPORTS)
        first, second = list(SECURITY_REPORTS)
        self.source({'items': items, 'coverage': {'id_aliases': [{'old_id': first, 'primary_id': second}]}})
        with self.assertRaisesRegex(ValueError, 'independent'):
            build_plan(self.archive(items), {}, self.root)

    def test_unreviewed_protected_field_is_rejected(self):
        archive = self.archive([report('R-target')])
        plan = build_plan(archive, {}, self.root)
        plan['updates'] = [{'id': 'R-target', 'fields': {'read_status': 'full_text_read'}}]
        with self.assertRaisesRegex(ValueError, 'unsafe metadata'):
            apply_plan(archive, {}, plan)


if __name__ == '__main__':
    unittest.main()
