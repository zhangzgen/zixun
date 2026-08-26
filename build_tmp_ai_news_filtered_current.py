import json
from pathlib import Path

src = json.loads(Path("tmp_ai_news_collect_candidates.json").read_text(encoding="utf-8"))
keep_ids = {"N001", "N052", "N053", "N054", "N056", "N057", "N058", "N059", "N060", "N061", "N062", "N064", "N066", "N067", "N069", "N070"}
items = []
for item in src["candidates"]:
    if item.get("id") not in keep_ids:
        continue
    kept = {k: v for k, v in item.items() if k != "id"}
    kept["agent_reason"] = "近24小时内具备存档价值的AI模型、智能体、机器人、算力、核心技术或头部公司动态；已剔除重复事件、非AI内容、搜索页和低价值条目。"
    items.append(kept)

status = dict(src["source_status"])
status["新智元"] += ";browser_checked:recent_items_overlap_or_date_only"
status["AIBase"] += ";browser_checked:recent_items_date_only"
status["新华网科技"] += ";browser_checked:no_recent_ai_detail"
out = {
    "date": src["date"],
    "selection_reason": "完整审阅全部候选及当天既有归档；通过原站浏览器核验异常来源，保留字段完整、时间明确且具备存档价值的AI/大模型/智能体/机器人/核心技术事件，不限TopK，并做语义去重。",
    "source_status": status,
    "items": items,
}
Path("tmp_ai_news_filtered.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "items": len(items)}, ensure_ascii=False))
