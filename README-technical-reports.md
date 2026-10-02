# 大模型技术报告采集与飞书归档

覆盖 Anthropic、OpenAI、DeepSeek、Kimi、GLM、Google、Seed。初次回填窗口为 2026-04-02 至 2026-10-02；保存正式技术报告、模型/系统卡、公开研究论文及包含独立方法和实验的技术研究文章。

截至 2026-10-03，已核实并完成全文阅读的回填共 224 份：Anthropic 67、OpenAI 44、DeepSeek 5、Kimi 4、GLM 9、Google 54、Seed 41。全文与附录有分段阅读收据；PDF 原件、逐页提取与字体/图像补读证据按 SHA256 留存。飞书七个厂商目录按正式报告去重，关联发布文章和已排除公告保留标记。

这不代表已覆盖所有官方候选：Google 尚有 15 个候选的精确首发日期、原文或范围证据待核实，未混入上述 224 份。公开的日期审核 metadata 位于 `technical_reports/publication_date_audit.json`；本地 `archive.json` 的 `backfill` 字段保存窗口、数量及待核实清单，定时任务继续重试。

## 官方固定入口

| 厂商 | 每日检查入口 |
| --- | --- |
| Anthropic | https://www.anthropic.com/research · https://www.anthropic.com/transparency · https://www.anthropic.com/engineering |
| OpenAI | https://openai.com/news/rss.xml · https://deploymentsafety.openai.com/ |
| DeepSeek | https://www.deepseek.com/news/ · https://github.com/deepseek-ai |
| Kimi | https://www.kimi.com/en/blog/ · https://github.com/MoonshotAI |
| GLM | https://www.zhipuai.cn/zh/research · https://github.com/zai-org |
| Google | https://deepmind.google/research/publications/ · https://deepmind.google/models/model-cards/ · https://deepmind.google/blog/ · https://research.google/pubs/ |
| Seed | https://seed.bytedance.com/zh/public_papers · https://seed.bytedance.com/zh/research |

这些地址是持续更新的目录，更新频率由研究/模型发布决定。采集器遍历分页或完整页面数据；目录、研究博客和模型报告需组合监测。原始首发日期与索引更新时间分开记录，引用旧论文不会产生一份新报告。

## 运行与定时任务

Codex 已建立独立项目定时任务「技术报告每日采集与飞书归档」，每天北京时间 00:00 执行，即前一天 24:00。执行定义为 `技术报告每日采集工作流.yml`，保存配置镜像位于 `automations/automation/automation.toml`。本地执行依赖本机在线、Codex 可运行及项目磁盘挂载。

沿用 `.env` 内既有 Responses 模型配置（OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL），不在报告或日志中保存密钥。PDF 优先使用 Codex 附带的 Python；其他机器先安装依赖，并用 REPORT_PYTHON 指定该 Python。

```sh
.venv-web/bin/pip install -r requirements-reports.txt
.venv-web/bin/python -m technical_reports collect --output .technical_reports/candidates.json
.venv-web/bin/python -m technical_reports.reader --input .technical_reports/candidates.json --concurrency 3
.venv-web/bin/python -m technical_reports ingest --input .technical_reports/candidates.json
.venv-web/bin/python -m technical_reports sync
.venv-web/bin/python -m technical_reports stats
```

指定首次回填日期：`collect --since 2026-04-02 --until 2026-10-02`。常规发现使用七天重叠窗口，并重查所有已归档原文以发现修订、重试失败。HTTP 正文被限制时，运行任务 Agent 通过正常浏览器或官方全文替代入口补齐。索引摘要页不能充当论文全文，PDF 必须来自明确主报告链接。

日常 `collect` 也检查 coverage 中明确列出的未定日期候选。`technical_reports/publication_date_audit.json` 保存已审核的公开日期与范围 metadata；已核实的 arXiv 首发日和合格范围均成立后才获取原文。仅出版方日期、年月信息和范围不明的稿件继续保留诊断，不补造日期。日期已核实但范围未审核的新稿列入 `new_date_verified_candidates`，Agent 检查正式原文后记录范围结论，再重跑 `collect`：

```sh
.venv-web/bin/python -m technical_reports.publication_recovery \
  --record-scope eligible --vendor Google --title '论文标题' \
  --url 'https://research.google/pubs/论文地址/' \
  --reason '正式原文中独立方法与实验的审核理由' --evidence-url '原文证据链接'
```

范围结论可为 `eligible`、`excluded` 或 `uncertain`，按厂商、标题和 URL 匹配，保存在本地 `.technical_reports/publication_scope_decisions.json`。日期缓存位于 `.technical_reports/publication_date_cache.json`；失败或无匹配的 metadata 查询 24 小时后重试，候选增加新原文入口时立即重试。核实日期的缓存不会固定旧 arXiv 版本，后续采集仍检查当前原文修订。

## 本地数据

- `technical_reports/archive.json`：完整目录与已读报告 JSON，包括 vendor、title、source、published_at、published_at_raw、date_precision、url、report_url、overview、summary、read_status、reading_evidence、versions、feishu_url。
- `technical_reports/fulltext/`：未截断的提取全文及按内容哈希保存的不可变版本，仅本地保存。
- `.technical_reports/pdf/`：原始 PDF 与逐页提取证据，仅本地保存；真正空白页须有对象检查证据，乱码或缺页需恢复。
- `.technical_reports/reading_notes/`：逐段阅读笔记及断点；`.technical_reports/analyzed/` 保存完成的阅读结果。
- `technical_report_daily/YYYY-MM-DD.json`：网站检索用概述与总结，明确标记 `content_kind=技术报告总结`。与 AI 资讯归档分开。
- `.technical_reports/feishu_state.json`：厂商/报告文档映射与渲染哈希；原文缺失及写入失败保留记录。

完整原文按连续字符区间送入阅读 Agent，包括附录。每段记录 SHA256、起止区间；只有全部段落完成后，才生成概述和金字塔总结并进入最新15份。概述先说核心结论，总结按问题与方法、关键证据和条件、局限及应用价值展开。厂商自报指标注明来源，不假称独立复现。

同内容哈希跳过重读；修订保留旧版原文和总结。未取得全部原文的记录保留 `pending_full_text` 及错误，不伪造总结。单源错误不得阻止其他来源。

## 飞书与每日推送

根目录：https://ocns5gikjtax.feishu.cn/wiki/BPXvwO7TyiT8kukRTHecuViMnZb 。下设七个厂商目录，每份已读报告有独立子文档。厂商目录保留完整已发现目录及待处理状态。

报告子文档与每日要闻共用渲染函数：标题与超链接、中文概述、来源/首次发布时间/原文链接，然后是默认折叠的「总结（展开查看）」。每日要闻新增与「AI资讯」同级的「技术报告」，按首次发布时间展示最新15份；不足15份则展示实际数量。

原有每日飞书发布脚本自动在文档和群卡片中附加技术报告；群卡片使用默认收起的折叠面板。自定义机器人请求体上限为20KB，完整15份报告按实际字节数自动分成数张卡片，同次每日推送包含全部15份及完整总结，不截断。成功发送的页有本地收据，部分失败重试会续发尚未成功的页。夜间任务更新报告目录和当天已有要闻文档，不额外发送群消息。写入新内容成功后再移除旧内容，以保留失败时的可读版本。

网站部署继续使用 GitHub/Render 的既有流程；夜间任务将本轮总结 JSON 同步 GitHub，并按提交 SHA 核验部署。公开部署包仅包含总结 JSON，不包含原始PDF、本机凭据、完整本地目录和阅读缓存。
