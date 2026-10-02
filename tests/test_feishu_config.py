import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def load_core(tmp_path, monkeypatch, env_text=None, environment=None):
    # Use a copied script and fake environment so tests never read local credentials.
    monkeypatch.setattr(os, "environ", dict(environment or {}))
    target = tmp_path / "run_ai_news.py"
    target.write_text((ROOT / "run_ai_news.py").read_text(encoding="utf-8"), encoding="utf-8")
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fake_feishu_core", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, target


def test_default_file_is_relative_to_script_and_quoted_values_work(tmp_path, monkeypatch):
    env = (
        "\ufeff# test-only configuration\n"
        "export FEISHU_APP_SECRET='fake secret # with spaces' # comment\n"
        'FEISHU_WEBHOOK="https://example.test/bot/v2/hook/fake#fragment" # comment\n'
        "PLAIN_URL=https://example.test/report#appendix # comment\n"
        "EMPTY= # comment\n"
    )
    monkeypatch.chdir(ROOT)
    core, _ = load_core(tmp_path, monkeypatch, env)
    assert core.APP_SECRET == "fake secret # with spaces"
    assert core.WEBHOOK == "https://example.test/bot/v2/hook/fake#fragment"
    assert os.environ["PLAIN_URL"] == "https://example.test/report#appendix"
    assert os.environ["EMPTY"] == ""


def test_environment_has_priority_including_explicit_empty_value(tmp_path, monkeypatch):
    core, _ = load_core(
        tmp_path, monkeypatch,
        "FEISHU_APP_SECRET=file-fake-secret\nFEISHU_WEBHOOK=https://example.test/file-fake\n",
        {"FEISHU_APP_SECRET": "environment-fake-secret", "FEISHU_WEBHOOK": ""},
    )
    assert core.APP_SECRET == "environment-fake-secret"
    assert core.WEBHOOK == ""


def test_missing_and_malformed_configuration_is_safe(tmp_path, monkeypatch):
    core, _ = load_core(tmp_path, monkeypatch)
    assert core.APP_SECRET == core.WEBHOOK == ""
    env = tmp_path / "test.env"
    env.write_text(
        "NOT AN ASSIGNMENT\nBAD-KEY=ignored\nBROKEN='unterminated\n"
        "TRAILING='quoted' extra\nVALUE=first\nVALUE=second\n"
        "LITERAL=$(never-execute)\nCOMMENT=# only a comment\n",
        encoding="utf-8",
    )
    core._load_local_env(env)
    assert os.environ == {"VALUE": "first", "LITERAL": "$(never-execute)", "COMMENT": ""}


def test_standalone_python_needs_only_standard_library(tmp_path, monkeypatch):
    _, target = load_core(
        tmp_path, monkeypatch,
        "FEISHU_APP_SECRET='stdlib fake secret'\nFEISHU_WEBHOOK=https://example.test/fake\n",
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import json,runpy,sys; m=runpy.run_path(sys.argv[1]); "
         "print(json.dumps([m['APP_SECRET'],m['WEBHOOK']]))", str(target)],
        cwd=tmp_path, env={}, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == ["stdlib fake secret", "https://example.test/fake"]


def test_safe_error_redacts_configured_and_request_credentials(tmp_path, monkeypatch):
    core, _ = load_core(
        tmp_path, monkeypatch,
        "FEISHU_APP_SECRET=fake-app-secret\n"
        "FEISHU_WEBHOOK=https://example.test/bot/v2/hook/fake-hook\n"
        "OPENAI_API_KEY=fake-model-key\n",
    )
    message = core.safe_error(
        "failed: fake-app-secret https://example.test/bot/v2/hook/fake-hook "
        "fake-model-key request-fake-token Bearer uncached-fake-token",
        secrets=("request-fake-token",),
    )
    assert message.startswith("failed:")
    for credential in ("fake-app-secret", "fake-hook", "fake-model-key", "request-fake-token", "uncached-fake-token"):
        assert credential not in message
    assert "[redacted]" in message


def test_safe_error_masks_unknown_webhook_and_preserves_empty_config(tmp_path, monkeypatch):
    core, _ = load_core(tmp_path, monkeypatch)
    assert core.safe_error("plain failure") == "plain failure"
    message = core.safe_error("POST https://open.feishu.cn/open-apis/bot/v2/hook/unknown-fake-hook HTTP 403")
    assert message == "POST [redacted webhook] HTTP 403"
