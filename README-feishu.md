# 飞书发布配置

每日要闻与技术报告共用 `run_ai_news.py` 中的飞书客户端。应用密钥和群机器人地址从项目根目录 `.env` 或环境变量读取，配置如下：

```dotenv
FEISHU_APP_SECRET='填入飞书应用的 App Secret'
FEISHU_WEBHOOK='填入群自定义机器人的完整 webhook 地址'
```

在已有 `.env` 中补充这两项并保留其他配置。`.env` 被 Git 和 Docker 排除；本机建议运行 `chmod 600 .env`。部署到其他机器时单独配置凭据。已导出的环境变量优先于 `.env`，包括显式设置的空值；支持带引号的值、`export KEY=value` 和行尾注释，URL 中的 `#fragment` 会保留。

共享客户端使用 Python 标准库读取 `.env`。每日发布统一使用`.venv-web/bin/python`运行prepare_ai_news_publish.py、get_news_details.py和publish_ai_news.py，确保技术报告模板的mistune依赖可用。技术报告的模型与PDF依赖按`README-technical-reports.md`安装。

当前源码已不包含上述凭据；旧 Git 提交中出现过的凭据需要在飞书控制台轮换，再更新本地 `.env`。配置测试仅使用临时假凭据，不访问飞书：

```sh
.venv-web/bin/python -m pytest tests/test_feishu_config.py -q
```
