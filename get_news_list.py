#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import run_ai_news as core


COLLECTORS = [
    ("量子位", core.collect_qbitai),
    ("新智元", core.collect_xinzhiyuan),
    ("AIBase", core.collect_aibase),
    ("新华网科技", core.collect_xinhua),
    ("机器之心", core.collect_jiqizhixin),
    ("今日头条/Tophub", core.collect_tophub),
    ("Readhub", core.collect_readhub),
]


def public_item(item, index):
    public = {
        "id": f"N{index:03d}",
        "资讯标题": item["资讯标题"],
        "发布时间": item["发布时间"],
        "来源": item["来源"],
        "超链接": item["超链接"],
        "desc": item.get("desc", ""),
    }
    for field in ("rss_content", "content_source", "member_only"):
        if field in item:
            public[field] = item[field]
    return public


def load_history(include_history):
    if not include_history:
        return {}, "", "skipped"

    token = core.tenant_token()
    parent = core.get_parent_node(token)
    space_id = parent.get("space_id")
    if not space_id:
        raise RuntimeError(f"missing space_id from parent node: {parent}")

    history_titles = [f"📖 {core.YESTERDAY} 要闻", f"📖 {core.TWO_DAYS_AGO} 要闻"]
    history_status = {}
    history_parts = []
    for title in history_titles:
        text = core.get_history_text(token, space_id, title)
        history_status[title] = "found" if text else "missing"
        history_parts.append(text)
    return history_status, "\n".join(history_parts), "loaded"


def main():
    parser = argparse.ArgumentParser(description="Collect AI news candidates for Agent review.")
    parser.add_argument("--output", default="tmp_ai_news_candidates.json", help="JSON file to write.")
    parser.add_argument("--no-history", action="store_true", help="Skip Feishu history lookup.")
    parser.add_argument("--workflow", default="AI咨询收集工作流.yml", help="Workflow reference file.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    workflow_path = Path(args.workflow)
    workflow_loaded = workflow_path.read_text(encoding="utf-8") if workflow_path.exists() else ""

    history_status, history_text, history_mode = load_history(not args.no_history)

    items = []
    source_status = {}
    source_mode = {}
    for source_name, collector in COLLECTORS:
        before = len(items)
        try:
            mode = collector(items)
            source_status[source_name] = f"ok:{len(items) - before}"
            if mode:
                source_mode[source_name] = mode
        except Exception as exc:
            source_status[source_name] = f"error:{exc}"

    window_items = sorted(items, key=lambda item: item["dt"], reverse=True)
    candidates = [public_item(item, index) for index, item in enumerate(window_items, 1)]

    result = {
        "ok": True,
        "date": core.TODAY,
        "window_start": core.SINCE.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": core.NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "workflow_loaded_bytes": len(workflow_loaded.encode("utf-8")),
        "history_mode": history_mode,
        "history_status": history_status,
        "history_text": history_text,
        "source_status": source_status,
        "source_mode": source_mode,
        "candidate_count": len(candidates),
        "agent_instruction": (
            "Candidates are the complete collected news list inside the time window. The list script does not apply "
            "keyword filtering, deduplication, or scoring. If collection is incomplete, Agent should operate a browser "
            "on the original source/channel pages to supplement recent candidates; do not use web search. Every kept "
            "URL must be the article detail URL. Agent must clean all archive-worthy AI items without applying TopK, "
            "write tmp_ai_news_filtered.json, then run archive_ai_news.py; do not call any Feishu API or publisher."
            if args.no_history else
            "Candidates are the complete collected news list inside the time window. The list script does not apply "
            "keyword filtering, deduplication, or scoring. Agent must remove stale/noisy/duplicate items using "
            "history_text, select TopK, and write tmp_ai_news_selected.json before fetching details."
        ),
        "selected_file_schema": {
            "topk": "integer",
            "selection_reason": "short explanation",
            "selected_items": [
                {
                    "id": "N001",
                    "资讯标题": "keep original title",
                    "发布时间": "keep original publish time",
                    "来源": "keep original source",
                    "超链接": "keep original URL",
                    "agent_reason": "why this item is worth publishing",
                }
            ],
        },
        "archive_file_schema": {
            "date": core.TODAY,
            "selection_reason": "filtering explanation",
            "source_status": "copy source_status from this file",
            "items": [{
                "资讯标题": "keep original title",
                "发布时间": "keep original publish time",
                "来源": "keep original source",
                "超链接": "keep original detail URL",
                "desc": "optional original description",
                "agent_reason": "why this item is archive-worthy",
            }],
        } if args.no_history else None,
        "candidates": candidates,
    }

    output = Path(args.output)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "candidate_count": len(candidates),
        "history_status": history_status,
        "source_status": source_status,
        "source_mode": source_mode,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
