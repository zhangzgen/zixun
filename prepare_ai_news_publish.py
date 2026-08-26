#!/usr/bin/env python3
"""Build publish candidates from today's and yesterday's local archives."""

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import archive_ai_news as archive
import run_ai_news as core


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_archives(archive_dir, dates):
    items = []
    status = {}
    for date_text in dates:
        path = Path(archive_dir) / f"{date_text}.json"
        if not path.exists():
            status[str(path)] = "missing"
            continue
        payload = load_json(path)
        batch = payload.get("items", [])
        status[str(path)] = f"found:{len(batch)}"
        for item in batch:
            record = dict(item)
            record["archive_date"] = date_text
            items.append(record)
    return items, status


def dedupe_exact(items):
    seen = set()
    unique = []
    for item in sorted(items, key=lambda value: value.get("发布时间", ""), reverse=True):
        keys = archive.item_keys(item)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        unique.append(item)
    return unique


def load_feishu_history(history_dates):
    token = core.tenant_token()
    parent = core.get_parent_node(token)
    space_id = parent.get("space_id")
    if not space_id:
        raise RuntimeError(f"missing space_id from parent node: {parent}")

    status = {}
    parts = []
    for date_text in history_dates:
        title = f"📖 {date_text} 要闻"
        text = core.get_history_text(token, space_id, title)
        status[title] = "found" if text else "missing"
        if text:
            parts.append(text)
    return status, "\n".join(parts)


def public_item(item, index):
    public = {
        "id": f"N{index:03d}",
        "archive_id": item.get("archive_id", ""),
        "archive_date": item.get("archive_date", ""),
        "资讯标题": item.get("资讯标题", ""),
        "发布时间": item.get("发布时间", ""),
        "来源": item.get("来源", ""),
        "超链接": item.get("超链接", ""),
        "desc": item.get("desc", ""),
    }
    for field in ("rss_content", "content_source", "member_only", "agent_reason"):
        if item.get(field) not in (None, ""):
            public[field] = item[field]
    return public


def main():
    parser = argparse.ArgumentParser(description="Prepare Agent publish review from local daily archives.")
    parser.add_argument("--archive-dir", default="ai_news_daily", help="Directory containing YYYY-MM-DD.json files.")
    parser.add_argument("--date", help="Publish date in YYYY-MM-DD; defaults to Asia/Shanghai today.")
    parser.add_argument("--output", default="tmp_ai_news_candidates.json", help="Publish candidate JSON path.")
    parser.add_argument("--history-days", type=int, default=3, help="Feishu document dates to include, newest first.")
    parser.add_argument("--no-history", action="store_true", help="Skip Feishu history lookup (for tests only).")
    args = parser.parse_args()

    publish_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=core.TZ) if args.date else datetime.now(core.TZ)
    archive_dates = [publish_date.strftime("%Y-%m-%d"), (publish_date - timedelta(days=1)).strftime("%Y-%m-%d")]
    history_dates = [(publish_date - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(args.history_days)]

    combined, archive_status = load_archives(args.archive_dir, archive_dates)
    unique = dedupe_exact(combined)
    if args.no_history:
        history_status, history_text, history_mode = {}, "", "skipped"
    else:
        history_status, history_text = load_feishu_history(history_dates)
        history_mode = "loaded"

    candidates = [public_item(item, index) for index, item in enumerate(unique, 1)]
    result = {
        "ok": True,
        "date": publish_date.strftime("%Y-%m-%d"),
        "archive_dates": archive_dates,
        "archive_status": archive_status,
        "history_mode": history_mode,
        "history_status": history_status,
        "history_text": history_text,
        "candidate_count": len(candidates),
        "agent_instruction": (
            "Candidates come only from today's and yesterday's local archives. Agent must semantically remove duplicates "
            "within candidates and any event already present in history_text, retain only high-value AI news, rank by "
            "impact/timeliness/credibility, and select at most TopK=20. Preserve original title, publish time, source, "
            "and article detail URL. Write tmp_ai_news_selected.json before fetching details."
        ),
        "selected_file_schema": {
            "topk": "integer <= 20",
            "selection_reason": "short explanation",
            "selected_items": [{
                "id": "N001",
                "资讯标题": "keep original title",
                "发布时间": "keep original publish time",
                "来源": "keep original source",
                "超链接": "keep original URL",
                "agent_reason": "selection reason",
            }],
        },
        "candidates": candidates,
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": args.output,
        "archive_status": archive_status,
        "candidate_count": len(candidates),
        "history_status": history_status,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
