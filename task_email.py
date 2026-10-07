#!/usr/bin/env python3
"""Send one local automation execution report via QQ SMTP, after all steps finish."""

import argparse
import hashlib
import html
import json
import os
import smtplib
import ssl
import sys
import uuid
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / ".task_reports"
ZONE = timezone(timedelta(hours=8))
LABELS = {
    "input_count": "本轮输入", "added_count": "新增归档", "updated_count": "更新归档",
    "skipped_duplicate_count": "重复跳过", "archive_count": "当天归档总数",
    "published_count": "飞书发布条数", "chat_count": "群聊摘要条数",
    "report_added_count": "新增技术报告", "report_revised_count": "修订技术报告",
    "report_reading_completed_count": "补齐全文阅读", "report_summary_updated_count": "总结更新",
    "report_changed_vendor_count": "有归档更新的厂商",
    "framework_source_count": "框架来源数", "framework_source_success_count": "采集成功来源",
    "framework_update_count": "技术更新数", "framework_changed_count": "有更新的框架",
    "framework_pending_count": "待处理更新", "framework_excluded_count": "非技术内容排除",
}
STATUSES = {"success": "成功", "partial": "部分完成", "failed": "失败", "skipped": "跳过"}


def now():
    return datetime.now(ZONE).isoformat(timespec="seconds")


def config():
    values = dict(dotenv_values(ROOT / ".env"))
    values.update(os.environ)
    return values


def redact(value, values):
    """Remove configured secrets even if an upstream error accidentally includes them."""
    text = str(value)
    secrets = [str(v) for k, v in values.items() if v and
               any(part in k.upper() for part in ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "WEBHOOK"))]
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    return text


def validate(report):
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    for key in ("run_id", "task", "status", "started_at", "finished_at", "summary"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f"missing report field: {key}")
    if report["status"] not in STATUSES:
        raise ValueError("invalid status")
    start = datetime.fromisoformat(report["started_at"])
    end = datetime.fromisoformat(report["finished_at"])
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("timestamps must include timezone and be ordered")
    if not isinstance(report.get("counts", {}), dict):
        raise ValueError("counts must be an object")
    if not isinstance(report.get("errors", []), list):
        raise ValueError("errors must be an array")


def render(report, values):
    validate(report)
    start = datetime.fromisoformat(report["started_at"])
    end = datetime.fromisoformat(report["finished_at"])
    lines = [
        ("任务", report["task"]), ("结果", STATUSES[report["status"]]),
        ("开始时间", start.astimezone(ZONE).strftime("%Y-%m-%d %H:%M:%S +08:00")),
        ("结束时间", end.astimezone(ZONE).strftime("%Y-%m-%d %H:%M:%S +08:00")),
        ("耗时", f"{int((end-start).total_seconds())} 秒"), ("执行摘要", report["summary"]),
    ]
    for key, value in report.get("counts", {}).items():
        lines.append((LABELS.get(key, key), value))
    if 'technical_report_updates' in report:
        updates = report['technical_report_updates']
        kinds = {'new': '新增', 'revised': '原文修订',
                 'reading_completed': '补齐全文阅读', 'summary_updated': '总结更新'}
        any_updates = False
        for vendor, items in updates.items():
            if not items:
                continue
            any_updates = True
            entries = []
            for item in items:
                state = '已全文阅读' if item['read_status'] == 'full_text_read' else '待完整阅读'
                entry = f"{kinds[item['kind']]} · {item['title']}（{state}）\n原文：{item['original_url']}"
                if item.get('feishu_url'):
                    entry += '\n飞书：' + item['feishu_url']
                entries.append(entry)
            lines.append((vendor + ' 技术报告', '\n\n'.join(entries)))
        if not any_updates:
            lines.append(('厂商报告更新', '本轮归档未记录新增、修订、阅读补齐或总结更新。采集覆盖及失败情况见下方数据源情况。'))
    if 'framework_updates' in report:
        updates = report['framework_updates']
        for framework, items in updates.items():
            entries = []
            for item in items:
                entry = f"{item['title']}\n技术变化：{item['what_changed']}\n用途：{item['usefulness']}\n原文：{item['url']}"
                if item.get('feishu_url'):
                    entry += '\n飞书：' + item['feishu_url']
                entries.append(entry)
            lines.append((framework, '\n\n'.join(entries)))
        if not any(updates.values()):
            lines.append(('框架技术更新', '本轮未发现可确认的新增技术更新；来源失败或待处理情况见下方。'))
    details = (("source_status", "数据源情况"), ("feishu", "飞书发布"))
    if report.get('report_type') != 'agent_frameworks':
        details += (("github", "GitHub 同步"), ("render", "Render 部署"))
    for key, label in details:
        if key == 'source_status' and isinstance(report.get(key), list):
            sources = []
            for source in report[key]:
                if isinstance(source, dict) and all(field in source for field in
                                                    ('framework', 'name', 'status', 'candidate_count')):
                    value = f"{source['framework']} / {source['name']}：{'成功' if source['status'] == 'success' else '异常'}（{source['candidate_count']}个候选）"
                    if source.get('baseline_note'):
                        value += '；' + str(source['baseline_note'])
                else:
                    # Technical reports use vendor records; other tasks may use strings.
                    value = json.dumps(source, ensure_ascii=False) if isinstance(source, dict) else str(source)
                sources.append(value)
            lines.append((label, '\n'.join(sources) or '未提供来源结果，覆盖情况未核验'))
            continue
        lines.append((label, report.get(key, "未执行或未核验")))
    lines.append(("错误 / 异常", "\n".join(str(e) for e in report.get("errors", [])) or "无"))
    lines.append(("执行编号", report["run_id"]))
    cleaned = [(label, redact(value, values)) for label, value in lines]
    title = redact(f"[资讯任务·{STATUSES[report['status']]}] {report['task']} · "
                   f"{end.astimezone(ZONE):%m-%d %H:%M}", values).replace("\r", " ").replace("\n", " ")
    plain = "\n\n".join(f"{label}：{value}" for label, value in cleaned)
    rows = "".join(f'<tr><td style="padding:12px;color:#64748b;vertical-align:top;width:110px">'
                   f'{html.escape(label)}</td><td style="padding:12px;white-space:pre-wrap;word-break:break-word">'
                   f'{html.escape(value)}</td></tr>' for label, value in cleaned)
    rich = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body '
            'style="margin:0;padding:24px;background:#f1f5f9;font-family:Arial,sans-serif;color:#0f172a">'
            '<div style="max-width:680px;margin:auto;background:#fff;border-radius:16px;padding:24px">'
            f'<h2 style="margin:0 0 20px">{html.escape(title)}</h2>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table></div></body></html>')
    return title, plain, rich


def send_report(report, values, receipt_dir=REPORT_DIR / "sent", smtp_factory=smtplib.SMTP_SSL):
    title, plain, rich = render(report, values)
    sender = (values.get("SMTP_USER") or "").strip()
    recipient = (values.get("SMTP_TO") or sender).strip()
    password = values.get("SMTP_PASSWORD") or ""
    for address in (sender, recipient):
        if "@" not in address or any(c in address for c in "\r\n,;<>"):
            raise ValueError("SMTP_USER/SMTP_TO must be a single email address")
    if not password:
        raise ValueError("SMTP_PASSWORD is missing")
    identity = hashlib.sha256(f"{report['run_id']}:{recipient}".encode()).hexdigest()
    receipt = receipt_dir / f"{identity}.json"
    if receipt.exists():
        return {"ok": True, "email": "already_sent", "run_id": report["run_id"]}
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(plain)
    message.add_alternative(rich, subtype="html")
    client = smtp_factory(values.get("SMTP_HOST") or "smtp.qq.com",
                          int(values.get("SMTP_PORT") or 465),
                          timeout=30, context=ssl.create_default_context())
    try:
        client.login(sender, password)
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTP recipient refused")
    finally:
        # QQ may return an abnormal QUIT response after accepting DATA; close directly.
        client.close()
    receipt_dir.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps({"run_id": report["run_id"], "sent_at": now(),
                                     "message_id": message["Message-ID"]}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(receipt)
    return {"ok": True, "email": "accepted_by_smtp", "run_id": report["run_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", type=Path, help="This run's freshly generated summary JSON.")
    group.add_argument("--test", action="store_true", help="Send a configuration verification email.")
    parser.add_argument("--dry-run", action="store_true", help="Validate without sending or printing credentials.")
    args = parser.parse_args()
    values = config()
    if args.test:
        stamp = now()
        report = {"run_id": str(uuid.uuid4()), "task": "邮件通知配置测试", "status": "success",
                  "started_at": stamp, "finished_at": stamp,
                  "summary": "QQ 邮箱通知已接入。后续每次采集或飞书推送任务结束后，发送本轮执行摘要；成功、失败及跳过均通知。此邮件仅测试邮件通道，没有运行采集、飞书发布或部署。",
                  "errors": []}
    else:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    if args.dry_run:
        render(report, values)
        result = {"ok": True, "email": "dry_run", "run_id": report["run_id"]}
    else:
        result = send_report(report, values)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # SMTP exceptions may include server details; never print raw exception contents.
        print(json.dumps({"ok": False, "email": "failed", "error_type": type(exc).__name__,
                          "smtp_code": getattr(exc, "smtp_code", None)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
