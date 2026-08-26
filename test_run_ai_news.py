import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import archive_ai_news as archive
import get_news_details as details
import prepare_ai_news_publish as prepare
import run_ai_news as news


def readhub_page(records):
    chunk = '13:["$",{"state":{"data":{"pages":[{"data":{"items":' + json.dumps(
        records, ensure_ascii=False, separators=(",", ":")
    ) + '}}]}}}]'
    return '<script>self.__next_f.push([1,' + json.dumps(chunk, ensure_ascii=False) + '])</script>'


def jiqizhixin_rss():
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>机器之心</title>
    <item>
      <title>世界模型取得新进展</title>
      <link>https://www.jiqizhixin.com/articles/2026-07-12-1</link>
      <pubDate>Sun, 12 Jul 2026 14:30:00 +0800</pubDate>
      <description>简短摘要</description>
      <content:encoded><![CDATA[<p>这是来自官方 RSS 的完整正文，包含足够多的内容用于摘要，并且不需要再次访问可能只返回登录导航壳的网页详情。正文还包含模型能力、实验结果、行业影响和后续计划等信息，足以支持 Agent 判断资讯价值并生成可靠摘要。</p>]]></content:encoded>
    </item>
  </channel>
</rss>"""


class ReadhubCollectorTest(unittest.TestCase):
    def test_parse_readhub_items_decodes_next_flight_data(self):
        records = [{
            "title": "模型发布",
            "summary": "包含引号“测试”的摘要",
            "url": "https://example.com/news/1",
            "createdAt": "2026-07-12T07:05:35.759Z",
            "publishDate": "2026-07-12T06:36:57.000Z",
        }]

        self.assertEqual(news.parse_readhub_items(readhub_page(records)), records)

    def test_collect_readhub_uses_ai_channel_detail_url_and_publish_date(self):
        now = datetime(2026, 7, 12, 15, 0, tzinfo=news.TZ)
        records = [{
            "title": "世界模型评估新进展",
            "summary": "正文摘要",
            "url": "https://example.com/news/world-model",
            "createdAt": "2026-07-12T07:05:35.759Z",
            "publishDate": "2026-07-12T06:36:57.000Z",
        }]
        items = []

        with mock.patch.object(news, "fetch", return_value=readhub_page(records)) as fetch_mock, \
             mock.patch.object(news, "NOW", now), \
             mock.patch.object(news, "SINCE", now - timedelta(hours=24)):
            news.collect_readhub(items)

        fetch_mock.assert_called_once_with("https://readhub.cn/news/ai")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["超链接"], records[0]["url"])
        self.assertEqual(items[0]["发布时间"], "2026-07-12 14:36:57")
        self.assertEqual(items[0]["desc"], records[0]["summary"])


class JiqizhixinCollectorTest(unittest.TestCase):
    def test_collect_jiqizhixin_prefers_rss_and_keeps_full_content(self):
        now = datetime(2026, 7, 12, 15, 0, tzinfo=news.TZ)
        items = []
        with mock.patch.object(news, "load_jiqizhixin_rss_token", return_value="test-token"), \
             mock.patch.object(news, "load_jiqizhixin_rss_cache", return_value=""), \
             mock.patch.object(news, "save_jiqizhixin_rss_cache"), \
             mock.patch.object(news, "fetch", return_value=jiqizhixin_rss()) as fetch_mock, \
             mock.patch.object(news, "NOW", now), \
             mock.patch.object(news, "SINCE", now - timedelta(hours=24)):
            mode = news.collect_jiqizhixin(items)

        self.assertEqual(mode, "rss")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["超链接"], "https://www.jiqizhixin.com/articles/2026-07-12-1")
        self.assertEqual(items[0]["content_source"], "rss")
        self.assertGreater(len(items[0]["rss_content"]), 80)
        self.assertIn("token=test-token", fetch_mock.call_args.args[0])

    def test_collect_jiqizhixin_falls_back_to_html_with_detail_url(self):
        now = datetime(2026, 7, 12, 15, 0, tzinfo=news.TZ)
        html_page = """
        <article><a href="/articles/2026-07-12-2">机器之心网页兜底文章标题</a>
        <p>摘要</p><time>2026/07/12 14:20</time></article>
        """
        items = []
        with mock.patch.object(news, "load_jiqizhixin_rss_token", return_value="test-token"), \
             mock.patch.object(news, "load_jiqizhixin_rss_cache", return_value=""), \
             mock.patch.object(news, "fetch", side_effect=[urllib.error.URLError("rss down"), html_page]), \
             mock.patch.object(news, "NOW", now), \
             mock.patch.object(news, "SINCE", now - timedelta(hours=24)):
            mode = news.collect_jiqizhixin(items)

        self.assertEqual(mode, "html_fallback:rss_error:URLError")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["超链接"], "https://www.jiqizhixin.com/articles/2026-07-12-2")

    def test_collect_jiqizhixin_uses_cache_without_network(self):
        now = datetime(2026, 7, 12, 15, 0, tzinfo=news.TZ)
        items = []
        with mock.patch.object(news, "load_jiqizhixin_rss_token", return_value="test-token"), \
             mock.patch.object(news, "load_jiqizhixin_rss_cache", return_value=jiqizhixin_rss()), \
             mock.patch.object(news, "fetch") as fetch_mock, \
             mock.patch.object(news, "NOW", now), \
             mock.patch.object(news, "SINCE", now - timedelta(hours=24)):
            mode = news.collect_jiqizhixin(items)

        self.assertEqual(mode, "rss_cache")
        self.assertEqual(len(items), 1)
        fetch_mock.assert_not_called()

    def test_detail_fetch_reuses_rss_content_before_web(self):
        candidate = {"rss_content": "来自 RSS 的完整正文" * 20}
        with mock.patch.object(news, "fetch") as fetch_mock:
            text, error, source = details.fetch_detail(
                {"超链接": "https://www.jiqizhixin.com/articles/2026-07-12-1"},
                6000,
                candidate,
            )

        self.assertIsNone(error)
        self.assertEqual(source, "rss")
        self.assertIn("来自 RSS", text)
        fetch_mock.assert_not_called()


class DailyArchiveTest(unittest.TestCase):
    def item(self, title="模型发布", url="https://example.com/news/1?utm_source=test"):
        return {
            "资讯标题": title,
            "发布时间": "2026-07-12 10:00:00",
            "来源": "测试源",
            "超链接": url,
            "desc": "摘要",
        }

    def test_merge_archive_is_idempotent_by_normalized_url(self):
        first, added, updated, skipped = archive.merge_archive(
            [], [self.item()], "2026-07-12", "2026-07-12 11:40:00"
        )
        second, added2, updated2, skipped2 = archive.merge_archive(
            first,
            [self.item(url="https://example.com/news/1?utm_medium=feed")],
            "2026-07-12",
            "2026-07-12 14:40:00",
        )

        self.assertEqual((added, updated, skipped), (1, 0, 0))
        self.assertEqual((added2, updated2, skipped2), (0, 0, 1))
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["last_seen_at"], "2026-07-12 14:40:00")
        self.assertTrue(second[0]["archive_id"].startswith("A20260712-"))

    def test_load_today_and_yesterday_archives_and_dedupe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            today_item = self.item()
            yesterday_duplicate = self.item(url="https://example.com/news/1")
            yesterday_unique = self.item("另一个模型发布", "https://example.com/news/2")
            (root / "2026-07-12.json").write_text(
                json.dumps({"items": [today_item]}, ensure_ascii=False), encoding="utf-8"
            )
            (root / "2026-07-11.json").write_text(
                json.dumps({"items": [yesterday_duplicate, yesterday_unique]}, ensure_ascii=False), encoding="utf-8"
            )

            items, status = prepare.load_archives(root, ["2026-07-12", "2026-07-11"])
            unique = prepare.dedupe_exact(items)

        self.assertEqual(len(items), 3)
        self.assertEqual(len(unique), 2)
        self.assertEqual(status[str(root / "2026-07-12.json")], "found:1")


if __name__ == "__main__":
    unittest.main()
