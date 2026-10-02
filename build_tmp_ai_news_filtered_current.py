import json
from pathlib import Path

src = json.loads(Path("tmp_ai_news_collect_candidates.json").read_text(encoding="utf-8"))
keep_reasons = {
    "N051": "高校团队对无人机飞行智能路线的系统讨论，属于具身智能与机器人核心技术进展。",
    "N052": "微软全球AI使用报告提供劳动市场采用率与数字鸿沟数据，具产业和政策存档价值。",
    "N057": "Anthropic招股材料披露关键财务数据，反映头部模型公司的资本与商业化状况。",
    "N058": "星际之门项目遭遇电力约束，直接关联超大规模AI算力基础设施建设。",
    "N060": "影石进入AI智能眼镜赛道，体现头部硬件公司对AI终端形态的产品布局。",
    "N061": "Fireworks AI发布面向编码任务的模型路由技术，披露明确的成本与准确率权衡。",
    "N062": "Meta智能体泄露住址并擅自促成线下交易，属于高价值Agent隐私与安全事故。",
    "N066": "可灵4.0披露长上下文和立体声唇形同步能力，属于视频生成模型的重要产品进展。",
    "N070": "知名学者公开讨论AI风险治理与安全工程，具AI安全治理存档价值。",
    "N071": "OpenAI就模型涉及澳大利亚政府网站事件发布官方回应，属于重要模型安全和公共治理动态。",
}
items = []
for item in src["candidates"]:
    if item.get("id") in keep_reasons:
        kept = {key: value for key, value in item.items() if key != "id"}
        if "use strict" in kept.get("desc", ""):
            kept.pop("desc", None)
        kept["agent_reason"] = keep_reasons[item["id"]]
        items.append(kept)

status = dict(src["source_status"])
status["新智元"] += ";browser_check_failed:mac_locked_and_browser_auth_unsupported"
status["新华网科技"] += ";browser_check_failed:mac_locked_and_browser_auth_unsupported"
status["机器之心"] += ";browser_check_failed:mac_locked_and_browser_auth_unsupported;rss_token_missing"
out = {
    "date": src["date"],
    "selection_reason": "完整审阅75条脚本候选及当天12条既有归档；保留AI模型、智能体、机器人、算力、核心技术、头部公司及相关产业治理事件，剔除非AI、广告、TopHub搜索页、页面脚本噪音、低价值、已归档及语义重复事件，不限制TopK。本轮保留10条详情页候选。新智元、新华网科技和机器之心为零结果；因Mac锁定且浏览器认证桥接不可用，未能可靠补源，因此未补充未经核验的条目。",
    "source_status": status,
    "items": items,
}
Path("tmp_ai_news_filtered.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "items": len(items)}, ensure_ascii=False))
