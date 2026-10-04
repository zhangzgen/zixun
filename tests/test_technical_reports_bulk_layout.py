from copy import deepcopy
from unittest.mock import patch

import pytest

from technical_reports import feishu


def tree(number):
    block = {'block_type': 2, 'text': {'elements': [{'text_run': {'content': str(number)}}], 'style': {'folded': True}}}
    block['children'] = [{'block_type': 2, 'text': {'elements': [{'text_run': {'content': 'evidence'}}]}} for _ in range(14)]
    return block


def test_bulk_batches_complete_trees_and_resolves_ids_regardless_of_response_order():
    blocks = [tree(i) for i in range(224)]
    original = deepcopy(blocks)
    calls = []

    def api(token, route, method, body):
        calls.append(body)
        assert len(body['descendants']) <= 900
        ids = {block['block_id'] for block in body['descendants']}
        assert set(body['children_id']) <= ids
        assert all(set(block['children']) <= ids for block in body['descendants'])
        return {'block_id_relations': [{'temporary_block_id': uid, 'block_id': 'real-' + uid} for uid in reversed(list(ids))]}

    with patch.object(feishu, 'api', side_effect=api), patch.object(feishu.time, 'sleep'):
        result = feishu.write_blocks('token', 'doc', blocks, bulk=True)
    assert len(calls) == 4
    assert len(result) == 224
    assert all(block['block_id'].startswith('real-') for block in result)
    assert blocks == original


def test_bulk_missing_descendant_confirmation_does_not_delete_old_content():
    with patch.object(feishu.core, 'list_doc_blocks', return_value=[{'block_id': 'doc', 'children': ['old']}]), \
         patch.object(feishu, 'api', return_value={'block_id_relations': []}) as api:
        with pytest.raises(RuntimeError, match='every nested block'):
            feishu.write_blocks('token', 'doc', [tree(1)], replace=True, bulk=True)
    assert api.call_count == 1 and api.call_args.args[2] == 'POST'
