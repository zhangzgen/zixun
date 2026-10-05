# 大模型技术报告采集与飞书归档

覆盖 Anthropic、OpenAI、DeepSeek、Kimi、GLM、Google、Seed。初次回填窗口为 2026-04-02 至 2026-10-02；保存正式技术报告、模型/系统卡、公开研究论文及包含独立方法和实验的技术研究文章。

截至 2026-10-03，已核实并完成全文阅读的回填共 224 份：Anthropic 67、OpenAI 44、DeepSeek 5、Kimi 4、GLM 9、Google 54、Seed 41。全文与附录有分段阅读收据；PDF 原件、逐页提取与字体/图像补读证据按 SHA256 留存。飞书七个厂商目录按正式报告去重，关联发布文章和已排除公告保留标记。

这不代表已覆盖所有官方候选：Google 尚有 15 个候选的精确首发日期、原文或范围证据待核实，未混入上述 224 份。公开的日期审核 metadata 位于 `technical_reports/publication_date_audit.json`；本地 `archive.json` 的 `backfill` 字段保存窗口、数量及待核实清单，历史补齐由用户另行安排。

## 官方固定入口

| 厂商 | 每日检查入口 |
| --- | --- |
| Anthropic | https://www.anthropic.com/research · https://www.anthropic.com/transparency · https://www.anthropic.com/engineering · https://alignment.anthropic.com/ · https://transformer-circuits.pub/ · https://www.anthropic.com/news |
| OpenAI | https://openai.com/news/rss.xml · https://deploymentsafety.openai.com/ |
| DeepSeek | https://www.deepseek.com/news/ · https://www.deepseek.com/transparency/ · https://github.com/deepseek-ai |
| Kimi | https://www.kimi.com/en/blog/ · https://github.com/MoonshotAI |
| GLM | https://www.zhipuai.cn/zh/research · https://github.com/zai-org |
| Google | https://deepmind.google/research/publications/ · https://deepmind.google/models/model-cards/ · https://deepmind.google/blog/ · https://research.google/pubs/ |
| Seed | https://seed.bytedance.com/zh/public_papers · https://seed.bytedance.com/en/public_papers · https://seed.bytedance.com/zh/research · https://seed.bytedance.com/transparency |

结构化来源清单保存在`technical_reports/sources.json`，按厂商列出23个官方入口及用途。飞书技术报告目录下另设[技术报告来源列表](https://ocns5gikjtax.feishu.cn/wiki/JMhqwXVH5ih2kqkX57FcHCv2nsh)子页面，以🔗标识和可点击完整网址展示。飞书开放接口的LinkPreview目前只支持消息链接，外部网站不能通过此接口直接生成带网站图标的预览卡片。

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

指定首次回填日期：`collect --since 2026-04-02 --until 2026-10-02`。常规采集默认使用 `new_only` 模式：以七天重叠窗口检查官方网址、索引、RSS及分页，按报告身份和网址与归档去重，只获取未归档新报告的全文。已归档报告（包括历史待处理记录、同一论文的新版本）直接跳过，不下载、不检查原文修订、不重新阅读。

候选 `items` 和 `new_date_verified_candidates` 都为空时，跳过阅读、入库、飞书同步、GitHub及Render，只记录本轮检查结果并按既有规则发送最终邮件。只有新报告才完整阅读正文和附录、生成总结并归档。来源失败或新候选日期/范围未决仍如实报告。用户另行要求历史维护时可显式使用 `collect --recheck-archived`；每日定时任务禁止使用该选项。

新报告的 HTTP 正文被限制时，运行任务 Agent 通过正常浏览器或官方全文替代入口补齐。索引摘要页不能充当论文全文，PDF 必须来自明确主报告链接。

日常 `collect` 也检查 coverage 中明确列出的未定日期候选。`technical_reports/publication_date_audit.json` 保存已审核的公开日期与范围 metadata；已核实的 arXiv 首发日和合格范围均成立后才获取原文。仅出版方日期、年月信息和范围不明的稿件继续保留诊断，不补造日期。日期已核实但范围未审核的新稿列入 `new_date_verified_candidates`，Agent 检查正式原文后记录范围结论，再重跑 `collect`：

```sh
.venv-web/bin/python -m technical_reports.publication_recovery \
  --record-scope eligible --vendor Google --title '论文标题' \
  --url 'https://research.google/pubs/论文地址/' \
  --reason '正式原文中独立方法与实验的审核理由' --evidence-url '原文证据链接'
```

范围结论可为 `eligible`、`excluded` 或 `uncertain`，按厂商、标题和 URL 匹配，保存在本地 `.technical_reports/publication_scope_decisions.json`。日期缓存位于 `.technical_reports/publication_date_cache.json`；新候选的失败或无匹配 metadata 查询 24 小时后重试，候选增加新原文入口时立即重试。已归档论文直接按身份去重，不因 arXiv 版本号或索引更新时间变化重新获取原文。

## 本地数据

- `technical_reports/archive.json`：完整目录与已读报告 JSON，包括 vendor、title、source、published_at、published_at_raw、date_precision、url、report_url、overview、summary、read_status、reading_evidence、versions、feishu_url。
- `technical_reports/fulltext/`：未截断的提取全文及按内容哈希保存的不可变版本，仅本地保存。
- `.technical_reports/pdf/`：原始 PDF 与逐页提取证据，仅本地保存；真正空白页须有对象检查证据，乱码或缺页需恢复。
- `.technical_reports/reading_notes/`：逐段阅读笔记及断点；`.technical_reports/analyzed/` 保存完成的阅读结果。
- `technical_report_daily/YYYY-MM-DD.json`：网站检索用概述与总结，明确标记 `content_kind=技术报告总结`。与 AI 资讯归档分开。
- `.technical_reports/feishu_state.json`：厂商/报告文档映射与渲染哈希；原文缺失及写入失败保留记录。

完整原文按连续字符区间送入阅读 Agent，包括附录。每段记录 SHA256、起止区间；只有全部段落完成后，才生成概述和金字塔总结并进入最新15份。概述先说核心结论，总结按问题与方法、关键证据和条件、局限及应用价值展开。厂商自报指标注明来源，不假称独立复现。

新报告本轮读取可按断点与内容哈希继续，历史报告的原文和总结保持存档。未取得全部原文的记录保留 `pending_full_text` 及错误，不伪造总结。单源错误不得阻止其他来源。

## 飞书与每日推送

根目录：https://ocns5gikjtax.feishu.cn/wiki/BPXvwO7TyiT8kukRTHecuViMnZb 。下设七个厂商目录，每份已读报告有独立子文档。厂商目录保留完整已发现目录及待处理状态。

技术报告总目录、七个厂商汇总和每日要闻采用同一版式：正文第一行「🔸 [来源] 原题」，换行显示中文概述；只有来源文字链接报告原文。标题左侧箭头默认收起完整金字塔总结，没有单独的「总结」标题。总结以原生文档块写入正文父块的子层级，标题、加粗及列表正确渲染。页面不展示发布时间，日期仍在本地 JSON 中保存并用于最新优先排序。每日要闻新增与「AI资讯」同级的「技术报告」，固定展示最新15份；不足15份则展示实际数量。

所有技术报告汇总页直接展示报告，不显示「已收录…份；其中…份已完成全文阅读」等收录信息。每次同步按发布时间倒序排列，后续新增的最新报告默认位于最上面。

现有单份报告子页面保留原版式和渲染哈希。只迁移汇总时运行 `.venv-web/bin/python -m technical_reports sync --catalogues-only`，跳过子页面内容、名称及质量标记；常规同步继续按旧版式维护新建或内容更新的单份子页面。

每日飞书群卡片在与AI资讯同级的「技术报告」栏展示最新5份已读报告：编号短句与绿色厂商标签，概述优先使用全文阅读Agent生成的chat_overview，不超过60个字符。历史记录缺少短句时使用受长度限制的概述作为兼容回退。群卡片不携带完整总结、发布时间或折叠面板，底部More按钮打开仍含最新15份及完整折叠总结的要闻文档。请求体须小于20KB；成功发送保留本地收据，同一轮重试不重复发送已成功页。夜间任务更新报告目录和当天已有要闻文档，不额外发送群消息。写入新内容成功后再移除旧内容，以保留失败时的可读版本。

夜间技术报告任务在业务开始前运行`.venv-web/bin/python -m technical_reports.task_report begin`，保存run_id、开始时间和归档快照；完成或失败后填写本轮实际结果，再运行`finish --report .task_reports/<run_id>.json`生成按厂商分组的新增、原文修订、补齐阅读及总结更新清单，最后调用`task_email.py`发送一封最终邮件。每轮都发邮件，无更新也明确说明；快照无法核验时不能声称无更新。邮件沿用现有SMTP配置和发送收据，包含报告标题、原文及飞书链接、阅读状态与执行异常。详见`README-email.md`。

网站部署继续使用 GitHub/Render 的既有流程；夜间任务将本轮总结 JSON 同步 GitHub，并按提交 SHA 核验部署。公开部署包仅包含总结 JSON，不包含原始PDF、本机凭据、完整本地目录和阅读缓存。
