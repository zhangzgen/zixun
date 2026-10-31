import json
from pathlib import Path


candidate_path = Path("tmp_ai_news_collect_candidates.json")
source = json.loads(candidate_path.read_text(encoding="utf-8"))
archive_path = Path("ai_news_daily") / f"{source['date']}.json"
archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {"items": []}

# Keep only new, archive-worthy Readhub and QbitAI items with direct detail URLs.
keep_ids = {"N051", "N053", "N054", "N056", "N057", "N061"}
reasons = {
    "N051": "英特尔CPU/GPU/NPU及AI开发生态的产品与产业动态。",
    "N053": "AI服务器与算力基础设施带动PCB产业扩产融资，反映供应链投资趋势。",
    "N054": "OpenAI暂停前沿模型训练及工具调用推理，涉及智能体安全和模型治理。",
    "N056": "AI生成技术进入获批公映的院线电影，属于生成式AI产业应用进展。",
    "N057": "头部手机厂商发布大模型影像功能，涉及端侧AI产品与云端处理。",
    "N061": "新模型创业公司融资及低成本商业模式，属于大模型产业动态。",
}

existing_urls = {item.get("超链接", "").split("?", 1)[0].rstrip("/") for item in archive.get("items", [])}
items = []
for candidate in source["candidates"]:
    if candidate.get("id") not in keep_ids:
        continue
    url = candidate.get("超链接", "")
    if not url.startswith(("https://", "http://")) or "/search?" in url:
        continue
    if url.split("?", 1)[0].rstrip("/") in existing_urls:
        continue
    item = {key: value for key, value in candidate.items() if key not in {"id", "dt"}}
    item["agent_reason"] = reasons[candidate["id"]]
    items.append(item)

status = dict(source["source_status"])
status["新智元"] += ";direct_channel_checked:no_parseable_dated_detail_items"
status["AIBase"] += ";direct_channel_checked:news_landing_shell_without_dated_items"
status["新华网科技"] += ";direct_channel_checked:no_parseable_dated_detail_items"
status["机器之心"] += ";direct_channel_checked:industry_page_unavailable_or_empty;rss_token_missing"
status["浏览器"] = "unavailable:Mac_locked_and_browser_auth_unsupported;public_source_pages_checked_directly"

payload = {
    "date": source["date"],
    "selection_reason": "审阅全部66条候选及当天已归档15条；保留过去24小时内未归档、具备AI产业或技术价值且使用详情页链接的6条。剔除Tophub的50条搜索链接、非AI项、同事件的重复报道及当日已有事件。新智元、AIBase、新华网科技和机器之心公开频道页未提供可核验的新增带日期详情项；浏览器UI因Mac锁定及认证桥接不可用，直接只读访问公开源补查。",
    "source_status": status,
    "items": items,
}
Path("tmp_ai_news_filtered.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"date": payload["date"], "input_count": len(items), "titles": [x["资讯标题"] for x in items]}, ensure_ascii=False, indent=2))
