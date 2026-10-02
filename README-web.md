# 资讯之间 · 历史资讯研究助手

一个聊天窗口，加动态背景。通过 OpenAI Responses 协议的 Agent loop 检索、阅读历史归档并生成带来源的 Markdown 回答。公开访问，无登录，对话仅保留在页面内存，刷新即清空。不会触发采集、修改归档或发送飞书消息。

## 本地运行

需要 Python 3.12+ 和 Node.js 22。

```sh
python3 -m venv .venv-web
.venv-web/bin/pip install -r requirements-web.txt
npm --prefix web ci
npm --prefix web run build
test -f .env || cp .env.example .env
# 在 .env 中配置模型；已有 .env 会保留。
.venv-web/bin/uvicorn news_web.app:app --host 127.0.0.1 --port 8000
```

访问 http://127.0.0.1:8000 。未配置模型时，聊天会明确提示服务尚未连接。

开发前端时运行 `npm --prefix web run dev`，Vite 将 `/api` 转发到本机 8000 端口。

## 模型配置

后端读取环境变量或项目根目录 `.env`：

| 变量 | 含义 |
| --- | --- |
| `OPENAI_API_KEY` | 模型服务密钥，只能保存在后端 |
| `OPENAI_BASE_URL` | API 基础地址；应用在后面追加 `/responses`，不自行追加 `/v1` |
| `OPENAI_MODEL` | 服务提供的模型名称，须支持 Responses 流式响应和 function tools |
| `ARCHIVE_DIR` | 可选，归档目录，默认 `ai_news_daily` |
| `NEWS_DB_PATH` | 可选，SQLite 缓存位置，默认 `.news_web/index.sqlite3` |

兼容 OpenAI 官方服务及兼容 Responses 的中转服务，不使用 Chat Completions。工具使用扁平 function 定义、`function_call_output` 和 `call_id`，每轮保留响应输出项。所有请求 `store: false`。一个问题最多 6 轮模型请求、12 次本地工具调用，总超时 150 秒。

本地模型配置已按用户要求从 Codex 导入 `.env`。该文件被 Git 和 Docker 排除；部署时需要在托管平台单独配置，不能依赖本机 `.env`。

## Render 公网部署

1. 将网页代码、`web/package-lock.json`、`Dockerfile`、`render.yaml` 以及最新 `ai_news_daily/*.json` 提交到 GitHub。
2. 在 Render 选择 New → Blueprint，连接 `zhangzgen/zixun` 的 `main` 分支。Render 读取根目录 `render.yaml` 并构建 Docker 服务。
3. 在创建服务时填入 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。可从本地 `.env` 复制，密钥不要放进仓库。Render 控制台负责保管这些环境变量。
4. 部署完成后使用 Render 提供的 HTTPS 地址，任何人都能直接访问，无登录。
5. 后续 GitHub `main` 提交触发重新部署。归档通过仓库一起部署：日内采集任务在每次归档成功后提交并推送本轮归档，再按本次提交 SHA 核验 Render 部署。每日飞书发布任务也保留发布成功后的 GitHub 同步步骤。

选择免费实例可能遇到休眠和冷启动；需要持续响应时可在平台升级实例。SQLite 是可重建索引，不需要持久磁盘：启动时扫描部署包内的归档，之后按文件变化增量更新。原始 JSON 文件是数据来源。部署目录不会包含现有飞书发布脚本和配置。

公网服务已部署到 https://zixun-archive.onrender.com/ 。2026-10-02 已通过 GitHub deployments 确认 Render 自动部署成功，并验证公网健康检查及真实 Responses 问答。本机 Docker Hub 镜像拉取仍受网络影响。

若用 GitHub Actions 做其他部署，GitHub Secrets 供 Actions 使用；运行中的服务仍需从托管平台环境变量读取密钥。不要设置 `VITE_OPENAI_API_KEY`，它会进入浏览器构建产物。

## 检索和答案

- SQLite FTS5，中文采用字符二元组，英文采用前缀检索。多个关键词按 AND 匹配；复杂自然语言问题由 Agent 拆分关键词，分别查询同义词。
- Agent 可按**资讯发布时间**、来源筛选，搜索结果按 URL 去重；直接在问题里说明查询范围即可。
- 工具：`search_news`、`read_news`、`archive_stats`。工具只查询本地索引，无 shell、任意文件读取或联网抓取能力。
- 搜索结果会按 URL 合并去重，按发布时间排序，以带网站图标的标题链接列出。默认展示 3 条，超过 3 条时可展开全部或收起；完整保留所有已执行关键词搜索的匹配记录，不截断前 20 条。模型仅收到有限条摘要预览，避免过大的上下文，查找类回答简短说明检索范围或结果情况，不重复链接清单。
- Agent 会用中英文关键词和同义表述补充检索。链接清单反映已执行搜索的匹配范围，不代表已穷尽所有语义相关事件。
- 网站图标从原站 `/favicon.ico` 加载，失败时显示域名首字母。答案中的 N1 等引用也能直接打开原始来源；复制回答包含完整结果清单及原始链接。
- 有些记录只有摘要，工具与提示词明确区分摘要和正文；模型未检索到证据时应说明范围或无结果。
- 不使用 localStorage、sessionStorage 或服务器会话存储。当前页面内可连续追问，刷新或点击「新对话」即清空。
- 未使用登录或账户系统。输入长度、检索轮数和并发上限属于运行边界。

## 验证

```sh
.venv-web/bin/pip install -r requirements-web-test.txt
.venv-web/bin/python -m pytest tests/test_news_web.py
npm --prefix web run build
```

测试使用隔离的临时归档和模拟 Responses SSE，涵盖中文搜索、日期边界、跨日去重、索引更新、引用回传、工具循环、超过单页限制的全量结果及错误处理，不使用真实 API Key。

协议参考：[工具调用](https://developers.openai.com/api/docs/guides/function-calling)、[流式 Responses](https://developers.openai.com/api/docs/guides/streaming-responses)。
