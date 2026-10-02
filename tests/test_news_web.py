import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from news_web.agent import ResearchAgent
from news_web.app import create_app
from news_web.archive_store import ArchiveStore
from news_web.config import Settings


def write_archive(root, day, items):
    root.mkdir(exist_ok=True)
    (root / (day + ".json")).write_text(json.dumps({"items": items}, ensure_ascii=False))


def news(uid, title, url, published, source="研究源", summary="可核验的归档摘要"):
    return {"archive_id": uid, "资讯标题": title, "超链接": url, "发布时间": published, "来源": source, "desc": summary}


@pytest.fixture
def indexed(tmp_path):
    archive = tmp_path / "archive"
    write_archive(archive, "2026-09-29", [
        news("a1", "Agent 长期记忆系统", "https://example.com/memory", "2026-09-29 08:00:00"),
        news("a2", "机器人新进展", "https://example.com/robot", "2026-09-29 23:59:59"),
        news("unsafe", "无效地址", "javascript:alert(1)", "2026-09-29 10:00:00"),
    ])
    write_archive(archive, "2026-09-30", [
        news("a3", "Agent 长期记忆系统", "https://example.com/memory", "2026-09-29 08:00:00", summary="后续补充的归档摘要"),
        news("a4", "Agentic 工具调用", "https://example.com/tools", "2026-09-30 00:00:00", source="另一来源"),
    ])
    store = ArchiveStore(archive, tmp_path / "index.sqlite3")
    store.refresh(force=True)
    settings = Settings(archive, tmp_path / "index.sqlite3", tmp_path / "dist", api_key="private-test-key", model="test-model", base_url="https://model.test/v1")
    return store, settings


@pytest.fixture
def many_indexed(tmp_path):
    archive = tmp_path / "archive"
    write_archive(archive, "2026-09-29", [
        news("old-memory", "Memory 记忆研究旧版", "https://example.com/memory/0", "2026-09-29 08:00:00", source="旧来源"),
    ])
    write_archive(archive, "2026-09-30", [
        news(f"memory-{number}", f"Agent memory 记忆研究 {number}", f"https://example.com/memory/{number}", f"2026-09-30 00:{number:02d}:00")
        for number in range(36)
    ])
    store = ArchiveStore(archive, tmp_path / "index.sqlite3")
    store.refresh(force=True)
    settings = Settings(archive, tmp_path / "index.sqlite3", tmp_path / "dist", api_key="private-test-key", model="test-model", base_url="https://model.test/v1")
    return store, settings


def test_chinese_search_dedup_and_date_boundaries(indexed):
    store, _ = indexed
    result = store.search("Agent 记忆")
    assert result["total"] == 1
    assert result["items"][0]["id"] == "a3"
    assert result["items"][0]["summary"] == "后续补充的归档摘要"
    assert store.search("机器人", end_date="2026-09-29")["total"] == 1
    assert store.search(start_date="2026-09-30")["total"] == 1
    assert store.search("Agent")["total"] == 2  # English prefix includes Agentic.
    assert store.search('" OR * --')["total"] == 0
    assert store.stats()["record_count"] == 4
    assert store.stats()["article_count"] == 3
    with pytest.raises(ValueError):
        store.search(start_date="2026-09-30", end_date="2026-09-29")


def test_search_can_return_all_matches_above_page_limit(many_indexed):
    store, _ = many_indexed
    result = store.search("记忆", limit=None)
    assert result["total"] == 36
    assert result["limit"] is None
    assert len(result["items"]) == 36
    assert len({item["url"] for item in result["items"]}) == 36
    assert [item["id"] for item in result["items"]] == [f"memory-{number}" for number in reversed(range(36))]
    # Ordinary paginated callers retain their existing bounds.
    assert len(store.search("记忆", limit=100)["items"]) == 30


def test_refresh_replaces_fts_and_preserves_bad_archive(indexed):
    store, _ = indexed
    original = (store.archive_dir / "2026-09-29.json").read_bytes()
    write_archive(store.archive_dir, "2026-09-30", [news("a4", "算力更新", "https://example.com/tools", "2026-09-30 00:00:00")])
    store.refresh(force=True)
    assert store.search("工具")["total"] == 0
    assert store.search("算力")["total"] == 1
    assert (store.archive_dir / "2026-09-29.json").read_bytes() == original
    (store.archive_dir / "2026-09-30.json").write_text("{")
    store.refresh(force=True)
    assert store.search("算力")["total"] == 1
    assert store.stats()["warnings"]
    (store.archive_dir / "2026-09-30.json").unlink()
    store.refresh(force=True)
    assert store.search("算力")["total"] == 0


def sse(*events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)


def complete(output):
    return {"type": "response.completed", "response": {"output": output}}


def call(name, args, number):
    return {"type": "function_call", "id": "fc_" + str(number), "call_id": "call_" + str(number), "name": name, "arguments": json.dumps(args)}


def test_responses_loop_reads_evidence_and_preserves_output(indexed):
    store, settings = indexed
    requests = []
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False
        if len(requests) == 1:
            assert body["tool_choice"] == "required"
            output = [reasoning, call("search_news", {"query": "记忆", "start_date": None, "end_date": None, "source": None, "limit": 5}, 1)]
            return httpx.Response(200, text=sse(complete(output)))
        if len(requests) == 2:
            assert reasoning in body["input"]
            observation = json.loads(body["input"][-1]["output"])
            assert observation["items"][0]["citation"] == "N1"
            assert body["input"][-1]["call_id"] == "call_1"
            return httpx.Response(200, text=sse(complete([call("read_news", {"ids": ["a3"]}, 2)])))
        observation = json.loads(body["input"][-1]["output"])
        assert observation["items"][0]["content_kind"] == "摘要"
        text = "## 检索结果\n依据归档摘要，Agent 记忆系统有更新。[N1](news:N1)"
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": text}, complete([])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            agent = ResearchAgent(store, settings, client)
            return [event async for event in agent.run("查询记忆", [], {})]
    events = asyncio.run(run())
    assert len(requests) == 3
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["tool_calls"] == 2
    assert events[-1]["data"]["sources"][0]["url"] == "https://example.com/memory"
    assert any(event["event"] == "delta" and "news:N1" in event["data"]["text"] for event in events)


def test_search_preview_is_bounded_but_stream_displays_all_matches(many_indexed):
    store, settings = many_indexed
    args = {"query": "记忆", "start_date": None, "end_date": None, "source": None, "limit": 1}
    result = ResearchAgent(store, settings).execute("search_news", args, {})
    assert result["total"] == 36
    assert result["displayed_count"] == 36
    assert result["returned_count"] == 1
    assert len(result["items"]) == 1
    assert len(result["_matches"]) == 36
    assert all({"id", "title", "url", "source", "published_at"} <= set(item) for item in result["_matches"])
    assert all("summary" not in item and "content" not in item for item in result["_matches"])
    assert len(ResearchAgent(store, settings).execute("search_news", {**args, "limit": 100}, {})["items"]) == 20
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, text=sse(complete([call("search_news", args, 1)])))
        observations = [json.loads(item["output"]) for item in body["input"] if item.get("type") == "function_call_output"]
        assert len(observations) == 1
        observation = observations[0]
        assert observation["total"] == 36
        assert observation["displayed_count"] == 36
        assert observation["returned_count"] == 1
        assert len(observation["items"]) == 1
        assert observation["items"][0]["citation"] == "N1"
        assert "_matches" not in observation
        assert "memory-0" not in json.dumps(observation)
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "已找到相关资讯。"}, complete([])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in ResearchAgent(store, settings, client).run("查找记忆研究", [], {})]

    events = asyncio.run(run())
    assert len(requests) == 2
    result_events = [event for event in events if event["event"] == "results"]
    assert result_events
    matches = result_events[-1]["data"]["items"]
    assert len(matches) == 36
    assert {item["url"] for item in matches} == {f"https://example.com/memory/{number}" for number in range(36)}
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["results"] == matches
    assert "_matches" not in json.dumps(events)


def test_multiple_searches_merge_by_url_and_keep_latest_result(many_indexed):
    store, settings = many_indexed
    requests = []
    queries = [
        {"query": "记忆", "start_date": None, "end_date": None, "source": "旧来源", "limit": 1},
        {"query": "memory", "start_date": None, "end_date": None, "source": None, "limit": 1},
    ]

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) <= len(queries):
            return httpx.Response(200, text=sse(complete([call("search_news", queries[len(requests) - 1], len(requests))])))
        observations = [json.loads(item["output"]) for item in body["input"] if item.get("type") == "function_call_output"]
        assert [item["total"] for item in observations] == [1, 36]
        assert all(len(item["items"]) == 1 and "_matches" not in item for item in observations)
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "已合并中英文检索结果。"}, complete([])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in ResearchAgent(store, settings, client).run("记忆 memory 研究", [], {})]

    events = asyncio.run(run())
    assert len(requests) == 3
    result_events = [event for event in events if event["event"] == "results"]
    assert result_events[0]["data"]["items"][0]["id"] == "old-memory"
    assert events[-1]["event"] == "done"
    matches = events[-1]["data"]["results"]
    assert len(matches) == 36
    assert len({item["url"] for item in matches}) == 36
    assert next(item for item in matches if item["url"] == "https://example.com/memory/0")["id"] == "memory-0"
    assert [item["published_at"] for item in matches] == sorted((item["published_at"] for item in matches), reverse=True)
    assert result_events[-1]["data"]["items"] == matches


def test_stream_keeps_news_and_report_summary_at_same_url(indexed, tmp_path):
    store, settings = indexed
    reports = tmp_path / 'reports'
    item = news('report-memory', '长期记忆技术报告', 'https://example.com/memory',
                '2026-09-29 00:00:00', source='Kimi 技术报告', summary='全文阅读后的结论')
    item.update(content='金字塔总结', content_kind='技术报告总结')
    write_archive(reports, '2026-09-29', [item])
    store.report_archive_dir = reports
    store.refresh(force=True)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text=sse(complete([call('search_news', {'query': '记忆'}, 1)])))
        return httpx.Response(200, text=sse({'type': 'response.output_text.delta', 'delta': '已找到资讯和技术报告总结。'}, complete([])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in ResearchAgent(store, settings, client).run('记忆技术报告', [], {})]

    matches = asyncio.run(run())[-1]['data']['results']
    assert len(matches) == 2
    assert {item['content_kind'] for item in matches} == {'摘要', '技术报告总结'}
    assert len({item['url'] for item in matches}) == 1


def test_distinct_reports_sharing_release_page_survive_search_stats_and_stream(indexed, tmp_path):
    store, settings = indexed
    reports = tmp_path / 'reports'
    items = []
    for uid, title in [('report-framework', '长期记忆安全框架'), ('report-roadmap', '长期记忆安全路线图')]:
        item = news(uid, title, 'https://example.com/memory', '2026-09-29 00:00:00', source='Google 技术报告')
        item.update(content='独立报告的金字塔总结', content_kind='技术报告总结')
        items.append(item)
    write_archive(reports, '2026-09-29', items)
    store.report_archive_dir = reports
    store.refresh(force=True)
    assert {item['id'] for item in store.search('记忆', limit=None)['items']} == {'a3', 'report-framework', 'report-roadmap'}
    assert store.stats()['article_count'] == 5  # Three news URLs and two report IDs.
    assert next(s['count'] for s in store.stats()['sources'] if s['name'] == 'Google 技术报告') == 2
    requests = []
    queries = [{'query': '记忆', 'source': 'Google 技术报告'}, {'query': '记忆'}, {'query': '记忆', 'source': 'Google 技术报告'}]

    def handler(request):
        requests.append(request)
        if len(requests) <= len(queries):
            return httpx.Response(200, text=sse(complete([call('search_news', queries[len(requests) - 1], len(requests))])))
        return httpx.Response(200, text=sse({'type': 'response.output_text.delta', 'delta': '已找到两份独立报告和一条资讯。'}, complete([])))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in ResearchAgent(store, settings, client).run('查找安全报告', [], {})]

    done = asyncio.run(run())[-1]
    assert done['event'] == 'done'
    assert {item['id'] for item in done['data']['results']} == {'a3', 'report-framework', 'report-roadmap'}
    assert len(done['data']['results']) == 3


@pytest.mark.parametrize("status,body", [(401, "secret provider body private-test-key"), (200, sse({"type": "response.output_text.delta", "delta": "partial"}))])
def test_provider_errors_and_incomplete_stream_never_finish(indexed, status, body):
    store, settings = indexed
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body))) as client:
            return [event async for event in ResearchAgent(store, settings, client).run("问题", [], {})]
    events = asyncio.run(run())
    assert events[-1]["event"] == "error"
    assert "private-test-key" not in json.dumps(events)
    assert not any(event["event"] == "done" for event in events)


def test_hard_filters_unknown_tools_and_read_bounds(indexed):
    store, settings = indexed
    agent = ResearchAgent(store, settings)
    assert agent.execute("search_news", {"query": "", "start_date": None, "end_date": None, "limit": 5}, {"source": "另一来源"})["total"] == 1
    assert agent.execute("read_news", {"ids": ["a3"]}, {"start_date": "2026-09-30"})["items"] == []
    assert "error" in agent.execute("run_shell", {"command": "rm -rf /"}, {})
    with pytest.raises(ValueError):
        agent.execute("read_news", {"ids": "a3"}, {})


def test_loop_forces_final_answer_after_tool_budget(indexed):
    store, original = indexed
    from dataclasses import replace
    settings = replace(original, max_rounds=3, max_tool_calls=1)
    payloads = []
    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(200, text=sse(complete([call("archive_stats", {}, 1)])))
        assert body["tool_choice"] == "none"
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "归档有 2 天。"}, complete([])))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [event async for event in ResearchAgent(store, settings, client).run("覆盖范围", [], {})]
    assert asyncio.run(run())[-1]["event"] == "done"
    assert len(payloads) == 2


def test_public_api_and_missing_model(indexed):
    store, original = indexed
    from dataclasses import replace
    settings = replace(original, api_key="")
    settings.static_dir.mkdir()
    (settings.static_dir / "index.html").write_text("<h1>Archive</h1>")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json() == {"ok": True, "model_ready": False}
        assert app.state.store.stats()["article_count"] == 3
        assert client.post("/api/chat", json={"message": "记忆", "filters": {"start_date": "2026-09-31"}}).status_code == 422
        assert client.post("/api/chat", json={"message": "记忆", "filters": {"start_date": "2026-09-30", "end_date": "2026-09-29"}}).status_code == 422
        assert client.get("/.env").status_code == 404
        assert client.get("/api/missing").status_code == 404
        assert client.post("/api/chat", json={"message": " ", "history": []}).status_code == 422
        result = client.post("/api/chat", json={"message": "记忆"})
        assert result.status_code == 200
        assert "event: error" in result.text
        assert "event: done" not in result.text
