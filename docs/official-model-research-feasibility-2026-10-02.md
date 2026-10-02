# 大模型厂商官方技术内容接入可行性

核查日期：2026-10-02（Asia/Shanghai）。范围：Anthropic、OpenAI、DeepSeek、Moonshot AI / Kimi、智谱 / Z.ai / GLM、Google / Google DeepMind、ByteDance Seed。本文是调研与方案判断，未启用新采集源或修改生产流程。

此页保留前期调研判断；后续接入已实施，当前采集、全文阅读、飞书与定时任务使用说明见 [README-technical-reports.md](../README-technical-reports.md)。

## 判断

七家均持续发布官方技术内容，但发布节奏以研究进展、模型版本和专项研究为主，没有核实到统一的固定周期承诺。技术报告、系统卡、研究博客、工程实践和 API 更新不是同一种内容，需要单独分类。

接入现有「资讯之间」系统可行。第一阶段接入标题、发布时间、官方来源、中文摘要、原始链接和报告附件链接，难度低到中等；第二阶段支持长报告全文、页码引用和修订版本，难度中等到较高。应优先做第一阶段，再根据实际研究需求做第二阶段。

本次确认的是入口、公开内容形式和一次实际访问可用性，不代表长期抓取稳定性或完整覆盖率。未对全部历史论文、全部 PDF 附件进行全文验证。

## 入口与内容

| 厂商 | 建议监测入口 | 主要内容 | 形式及节奏 |
| --- | --- | --- | --- |
| Anthropic | [研究](https://www.anthropic.com/research)、[工程](https://www.anthropic.com/engineering)、[透明度中心](https://www.anthropic.com/transparency)、[新闻](https://www.anthropic.com/news) | 模型能力、安全与红队评估、对齐、可解释性、AI 社会经济影响；Agent、工具调用等工程实践 | HTML 博客、PDF System Card、论文/arXiv 链接；研究和工程内容不定期，系统卡主要随模型版本发布 |
| OpenAI | [研究索引](https://openai.com/zh-Hans-CN/research/index/)、[部署安全与系统卡](https://deploymentsafety.openai.com/)、[官方新闻 RSS](https://openai.com/news/rss.xml)、[API 更新](https://developers.openai.com/api/docs/changelog) | 模型与多模态研究、推理、评测、安全和部署评估、模型系统卡、工程与 API 变化 | HTML、报告/系统卡、部分 PDF、RSS XML；综合发布频繁，完整技术报告通常随项目或模型发布，不保证每次版本都公开完整训练细节 |
| DeepSeek | [研究与动态](https://www.deepseek.com/news/)、[官方 GitHub](https://github.com/deepseek-ai)、[API 更新](https://api-docs.deepseek.com/updates/) | MoE、注意力与长上下文、强化学习和推理、训练与推理效率、OCR、多模态、部署与开源工具 | HTML、GitHub README/Markdown、PDF、arXiv；模型及研究项目驱动，API 更新更偏服务变化 |
| Kimi / Moonshot AI | [中文研究博客](https://www.kimi.com/blog/)、[英文研究博客](https://www.kimi.com/en/blog/)、[官方 GitHub](https://github.com/MoonshotAI) | 模型架构、优化器、强化学习、长上下文、Agent 与工具调用、多模态、评测和推理系统 | HTML 技术博客、Markdown、PDF、arXiv/项目页；模型与专题研究驱动，中英文内容存在重复 |
| GLM / 智谱 / Z.ai | [官网研究](https://www.zhipuai.cn/zh/research)、[官方 GitHub](https://github.com/zai-org)、[模型发布记录](https://docs.z.ai/release-notes/new-released) | GLM 模型架构与训练、编程和长程 Agent、多模态、推理工程、开源与部署、API 能力 | HTML、GitHub README/Markdown、arXiv 技术报告和文档；模型迭代与专题研究驱动 |
| Google / Google DeepMind | [DeepMind 论文](https://deepmind.google/research/publications/)、[DeepMind 博客](https://deepmind.google/blog/)、[Gemini 模型与评测入口](https://deepmind.google/models/gemini/)、[Google Research 论文](https://research.google/pubs/) | Gemini/Gemma、推理、多模态与生成、Agent/机器人、评测、安全、训练方法；也包括科学与其他计算机研究 | HTML 博客、论文摘要页、PDF、arXiv、模型报告；持续发布，模型报告随项目/版本发布；需要筛选大模型相关内容 |
| ByteDance Seed / 字节跳动 Seed | [中文博客与研究](https://seed.bytedance.com/zh/research)、[英文博客与研究](https://seed.bytedance.com/en/research)、[论文索引](https://seed.bytedance.com/zh/public_papers) | Seed 基础模型、Agent、Seedream 图像生成、Seedance 视频生成、语音/音视频模型、训练与推理基础设施、评测与前沿研究 | HTML 博客、论文/项目页、模型卡、PDF/arXiv；模型迭代与研究项目驱动，中英文存在重复 |

不要只监测首页。研究索引和官网关联的官方仓库更适合持续发现报告。GLM 是模型系列，发布主体为智谱 / Z.ai；Kimi 是 Moonshot AI 的模型/产品品牌，来源字段宜统一厂商名称。

Google 建议以 `vendor=Google` 归组，再记录 `publisher=Google DeepMind / Google Research`；Seed 建议以 `vendor=ByteDance`、`publisher=Seed` 记录，模型家族单独标识 Seed、Seedream、Seedance 等。豆包是产品/服务品牌，不宜与 Seed 研究团队混成同一个内容类型。

## 具体报告与发布实例

以下实例用于验证内容形态，不是最新模型推荐，也不是穷尽清单。

- Anthropic：[Constitutional AI 研究页](https://www.anthropic.com/research/constitutional-ai-harmlessness-from-ai-feedback)，页面包含摘要、arXiv 和 PDF 链接；透明度中心集中列出多个 Claude 版本的系统卡与安全评估。
- OpenAI：研究索引列出模型发布、论文/研究和系统卡，包括 [GPT-6 Astra System Card](https://deploymentsafety.openai.com/gpt-6-astra)。RSS 中还能定位到 [gpt-oss-safeguard technical report](https://openai.com/index/gpt-oss-safeguard-technical-report)，该报告标题、描述和日期由本次实际读取的官方 RSS 提供。
- DeepSeek：[V3 官方发布页](https://www.deepseek.com/news/deepseek-v3/)直接链接到 [DeepSeek V3 PDF](https://github.com/deepseek-ai/DeepSeek-V3/blob/main/DeepSeek_V3.pdf)；[R1 仓库](https://github.com/deepseek-ai/DeepSeek-R1)包含模型介绍、评测、部署说明和技术报告。
- Kimi：[K2 技术博客](https://www.kimi.com/en/blog/kimi-k2)、[K2 官方仓库](https://github.com/MoonshotAI/Kimi-K2)和 [K2 PDF](https://github.com/MoonshotAI/Kimi-K2/blob/main/tech_report.pdf)。研究索引还包含 Muon、MoBA、Mooncake 等训练/推理专题。
- GLM：[GLM-5 官方仓库](https://github.com/zai-org/GLM-5)直接链接到 [GLM-5 技术报告](https://arxiv.org/abs/2602.15763)；[GLM-4.5 仓库](https://github.com/zai-org/GLM-4.5)包含模型能力、评测、部署及论文链接。
- Google：[Visual General Intelligence: A White Paper](https://deepmind.google/research/publications/270149/)的官方论文摘要页，包含摘要、作者、arXiv 和 PDF 链接。DeepMind 论文索引与博客适合组合监测，模型报告还需跟进对应模型项目页。
- Seed：[Seed2.0 Model Card](https://seed.bytedance.com/zh/public_papers/seed2-0-model-card-towards-intelligence-frontier-for-real-world-complexity)由官网研究索引直接链接，详情页包含摘要、作者和 [PDF](https://arxiv.org/pdf/2607.00248)；[Seed2.1 技术发布博客](https://seed.bytedance.com/zh/blog/seed2-1-officially-released-advancing-ai-productivity)提供 Agent、编程、多模态评测和项目页入口。论文索引还列出 EdgeBench、MegaScale-Omni、Seedance 2.0 等研究，体现其不止发布文本模型报告。

## 实际访问发现

1. Anthropic 的研究、新闻、透明度中心均返回 HTTP 200，页面可直接提取内容。官网 sitemap 也可读取。尚未确认其提供覆盖这些栏目、适合作为主入口的官方 RSS。
2. OpenAI 的新闻 RSS 返回 HTTP 200，本次包含 1,243 个 item，含标题、链接、发布时间和部分描述、分类。`content:encoded` 全文元素计数为 0，因此 RSS 可发现文章，但不能视为全文来源；部分系统卡描述也为空。综合 RSS 包含客户案例、公司新闻等非技术内容，需要二次分类。
3. OpenAI 研究/新闻详情的普通 HTTP 请求返回 403；内置浏览器在正常页面加载后可读取研究概览和研究索引。说明存在正文采集访问差异，应以 RSS 发现为主，浏览器作为补充，不以单次浏览器成功承诺长期自动抓取成功。
4. OpenAI 研究索引还出现 `deploymentsafety.openai.com` 的系统卡/增补报告链接，单靠主站 RSS 不应承诺覆盖所有安全报告。已进一步验证该安全报告站首页和一篇系统卡均返回 HTTP 200，可读取 HTML 正文，适合作为独立补充来源。
5. DeepSeek `/news/` 页面同时包含动态与研究索引，可读到论文日期及 arXiv 链接，官网 sitemap 可读取。`api-docs.deepseek.com/news` 返回的是 API 起始介绍内容，不能作为新闻列表；使用 `/updates/` 获取 API 变更。
6. Kimi `/blog/` 和 `/en/blog/` 可读取，路径末尾 `/` 需要保留，本次无斜杠请求遇到 HTTP 308。根 sitemap 本次主要列出产品页，没有研究文章，故不能把它作为研究发现的唯一来源。
7. 智谱中文研究页可读取正文和列表；部分列表项目由交互及页面数据提供，不宜仅依赖 `<a href>` 提取。Z.ai 的 `/blog` 返回 404，而已知单篇 `/blog/glm-4.5` 返回 200 但只取得极短页面壳；不宜把 `/blog` 假设为可采集的索引页，优先使用智谱研究页和官方仓库。
8. 三个官方 GitHub 组织的公开仓库列表 API 均可读取；具体模型仓库可取得 README 和论文链接。不要把仓库 `updated_at`、每次 commit 或 star/issue 活动当作新报告发布。
9. Google DeepMind 的论文列表、博客列表、一个论文详情页，以及 Google Research 论文列表均返回 HTTP 200，可读取内容。论文列表有分页，DeepMind 博客会链接到 `blog.google`，采集器需要保留最终官方详情 URL，不能只允许单一域名。Google Research 涵盖广泛学科，应按大模型/生成式 AI 主题过滤；期刊年份也不能直接当作精确网页发布日期。
10. Seed 中英文首页、研究入口、技术博客、完整论文索引和一个模型卡详情均返回 HTTP 200，可读取内容；研究页将博客分成基础模型、视觉智能、语音智能、AI Infra、前沿研究，并附精选论文列表及完整论文入口。本次论文索引显示 242 条、13 页，首页精选列表不足以覆盖全部论文，需要处理分页与研究主题筛选。官网 sitemap 可读，但其中 `lastmod` 与博客正文发布日期不同，只能辅助发现或检查更新。部分精选论文标题没有普通 `<a>` 链接，不能仅用锚点采集器承诺发现全部论文。尚未确认覆盖其研究内容的官方 RSS。

## 与现有系统的对应关系

已阅读 `get_news_list.py`、`get_news_details.py`、`archive_ai_news.py`、`news_web/archive_store.py`、`news_web/agent.py`、`README-web.md` 和日内采集工作流。

- 候选层：`get_news_list.py` 的 `COLLECTORS` 当前接入七个媒体/聚合源，尚未接入这七家的专用官方采集器。可以新增官方来源适配器，将 RSS、HTML 和仓库材料转成现有候选结构。
- 归档层：已有 `资讯标题`、`发布时间`、`来源`、`超链接`、摘要/正文等字段。`archive_ai_news.py` 可保留额外字段，因此元数据可以渐进增加。
- 检索层：`ArchiveStore` 已支持本地 SQLite FTS5、来源与日期筛选、摘要/正文阅读和 URL 去重。官方记录进入日归档后，可复用现有问答和引用展示。
- 运行方式：现有日内流程每天四轮采集，成功后通过 GitHub 同步和 Render 部署更新网站。新增来源可以沿用此流程。当前网页 Agent 仅检索本地归档，实时联网不在其能力范围，官方资料仍应先由采集侧入库。

## 必须处理的差异

| 问题 | 当前行为或限制 | 接入建议 |
| --- | --- | --- |
| 延迟收录与历史资料 | 采集器按最近 24 小时过滤 | 用每源游标/已见 URL 加 3–7 天重叠发现窗口；历史回填单独运行，保留原始发布日期 |
| 日期精度 | 归档要求秒级时间；网页可能只有日期，论文还存在初版/修订日期 | 增加 `published_at_raw`、`date_precision`、原时区；兼容时间的占位值不应展示为厂商精确发布时间；无日期资料记录首次发现时间并明确标识 |
| 同一研究多载体 | 现有主要按 URL/标题合并；媒体和官网可能是同一事件 | 以厂商、模型/项目、报告类型和论文标识关联，保留官方详情页、PDF、arXiv、仓库等所有资料链接 |
| 修订版本 | 归档合并主要补充空字段；同 URL 已有非空正文不会被新版替换 | 保存内容哈希、报告版本和修订时间；更新应生成可追踪的新版本，而非仅重复发现 |
| PDF 与长文 | 详情脚本只剥离 HTML，默认 6,000 字符；索引正文上限 16,000 字符 | 第一阶段仅存附件链接和短摘要；全文阶段增加 PDF 提取、章节/页级分块，按片段检索并保留页码 |
| 英文研究与中文提问 | 现有 FTS 主要做字面关键词匹配 | 保留英文原题和模型名称，同时写中文摘要与技术关键词；全文阶段再评估语义检索 |
| 失败与空内容 | HTTP 200 也可能仅返回动态页面壳 | 分开记录发现成功、详情失败、摘要可用、附件未解析；失败不得丢失已发现元数据或冒充读过全文 |
| 栏目噪音 | 新闻和发布记录混有案例、合作、公司活动和 API 变更 | 分类为技术报告、系统卡、研究、工程实践、模型发布、API 更新；商业/客户内容按现有收录策略处理 |

建议新增的元数据包括 `vendor`、`content_type`、`model_family`、`language`、`canonical_url`、`paper_id`、`report_url`、`related_urls`、`published_at_raw`、`date_precision`、`report_version`、`content_hash`、`fetch_status`。这些字段目前并未被网页索引完整建模，需要根据展示和筛选需求扩展数据库映射。

## 分阶段落地

**第一阶段：官方技术内容入库，建议实施。**

监测七家研究索引/博客、OpenAI RSS、Anthropic 透明度中心以及重点官方模型仓库。先采集元数据、可用短正文/摘要与附件地址，生成带证据的中文技术摘要；摘要明确主要创新、评测结论、适用场景和限制，厂商自报评测以「厂商报告」标识，保存评测配置而非只存排名。内容读取不足时只收录元数据，注明正文未取得。监测频率可复用现有四轮调度，附件下载和全文解析不必每轮重做。

Google 与 Seed 均建议纳入第一阶段：Google 优先监测 DeepMind 论文及博客，Google Research 作为主题筛选后的补充；Seed 优先监测中文研究入口与论文索引，英文版用于补全链接及内容，以同一论文/项目标识合并。两家元数据与摘要入库的可行性高，发现与正文采集适配难度为低到中等；长报告全文能力仍属于第二阶段。

公开链接、公开 RSS、公开仓库读取不要求购买各厂商模型 API。摘要生成可沿用现有 Agent 流程，成本取决于实际执行方式和所用模型，尚未测算。公共 GitHub API 有请求限额，应缓存并优先检查目标 README/论文文件的变更；不抓取每个 issue、star 或普通 commit。

验收应包括：每家至少一个真实样本进入隔离测试归档且可在问答中引用；同一 URL 重跑不新增重复；中文/英文及官网/论文/仓库材料正确关联；日期与首次发现日期分开；模拟单源失败时其他源继续；重复修改报告能识别版本而不覆盖证据。先试运行观察稳定性，再启用生产来源。

**第二阶段：报告全文研究，按需求实施。**

增加 PDF/HTML 正文清洗、章节与页级索引、附件缓存、修订记录和跨报告比较。扫描 PDF、公式与复杂表格可能需要 OCR 或多模态解析；不能仅依靠现有整篇文本截断。优先验证文本 PDF，并明确每条结论的章节或页码证据。按来源条款决定全文缓存和公开展示范围，网站可优先提供自写摘要、必要短摘录和原始链接。

现有服务部署包以日归档 JSON 为主要数据源，论文 PDF 与大批全文片段不宜无限放入日归档并随仓库一起发布；全文阶段需要单独评估附件存储与索引更新方式。

## 可扩展来源

- Qwen：[旧研究博客](https://qwenlm.github.io/blog/)已验证可访问，但页面提示迁往 `qwen.ai`；需要另行定位新站研究索引后接入，不宜只监测旧站并将历史条目当作最新发布。

最终建议：先接入七家官方技术内容的发现、分类、摘要和引用链路；报告全文研究作为独立增强。基础入库可复用现有架构，稳定覆盖和长报告精读需要上述专项适配。
