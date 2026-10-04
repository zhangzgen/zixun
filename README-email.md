# 定时任务邮件摘要

三个本机 Codex 定时任务在本轮操作完成后各发一封邮件：日内采集 09:00、12:00、15:00、18:00，每日飞书推送 08:30，技术报告采集归档每天00:00（Asia/Shanghai）。邮件包含起止时间、耗时、执行摘要、数量、数据源异常、飞书结果、GitHub 同步和 Render 状态。成功、部分完成、失败和无候选/无更新都发送。

邮件收尾由实际定时任务的 prompt 和工作流共同要求，覆盖 Agent 筛选及部署步骤，不在 archive_ai_news.py/publish_ai_news.py 中提前发送。手动单独运行业务脚本不会自动发摘要。Codex 进程被强制终止、电脑关机或任务未启动时，收尾无法执行；这不是独立的存活监控。

## 本机配置

在项目现有 `.env` 中保留原配置并添加：

```env
SMTP_USER=your-address@qq.com
SMTP_PASSWORD=QQ邮箱SMTP授权码
# 可选，默认发给自己
# SMTP_TO=your-address@qq.com
# SMTP_HOST=smtp.qq.com
# SMTP_PORT=465
```

授权码来自 QQ 邮箱的 SMTP 服务设置，不是登录密码。配置只用于本机定时任务，无需上传 GitHub Secrets 或 Render；`.env` 和 `.task_reports/` 被 Git 忽略，Docker 也不会包含它们。脚本通过 SSL SMTP 发送，环境变量优先于 `.env`。使用已有 `.venv-web` 环境的 python-dotenv，发邮件本身使用 Python 标准库。

## 本轮报告

开始执行前生成新的 UUID 和带时区时间。最终写入 `.task_reports/<run_id>.json`，例如：

```json
{
  "run_id": "new-uuid-for-this-run",
  "task": "每日 AI 资讯日内采集归档",
  "status": "partial",
  "started_at": "2026-10-02T09:00:00+08:00",
  "finished_at": "2026-10-02T09:05:00+08:00",
  "summary": "已追加归档并同步 GitHub，Render 尚在构建。",
  "counts": {
    "input_count": 10,
    "added_count": 3,
    "updated_count": 0,
    "skipped_duplicate_count": 7,
    "archive_count": 35
  },
  "source_status": "仅填写本轮来源实际结果及补充采集情况",
  "feishu": "不适用",
  "github": "填写实际推送状态、分支和 SHA",
  "render": "填写该 SHA 的实际部署状态；无确认时填未确认",
  "errors": []
}
```

这是格式示例，不是实际执行报告。`status` 可为 `success`（成功）、`partial`（部分完成）、`failed`（失败）、`skipped`（跳过）。飞书推送的数量使用 `published_count`、`chat_count`，`feishu` 包含实际发布状态和文档链接。未知数量不填，未执行/未核验的步骤明确说明；不要用旧结果文件冒充本轮成功。开始、结束时间必须带时区。

```sh
.venv-web/bin/python task_email.py --report .task_reports/<run_id>.json
# 仅校验，不发邮件
.venv-web/bin/python task_email.py --report .task_reports/<run_id>.json --dry-run
# 发一封配置测试邮件，不运行采集或飞书发布
.venv-web/bin/python task_email.py --test
```

邮件包含纯文本和 HTML 两个版本。脚本对 `.env`/环境变量中配置的密钥、授权码、token 和 webhook 做值替换脱敏；报告生成方仍须只写脱敏摘要，不能写原始凭据或完整日志。

技术报告任务先运行`.venv-web/bin/python -m technical_reports.task_report begin`，仅在本机保存采集前的归档快照和新run_id。业务与同步步骤完成后，Agent在返回的report路径中填写真实最终status、summary及各步骤状态，再运行`finish --report .task_reports/<run_id>.json`。该步骤根据采集前后差异写入`technical_report_updates`，按厂商列出新增、原文修订、补齐全文阅读及总结更新的报告标题、链接和阅读状态。只改检查时间、飞书链接或渲染格式不计作报告修订。邮件将这些清单显示为独立厂商条目；无归档变化也明确说明，来源失败仍单独列出。快照缺失时报告标为变更未核验，不能编造历史变更或无更新。

SMTP 接收成功后保存本机发送凭据，同一个 run_id 和收件人再次运行会返回 `already_sent`。`accepted_by_smtp` 表示邮件服务器接受发送，不等于已进入收件箱。失败时只输出错误类型和 SMTP 错误码，保留报告，不自动重试；连接中断等情况可能无法确认服务器是否已接收，不应换 run_id 重复发送。

离线验证：`.venv-web/bin/python -m pytest tests/test_task_email.py -q`。
