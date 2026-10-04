import json
from unittest.mock import patch

import pytest

from publish_ai_news import build_agent_card, build_agent_cards, _push_agent_cards


def reports():
    return [{'title': f'Report {i}', 'overview': '概述' * 80, 'summary': f'报告{i}的完整总结\n' + '核心结论、实验条件、方法与局限。' * 90,
             'chat_overview': f'编号{i}的独立简要结论。', 'vendor': 'Anthropic',
             'source': 'Official research', 'published_at': f'2026-09-{i + 1:02d}', 'url': f'https://example.com/{i}'} for i in range(15)]


def test_webhook_has_five_newest_short_previews_and_peer_sections_with_shared_more():
    items = [{'资讯简介': '示例资讯', '关键词': 'AI', '超链接': 'https://example.com/news'}]
    source = reports()
    cards = build_agent_cards('2026-10-02', 'https://example.com/doc', items, source)
    assert len(cards) == 1
    assert all(len(json.dumps({'msg_type': 'interactive', 'card': c}, ensure_ascii=False).encode()) < 20000 for c in cards)
    elements = cards[0]['body']['elements']
    assert all(e['tag'] != 'collapsible_panel' for e in elements)
    labels = [e['columns'][1]['elements'][0]['content'] for e in elements
              if e['tag'] == 'column_set' and e['columns'][1]['elements'][0]['tag'] == 'markdown']
    assert labels == ["<text_tag color='purple'>AI资讯</text_tag>", "<text_tag color='purple'>技术报告</text_tag>"]
    text = next(e['content'] for e in elements if e['tag'] == 'markdown' and '编号' in e['content'])
    assert text.splitlines() == [f"{index}. 编号{i}的独立简要结论。<text_tag color='green'>Anthropic</text_tag>"
                                for index, i in enumerate(range(14, 9, -1), 1)]
    payload = json.dumps(cards, ensure_ascii=False)
    assert all(r['summary'] not in payload and r['published_at'] not in payload for r in source)
    buttons = [e['columns'][1]['elements'][0] for e in elements
               if e['tag'] == 'column_set' and e['columns'][1]['elements'][0]['tag'] == 'button']
    assert len(buttons) == 1
    assert buttons[0]['behaviors'][0]['default_url'] == 'https://example.com/doc'


def test_default_card_requests_only_five_reports_and_handles_empty_archive():
    items = [{'资讯简介': '示例资讯', '关键词': 'AI', '超链接': 'https://example.com/news'}]
    with patch('publish_ai_news.latest_reports', return_value=[]) as latest:
        card = build_agent_card('2026-10-02', 'https://example.com/doc', items)
    latest.assert_called_once_with(5)
    assert '暂无已完成全文阅读的技术报告' in json.dumps(card, ensure_ascii=False)


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
