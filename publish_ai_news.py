#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path

import run_ai_news as core
from technical_reports.store import latest as latest_reports


REQUIRED_ITEM_FIELDS = ["资讯标题", "发布时间", "来源", "超链接", "资讯简介", "关键词"]
NON_DETAIL_URL_PATTERNS = [
    re.compile(r"^https?://(?:www\.)?jiqizhixin\.com/industry/?(?:[?#].*)?$", re.I),
    re.compile(r"^https?://(?:www\.)?jiqizhixin\.com/?(?:[?#].*)?$", re.I),
    re.compile(r"^https?://(?:www\.)?readhub\.cn/?(?:[?#].*)?$", re.I),
    re.compile(r"^https?://tophub\.today/", re.I),
    re.compile(r"^https?://so\.toutiao\.com/search\?", re.I),
]


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_items(items):
    if not isinstance(items, list) or not items:
        raise RuntimeError("publish input must contain a non-empty items array")

    valid = []
    for index, item in enumerate(items, 1):
        missing = [field for field in REQUIRED_ITEM_FIELDS if not item.get(field)]
        if missing:
            raise RuntimeError(f"item {index} missing required fields: {', '.join(missing)}")
        url = item["超链接"].strip()
        if not re.match(r"^https?://", url, re.I):
            raise RuntimeError(f"item {index} 超链接 must be an HTTP(S) detail URL: {url}")
        if any(pattern.search(url) for pattern in NON_DETAIL_URL_PATTERNS):
            raise RuntimeError(f"item {index} 超链接 is not a news detail URL: {url}")
        item["超链接"] = url
        if item.get("资讯简介") == "内容错误":
            continue
        valid.append(item)

    if not valid:
        raise RuntimeError("no valid publishable items after filtering 内容错误")
    return valid


def markdown_for(items):
    lines = ["### 🟣 AI 资讯", "", "---", ""]
    for item in items:
        lines.append(f"   🔸 **[{item['关键词']}]({item['超链接']})** {item['资讯简介']}")
        lines.append("")
    from technical_reports.renderer import markdown
    from technical_reports.store import latest
    return "\n".join(lines).rstrip() + "\n\n" + markdown(latest(15))


def build_agent_card(date_text, doc_url, items, reports=None):
    summary_lines = []
    for index, item in enumerate(items, 1):
        summary = item.get("群聊摘要") or re.sub(r"\*\*", "", item["资讯简介"])
        summary_lines.append(f"{index}. {summary}<text_tag color='green'>{item['关键词']}</text_tag>")

    card = core.build_news_card(date_text, doc_url, items)
    card["body"]["elements"][3]["content"] = "\n".join(summary_lines)
    from technical_reports.renderer import card_sections
    from technical_reports.store import latest
    reports = latest(15) if reports is None else reports
    card['body']['elements'].extend([
        {'tag': 'hr'}, {'tag': 'markdown', 'content': f'**🟢 技术报告 · 最新 {len(reports)} 份**'},
        *card_sections(reports),
    ])
    return card


def build_agent_cards(date_text, doc_url, items, reports=None, max_bytes=19000):
    """Webhook bodies are limited to 20 KB; preserve complete reports in pages."""
    from copy import deepcopy
    from technical_reports.renderer import card_sections
    reports = latest_reports(15) if reports is None else reports[:15]
    first = build_agent_card(date_text, doc_url, items, reports=[])
    first['body']['elements'][-1]['content'] = f'**🟢 技术报告 · 最新 {len(reports)} 份**'
    continuation = {'schema': '2.0', 'config': {'width_mode': 'fill'},
                    'header': {'template': 'purple', 'title': {'tag': 'plain_text', 'content': f'{date_text} 技术报告'}},
                    'body': {'elements': [{'tag': 'markdown', 'content': f'**🟢 技术报告 · 最新 {len(reports)} 份**\n[查看完整要闻文档]({doc_url})'}]}}
    def size(card):
        return len(json.dumps({'msg_type': 'interactive', 'card': card}, ensure_ascii=False).encode('utf-8'))
    if size(first) > max_bytes:
        raise ValueError('AI news card exceeds the Feishu webhook body limit')
    cards = [first]
    for report in reports:
        elements = card_sections([report])
        trial = deepcopy(cards[-1])
        trial['body']['elements'].extend(elements)
        if size(trial) <= max_bytes:
            cards[-1] = trial
        else:
            page = deepcopy(continuation)
            page['body']['elements'].extend(elements)
            if size(page) > max_bytes:
                raise ValueError('one complete report exceeds the Feishu webhook body limit')
            cards.append(page)
    for number, card in enumerate(cards, 1):
        if len(cards) > 1:
            card['header']['title']['content'] += f'（{number}/{len(cards)}）'
    return cards


def push_agent_cards(date_text, doc_url, items):
    import fcntl
    from technical_reports.common import ROOT
    folder = ROOT / '.technical_reports'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'push.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _push_agent_cards(date_text, doc_url, items)


def _push_agent_cards(date_text, doc_url, items):
    """Checkpoint accepted pages so a retry does not repeat a partial push."""
    from technical_reports.common import ROOT, digest, now, write_json
    ledger_path = ROOT / '.technical_reports' / 'push_state' / (date_text + '.json')
    ledger = load_json(ledger_path) if ledger_path.exists() else {'pages': {}}
    publication = digest(json.dumps([date_text, doc_url, items], ensure_ascii=False, sort_keys=True))
    events = ledger.setdefault('publications', {})
    if publication not in events:
        events[publication] = build_agent_cards(date_text, doc_url, items)
        write_json(ledger_path, ledger)
    cards = events[publication]
    results = []
    for card in cards:
        key = digest(json.dumps(card, ensure_ascii=False, sort_keys=True))
        if key in ledger['pages']:
            results.append({'code': 0, 'already_sent': True})
            continue
        result = core.send_card(f'{date_text} 要闻', card)
        if result.get('code', result.get('StatusCode')) != 0:
            raise RuntimeError(f"Feishu card page rejected: code={result.get('code', result.get('StatusCode'))}")
        ledger['pages'][key] = {'accepted_at': now()}
        write_json(ledger_path, ledger)
        results.append({'code': 0})
    return {'code': 0, 'card_count': len(cards), 'pages': results}


def cleanup_temp_files(paths):
    removed = []
    for path in paths:
        target = Path(path)
        if target.exists():
            target.unlink()
            removed.append(str(target))
    return removed


def main():
    parser = argparse.ArgumentParser(description="Publish Agent-formatted AI news to Feishu wiki and chat.")
    parser.add_argument("--input", default="tmp_ai_news_publish.json", help="Agent-formatted publish JSON.")
    parser.add_argument("--result", default="ai_news_run_result.json", help="Run result JSON path.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and render output without calling Feishu APIs.")
    parser.add_argument("--cleanup-temp", action="store_true", help="Delete workflow temp files after successful publish.")
    args = parser.parse_args()

    payload = load_json(args.input)
    date_text = payload.get("date") or core.TODAY
    items = validate_items(payload.get("items", []))
    chat_items = payload.get("chat_items") or items[:5]
    if len(chat_items) < 3 and len(items) >= 3:
        chat_items = items[:3]

    markdown = markdown_for(items)
    doc_title = payload.get("doc_title") or f"📖 {date_text} 要闻"

    if args.dry_run:
        doc_url = payload.get("doc_url") or "DRY_RUN_DOC_URL"
        push_res = {"dry_run": True}
    else:
        token = core.tenant_token()
        parent = core.get_parent_node(token)
        space_id = parent.get("space_id")
        if not space_id:
            raise RuntimeError(f"missing space_id from parent node: {parent}")

        node = core.create_wiki_doc(token, space_id, doc_title)
        doc_id = node.get("obj_token") or node.get("obj_id")
        doc_url = node.get("url") or f"https://ocns5gikjtax.feishu.cn/wiki/{node.get('node_token', '')}"
        if not doc_id:
            raise RuntimeError(f"created node missing document token: {node}")

        core.write_doc_blocks(token, doc_id, items)
        push_res = push_agent_cards(date_text, doc_url, chat_items)

    cleanup_removed = []
    if args.cleanup_temp and not args.dry_run:
        cleanup_removed = cleanup_temp_files([
            "tmp_ai_news_candidates.json",
            "tmp_ai_news_selected.json",
            "tmp_ai_news_details.json",
            args.input,
        ])

    result = {
        "ok": True,
        "date": date_text,
        "doc_title": doc_title,
        "doc_url": doc_url,
        "published_count": len(items),
        "chat_count": len(chat_items),
        "push_result": push_res,
        "cleanup_removed": cleanup_removed,
        "markdown": markdown,
        "items": items,
        "technical_report_count": len(latest_reports(15)),
    }
    Path(args.result).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "doc_title": doc_title,
        "doc_url": doc_url,
        "published_count": len(items),
        "chat_count": len(chat_items),
        "push_result": push_res,
        "cleanup_removed": cleanup_removed,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        core.failure(exc)
        raise
