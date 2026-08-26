#!/usr/bin/env python3
import json
from pathlib import Path

src = json.loads(Path("tmp_ai_news_collect_candidates.json").read_text(encoding="utf-8"))
reasons = {
    "N052": "美图AI视频应用商业化进展，反映头部影像公司的AI增长路径。",
    "N053": "平台级AI趋势报告揭示AI创作、办公和协作型AI的内容生态变化。",
    "N054": "具身模型实现观察示范后的零后训练泛化，属于机器人核心模型进展。",
    "N055": "通用物理AI模型与AI驱动物理系统设计方向，具备AI for Science价值。",
    "N056": "MiniMax收入与毛利高速增长，属于头部大模型公司的关键经营动态。",
    "N057": "京东发布物流工业级大模型并实现亿级路径秒级规划，具备产业应用价值。",
    "N058": "Noe-0以无本体数据训练世界动作模型，属于具身智能模型和数据范式进展。",
    "N059": "Shopify对Claude Code项目指令兼容性的讨论，反映编码智能体工程规范演进。",
    "N060": "启发学习提出跨域经验复用的推理侧框架，属于大模型推理核心技术探索。",
    "N061": "智算中心落地算电协同服务，属于AI算力基础设施建设动态。",
    "N062": "月之暗面洽谈Kimi K3海外云托管分成，属于国产大模型全球商业化动态。",
    "N063": "手机端本地模型基准覆盖智能与推理性能，反映端侧AI评测体系进展。",
    "N064": "百万行AI生成代码经持续验证投入大规模使用，体现编码智能体复杂工程能力。",
    "N065": "分析GPU租赁价格与超节点互联、利用率关系，具备AI算力产业存档价值。",
}

items = []
for item in src["candidates"]:
    if item["id"] in reasons:
        kept = {key: value for key, value in item.items() if key != "id"}
        kept["agent_reason"] = reasons[item["id"]]
        items.append(kept)

items.append({
    "资讯标题": "WRC回顾 | 五天三项新突破，星源智跑出具身智能“加速度”",
    "发布时间": "2026-08-26 17:51:33",
    "来源": "新智元",
    "超链接": "https://aiera.com.cn/2026/08/26/other/admin/110721/wrc%e5%9b%9e%e9%a1%be-%e4%ba%94%e5%a4%a9%e4%b8%89%e9%a1%b9%e6%96%b0%e7%aa%81%e7%a0%b4%ef%bc%8c%e6%98%9f%e6%ba%90%e6%99%ba%e8%b7%91%e5%87%ba%e5%85%b7%e8%ba%ab%e6%99%ba%e8%83%bd%e5%8a%a0/",
    "desc": "星源智在世界机器人大会展示具身智能产品与场景应用进展。",
    "content_source": "browser_direct",
    "agent_reason": "新智元原始频道最新详情稿，记录具身智能企业的产品和场景突破。",
})

source_status = dict(src["source_status"])
source_status["新智元"] += ";browser_checked;browser_supplement:1"
source_status["新华网科技"] += ";browser_check_failed:connection_closed"

out = {
    "date": src["date"],
    "selection_reason": "完整审阅91条候选及当天79条既有归档，并直接核查新智元与新华网科技原始频道；保留模型、智能体、机器人、算力、核心技术、头部公司与AI产业应用资讯，剔除非AI、广告、搜索壳、低价值和重复事件，不限制TopK。本轮保留14条脚本候选并补充1条新智元详情页。",
    "source_status": source_status,
    "items": items,
}
Path("tmp_ai_news_filtered.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "items": len(items)}, ensure_ascii=False))
