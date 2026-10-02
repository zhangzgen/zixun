import json
from unittest.mock import patch

import pytest

from publish_ai_news import build_agent_cards, _push_agent_cards


def reports():
    return [{'title': f'Report {i}', 'overview': '概述' * 80, 'summary': f'报告{i}的完整总结\n' + '核心结论、实验条件、方法与局限。' * 90,
             'source': 'Official research', 'published_at': '2026-09-01', 'url': f'https://example.com/{i}'} for i in range(15)]


def test_webhook_pages_preserve_fifteen_complete_folded_summaries():
    items = [{'资讯简介': '示例资讯', '关键词': 'AI', '超链接': 'https://example.com/news'}]
    source = reports()
    cards = build_agent_cards('2026-10-02', 'https://example.com/doc', items, source)
    assert len(cards) > 1
    assert all(len(json.dumps({'msg_type': 'interactive', 'card': c}, ensure_ascii=False).encode()) < 20000 for c in cards)
    panels = [e for c in cards for e in c['body']['elements'] if e['tag'] == 'collapsible_panel']
    assert len(panels) == 15
    assert all(not p['expanded'] for p in panels)
    assert [p['elements'][0]['content'] for p in panels] == [r['summary'] for r in source]


def test_partial_push_resumes_without_repeating_accepted_pages(tmp_path):
    cards = [{'schema': '2.0', 'body': {'elements': []}, 'page': i} for i in range(3)]
    items = [{'资讯标题': 'same news'}]
    with patch('technical_reports.common.ROOT', tmp_path), patch('publish_ai_news.build_agent_cards', return_value=cards), patch('publish_ai_news.core.send_card', side_effect=[{'code': 0}, {'code': 1}]) as send:
        with pytest.raises(RuntimeError, match='rejected'):
            _push_agent_cards('2026-10-02', 'https://example.com/doc', items)
        assert send.call_count == 2
    with patch('technical_reports.common.ROOT', tmp_path), patch('publish_ai_news.build_agent_cards', side_effect=AssertionError('must use frozen publication')), patch('publish_ai_news.core.send_card', return_value={'code': 0}) as send:
        result = _push_agent_cards('2026-10-02', 'https://example.com/doc', items)
    assert send.call_count == 2
    assert result['pages'][0]['already_sent']
