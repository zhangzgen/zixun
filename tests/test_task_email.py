from email import policy
from email.parser import BytesParser
import pytest

from task_email import render, send_report


def report():
    return {"run_id": "example-run", "task": "日内采集", "status": "partial",
            "started_at": "2026-10-02T09:00:00+08:00", "finished_at": "2026-10-02T09:03:00+08:00",
            "summary": "新增归档成功，部署仍在构建", "counts": {"added_count": 3},
            "github": "已推送 abc123", "render": "构建中", "errors": ["source <unavailable>"]}


VALUES = {"SMTP_USER": "example@qq.com", "SMTP_PASSWORD": "smtp-test-secret",
          "OPENAI_API_KEY": "model-test-secret"}


def test_render_status_and_sanitization():
    data = report()
    data["errors"] += ["SMTP smtp-test-secret; Authorization model-test-secret"]
    title, plain, rich = render(data, VALUES)
    assert "部分完成" in title and "180 秒" in plain and "新增归档：3" in plain
    assert "构建中" in rich and "source &lt;unavailable&gt;" in rich
    for secret in ("smtp-test-secret", "model-test-secret"):
        assert secret not in plain + rich + title


def test_no_stale_or_incomplete_result():
    with pytest.raises(ValueError):
        render({"ok": True, "date": "2026-10-01"}, VALUES)
    data = report()
    data["finished_at"] = "2026-10-02T08:59:00+08:00"
    with pytest.raises(ValueError):
        render(data, VALUES)


def test_send_multipart_and_skip_delivered_run(tmp_path):
    messages = []
    class SMTP:
        def __init__(self, host, port, timeout, context):
            assert (host, port, timeout) == ("smtp.qq.com", 465, 30)
        def login(self, user, password):
            assert user == VALUES["SMTP_USER"] and password == VALUES["SMTP_PASSWORD"]
        def send_message(self, message):
            messages.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))
            return {}
        def close(self):
            pass
    assert send_report(report(), VALUES, tmp_path, SMTP)["email"] == "accepted_by_smtp"
    assert send_report(report(), VALUES, tmp_path, SMTP)["email"] == "already_sent"
    assert len(messages) == 1
    assert messages[0]["To"] == "example@qq.com"
    assert messages[0].get_body(preferencelist=("html",)) is not None
    assert "构建中" in messages[0].get_body(preferencelist=("plain",)).get_content()


def test_auth_failure_leaves_no_receipt_and_closes(tmp_path):
    closed = []
    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *args):
            raise RuntimeError("authentication rejected")
        def close(self):
            closed.append(True)
    with pytest.raises(RuntimeError):
        send_report(report(), VALUES, tmp_path, SMTP)
    assert closed and not list(tmp_path.iterdir())
