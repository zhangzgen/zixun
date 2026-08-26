#!/usr/bin/env python3
import argparse
import json
import re
import time
from pathlib import Path

import run_ai_news as core


BAD_CONTENT_RE = re.compile(
    r"404|403 Forbidden|Access Denied|500 Internal Server Error|Allowed memory size|URL returned status",
    re.I,
)
SHELL_CONTENT_RE = re.compile(
    r"请先登录|登录后查看|注册登录|扫码登录|验证码|安全验证|人机验证|验证中心|"
    r"下载App|下载 APP|App 内打开|客户端打开|Just a moment|Checking your browser",
    re.I,
)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index_candidates(candidates_file):
    if not candidates_file:
        return {}
    data = load_json(candidates_file)
    return {item["id"]: item for item in data.get("candidates", [])}


def selected_items(selection_data, candidates_by_id):
    if isinstance(selection_data, list):
        return selection_data

    if "selected_items" in selection_data:
        return selection_data["selected_items"]

    if "selected_ids" in selection_data:
        items = []
        for item_id in selection_data["selected_ids"]:
            if item_id not in candidates_by_id:
                raise RuntimeError(f"selected id not found in candidates: {item_id}")
            item = dict(candidates_by_id[item_id])
            item["agent_reason"] = ""
            items.append(item)
        return items

    if "items" in selection_data:
        return selection_data["items"]

    raise RuntimeError("selection file must contain selected_items, selected_ids, items, or a JSON array")


def fetch_detail(item, max_chars, candidate=None):
    candidate = candidate or {}
    rss_content = candidate.get("rss_content") or item.get("rss_content") or ""
    if rss_content:
        text = core.strip_tags(rss_content)
        if len(text) >= 80 and not BAD_CONTENT_RE.search(text):
            return text[:max_chars], None, "rss"

    url = item.get("超链接")
    if not url:
        return "内容错误", "missing url", "none"

    try:
        html = core.fetch(url)
    except Exception as exc:
        return "内容错误", str(exc), "web"

    if BAD_CONTENT_RE.search(html):
        return "内容错误", "bad status content", "web"

    text = core.strip_tags(html)
    if len(text) < 80:
        return "内容错误", "too short", "web"
    if SHELL_CONTENT_RE.search(text) and len(text) < 1500:
        return "内容错误", "navigation/login shell", "web"

    return text[:max_chars], None, "web"


def main():
    parser = argparse.ArgumentParser(description="Fetch article details for Agent-selected news.")
    parser.add_argument("--input", default="tmp_ai_news_selected.json", help="Agent-selected JSON file.")
    parser.add_argument("--candidates", default="tmp_ai_news_candidates.json", help="Candidate JSON file for selected_ids lookup.")
    parser.add_argument("--output", default="tmp_ai_news_details.json", help="Detail JSON file to write.")
    parser.add_argument("--max-chars", type=int, default=6000, help="Maximum detail text chars per article.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between detail fetches.")
    args = parser.parse_args()

    selection_data = load_json(args.input)
    candidates_by_id = index_candidates(args.candidates)
    selected = selected_items(selection_data, candidates_by_id)

    details = []
    for item in selected:
        candidate = candidates_by_id.get(item.get("id", ""), {})
        detail_text, error, detail_source = fetch_detail(item, args.max_chars, candidate)
        enriched = {
            "id": item.get("id", ""),
            "资讯标题": item.get("资讯标题", ""),
            "发布时间": item.get("发布时间", ""),
            "来源": item.get("来源", ""),
            "超链接": item.get("超链接", ""),
            "agent_reason": item.get("agent_reason", ""),
            "正文抓取": "ok" if not error else error,
            "正文来源": detail_source,
            "正文": detail_text,
        }
        details.append(enriched)
        time.sleep(args.sleep)

    result = {
        "ok": True,
        "date": core.TODAY,
        "input": args.input,
        "detail_count": len(details),
        "agent_instruction": (
            "Agent must read details, remove items with 正文='内容错误', write final summaries and keywords "
            "to tmp_ai_news_publish.json, then call publish_ai_news.py. "
            "For items whose script detail fetch failed, hit anti-bot content, or only returned a navigation/login shell, "
            "Agent should operate a browser to open the original detail URL and recover article text; do not use web search. "
            "Only discard the item if both script fetch and browser recovery cannot establish reliable value. "
            "The final 超链接 must remain the article detail URL."
        ),
        "publish_file_schema": {
            "date": core.TODAY,
            "items": [
                {
                    "id": "N001",
                    "资讯标题": "original title",
                    "发布时间": "original publish time",
                    "来源": "original source",
                    "超链接": "original URL",
                    "资讯简介": "one concise sentence, core entities may be **bold**",
                    "关键词": "one keyword",
                    "群聊摘要": "optional shorter plain sentence for chat card",
                }
            ],
        },
        "details": details,
    }

    output = Path(args.output)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "detail_count": len(details),
        "failed_count": sum(1 for item in details if item["正文"] == "内容错误"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
