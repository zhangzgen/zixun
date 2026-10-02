import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from technical_reports import reader
from technical_reports.common import digest


def original(tmp_path):
    text = ('Methods, conditions, negative results.\n' * 1900) + 'Appendix: final evidence.'
    (tmp_path / 'original.txt').write_text(text)
    return text, {'vendor': 'Kimi', 'title': 'Memory report', 'source': 'Official research',
                  'url': 'https://example.com/report', 'published_at': '2026-09-01',
                  'fulltext_path': 'original.txt'}


def chunk_receipt(number, text):
    return {'covered_chunk': number, 'covered_sha256': digest(text), 'notes': '方法、条件与局限已阅读。'}


def test_interrupted_reading_resumes_exact_chunks_and_includes_appendix(tmp_path):
    text, item = original(tmp_path)
    chunks = reader.reading_chunks(text)
    settings = SimpleNamespace(model='test-model')
    first = AsyncMock(side_effect=[chunk_receipt(1, chunks[0][1]), ConnectionError('interrupt')])
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', first):
        with pytest.raises(ConnectionError):
            asyncio.run(reader.read_report(item, None, settings))
    final = {'overview': '长期记忆报告说明了方法、实验条件及证据边界。',
             'summary': '核心结论：长期记忆能够改善任务表现。' * 20}
    second = AsyncMock(side_effect=[chunk_receipt(2, chunks[1][1]), final])
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', second):
        result = asyncio.run(reader.read_report(item, None, settings))
    assert second.await_count == 2
    assert 'Appendix: final evidence.' in second.await_args_list[0].args[2]
    assert result['reading_evidence']['read_chars'] == len(text)
    assert result['reading_evidence']['chunks'][-1]['end'] == len(text)


def test_receipt_mismatch_never_certifies_reading(tmp_path):
    _, item = original(tmp_path)
    request = AsyncMock(return_value={'covered_chunk': 1, 'covered_sha256': 'wrong', 'notes': 'notes'})
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', request):
        with pytest.raises(ValueError, match='receipt'):
            asyncio.run(reader.read_report(item, None, SimpleNamespace(model='test')))


def test_transient_receipt_mismatch_retries_the_entire_same_chunk(tmp_path):
    text, item = original(tmp_path)
    chunks = reader.reading_chunks(text)
    final = {'overview': '长期记忆报告说明了方法、实验条件及证据边界。',
             'summary': '核心结论：长期记忆能够改善任务表现。' * 20}
    request = AsyncMock(side_effect=[
        {'covered_chunk': 1, 'covered_sha256': 'wrong', 'notes': '不合格收据'},
        chunk_receipt(1, chunks[0][1]), chunk_receipt(2, chunks[1][1]), final])
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', request):
        result = asyncio.run(reader.read_report(item, None, SimpleNamespace(model='test')))
    assert request.await_args_list[0].args == request.await_args_list[1].args
    assert result['reading_evidence']['read_chars'] == len(text)
    assert result['reading_evidence']['chunks'][0]['sha256'] == digest(chunks[0][1])


@pytest.mark.parametrize('failure', [
    {'original_kind': 'pdf', 'error': 'PDF text extraction failed; graphical pages omitted'},
    {'original_quality_error': 'image benchmark table needs visual recovery'},
    {'original_documents': [{'empty_pages': [7]}]},
])
def test_incomplete_cached_original_cannot_be_certified(tmp_path, failure):
    _, item = original(tmp_path)
    request = AsyncMock()
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', request):
        with pytest.raises(ValueError, match='original quality'):
            asyncio.run(reader.read_report({**item, **failure}, None, SimpleNamespace(model='test')))
    request.assert_not_awaited()


def test_completed_stream_is_required():
    async def incomplete(*args):
        yield {'type': 'response.output_text.delta', 'delta': json.dumps({'summary': 'partial'})}
    with patch.object(reader, 'response_stream', incomplete):
        with pytest.raises(ValueError, match='before completion'):
            asyncio.run(reader.request(None, SimpleNamespace(model='test'), 'original'))


def test_report_claim_prevents_duplicate_parallel_reading(tmp_path):
    with patch.object(reader, 'ROOT', tmp_path):
        with reader.claim_report('R-test') as first:
            assert first
            with reader.claim_report('R-test') as second:
                assert not second
        with reader.claim_report('R-test') as third:
            assert third


def test_failed_stream_accepts_null_details_and_reports_code():
    async def failed(*args):
        yield {'type': 'response.failed', 'response': {'incomplete_details': None,
                                                     'error': {'code': 'server_error'}}}
    with patch.object(reader, 'response_stream', failed):
        with pytest.raises(reader.ModelResponseError) as caught:
            asyncio.run(reader.request(None, SimpleNamespace(model='test'), 'original'))
    assert caught.value.reason == 'server_error'


def test_output_budget_retry_preserves_complete_result():
    request = AsyncMock(side_effect=[reader.ModelResponseError('response.incomplete', 'max_output_tokens'),
                                    {'summary': 'complete'}])
    with patch.object(reader, 'request', request), patch.object(reader.asyncio, 'sleep', AsyncMock()):
        result = asyncio.run(reader.request_with_retry(None, None, 'original', 6500))
    assert result == {'summary': 'complete'}
    assert request.await_args_list[1].args[3] == 13000


def test_policy_failure_is_not_retried():
    request = AsyncMock(side_effect=reader.ModelResponseError('response.incomplete', 'content_filter'))
    with patch.object(reader, 'request', request):
        with pytest.raises(reader.ModelResponseError):
            asyncio.run(reader.request_with_retry(None, None, 'original'))
    assert request.await_count == 1


def test_queued_report_stays_unclaimed_and_rechecks_finished_archive(tmp_path):
    text, first = original(tmp_path)
    second = {**first, 'url': 'https://example.com/second', 'title': 'Second',
              'published_at': '2026-08-31'}
    first_id, second_id = reader.report_id(first), reader.report_id(second)
    source = tmp_path / 'input.json'
    source.write_text(json.dumps({'items': [first, second]}))
    archive = {'items': []}
    settings = SimpleNamespace(model='test', model_ready=True)

    async def finish_first(item, client, settings):
        with reader.claim_report(second_id) as available:
            assert available  # Waiting for a semaphore must not reserve the report.
        archive['items'] = [{'id': second_id, 'read_status': 'full_text_read',
                             'content_hash': digest(text)}]
        return {'id': first_id, **first}

    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader.Settings, 'from_env', return_value=settings), \
         patch.object(reader, 'load', side_effect=lambda: archive), \
         patch.object(reader, 'read_report', AsyncMock(side_effect=finish_first)) as read, \
         patch.object(reader, 'merge_locked'), patch.object(reader, 'export_search'):
        asyncio.run(reader.run([source], concurrency=1))
    assert read.await_count == 1
    counts = json.loads((tmp_path / 'technical_reports' / 'reading_result.json').read_text())
    assert counts['completed'] == counts['skipped'] == 1


def test_revision_reuses_only_exact_read_prefix_and_reads_new_appendix(tmp_path):
    text, item = original(tmp_path)
    chunks = reader.reading_chunks(text)
    settings = SimpleNamespace(model='test-model')
    final = {'overview': '长期记忆方法在指定测试条件下改善表现，并有明确局限。',
             'summary': '核心结论及方法、证据和局限。' * 30}
    first = AsyncMock(side_effect=[chunk_receipt(n, c) for n, (_, c) in enumerate(chunks, 1)] + [final])
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', first):
        asyncio.run(reader.read_report(item, None, settings))
    revised = text + '\nAppendix addition: chart reports lower success under distribution shift.'
    (tmp_path / item['fulltext_path']).write_text(revised)
    new_chunks = reader.reading_chunks(revised)
    second = AsyncMock(side_effect=[chunk_receipt(2, new_chunks[1][1]), final])
    with patch.object(reader, 'ROOT', tmp_path), patch.object(reader, 'request_with_retry', second):
        result = asyncio.run(reader.read_report(item, None, settings))
    assert second.await_count == 2
    assert 'Appendix addition' in second.await_args_list[0].args[2]
    assert result['reading_evidence']['chunks'][0]['notes_reused_from']
    assert result['reading_evidence']['read_chars'] == len(revised)
