import json
import unittest
from pathlib import Path
from unittest.mock import patch

from technical_reports import adapters_china as adapter
from technical_reports.store import report_id


def flight(value, prefix="0:"):
    stream = prefix + json.dumps(value, ensure_ascii=False)
    return '<script>self.__next_f.push(' + json.dumps([1, stream], ensure_ascii=False) + ')</script>'


class ChinaResearchDiscoveryTests(unittest.TestCase):
    def test_model_card_date_uses_cover_publication_not_model_release(self):
        text = '[Page 1]\nDeepSeek V4\n发布日期：4 月 27 日, 2026\n更新日期：4 月 24 日, 2026\n'
        text += '[Page 2]\n模型名称 DeepSeek V4\n发布日期 2026 年 4 月 24 日'
        result = adapter._model_card_date(text, 'https://example.org/card.pdf')
        self.assertEqual(result['published_at'], '2026-04-27')
        self.assertEqual(result['date_basis'], 'pdf_cover_first_publication')
        self.assertEqual(result['date_source_page'], 1)
        with self.assertRaisesRegex(ValueError, 'cover'):
            adapter._model_card_date('[Page 1]\nModel card\n[Page 2]\n发布日期 2026 年 4 月 24 日', 'https://example.org/card.pdf')

    def test_glm_fulltext_decodes_raw_flight_text_frames_and_retains_the_end(self):
        article = {"id": 160, "content_zh": {"root": {"type": "root", "children": [
            {"type": "heading", "children": [{"type": "text", "text": "网络架构"}]},
            {"type": "paragraph", "children": [{"type": "text", "text": "完整正文" * 5000}]},
            {"type": "paragraph", "children": [{"type": "text", "text": "末尾参考文献与限制"}]},
        ]}}}
        # Flight text frames need not have a newline before the next JSON frame.
        html = flight({"blogsItems": [article]}, 'a:T12,raw\ntext!b:')
        text = adapter.extract_fulltext("https://www.zhipuai.cn/zh/research/160", html)
        self.assertIn("完整正文" * 5000, text)
        self.assertTrue(text.endswith("末尾参考文献与限制"))
        self.assertIsNone(adapter.extract_fulltext("https://www.zhipuai.cn/zh/research/999", html))

    def test_kimi_reference_pdf_is_not_promoted_to_its_own_report(self):
        page = flight({"posts": [{"id": "kimi-k2-6", "title": "Kimi K2.6", "date": "2026-04-20"}]})
        page += '<a href="https://example.org/claude-system-card.pdf">Claude comparison reference</a>'
        def get(url):
            if url == adapter.INDICES["Kimi"]:
                return page
            if "api.github.com" in url:
                return "[]"
            raise AssertionError("Unexpected fetch " + url)
        with patch.object(adapter, "_get", side_effect=get):
            result = adapter.discover("Kimi", "2026-04-02", "2026-10-02")
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("report_url", result["items"][0])
        self.assertEqual(result["items"][0]["url"], "https://www.kimi.com/en/blog/kimi-k2-6")

    def test_readme_third_party_references_are_excluded(self):
        own = """# DeepSpec
## Supported Algorithms
[DSpark](https://arxiv.org/abs/2607.05147), [DFlash](https://arxiv.org/abs/2602.06036)
## Citation
@misc{dspark,
 title={DSpark},
 eprint={2607.05147},
}
"""
        self.assertEqual(adapter._official_paper_ids(own), {"2607.05147"})
        harness = "# DeepSeek Harness\nPowered by Cordis, [paper](https://arxiv.org/abs/2608.25512)"
        self.assertEqual(adapter._official_paper_ids(harness), set())
        ordinary = "# Library\nUses [third party](https://arxiv.org/abs/2609.12345) as a baseline."
        self.assertEqual(adapter._official_paper_ids(ordinary), set())

    def test_primary_pdf_requires_an_explicit_report_label(self):
        base = "https://raw.githubusercontent.com/MoonshotAI/Kimi-K3/main/"
        self.assertEqual(adapter._own_pdf('<a href="k3_tech_report.pdf">Full Report</a>', base),
                         base + "k3_tech_report.pdf")
        self.assertIsNone(adapter._own_pdf('[Claude comparison](https://example.org/claude.pdf)', base))

    def test_paper_date_is_authoritative_and_revision_is_separate(self):
        html = '<meta name="citation_title" content="A &amp; B"><meta name="citation_date" content="2026/04/26">'
        html += '<strong>[v1]</strong> Sun, 26 Apr 2026 14:49:33 UTC<br><strong>[v2]</strong> Wed, 5 Aug 2026 01:57:13 UTC'
        with patch.object(adapter, "_get", return_value=html):
            result = adapter._paper_metadata("2606.19348")
        self.assertEqual(result["published_at"], "2026-04-26")
        self.assertEqual(result["revised_at"], "2026-08-05")
        self.assertEqual(result["title"], "A & B")

    def test_glm_traverses_pagination_and_deduplicates_cumulative_pages(self):
        def article(number):
            return {"id": number, "title_zh": "训练研究" + str(number), "createAt": "2026-07-01T16:00:00Z",
                    "content_zh": {"root": {"type": "root", "children": [
                        {"type": "text", "text": "模型训练与评测"}]}}}
        pages = {adapter.INDICES["GLM"]: flight({"blogsItems": [article(1)], "nextPage": 2, "hasMore": True}),
                 adapter.INDICES["GLM"] + "?page=2": flight({"blogsItems": [article(1), article(2)], "hasMore": False})}
        def get(url):
            if url in pages:
                return pages[url]
            if "api.github.com" in url:
                return "[]"
            raise AssertionError("Unexpected fetch " + url)
        with patch.object(adapter, "_get", side_effect=get):
            result = adapter.discover("GLM", "2026-04-02", "2026-10-02")
        self.assertFalse(result["errors"])
        self.assertEqual(len(result["items"]), 2)
        self.assertIn(adapter.INDICES["GLM"] + "?page=2", result["coverage"]["index_urls"])


class ReportEntityTests(unittest.TestCase):
    def test_arxiv_abstract_pdf_html_and_version_links_are_one_entity(self):
        variants = ['https://arxiv.org/abs/2606.19348', 'https://arxiv.org/pdf/2606.19348.pdf',
                    'https://arxiv.org/html/2606.19348v3', 'https://arxiv.org/abs/2606.19348v1#abstract']
        ids = {report_id({'vendor': 'DeepSeek', 'url': url}) for url in variants}
        ids.add(report_id({'vendor': 'DeepSeek', 'url': 'https://www.deepseek.com/news/v4/',
                           'paper_id': '2606.19348v2'}))
        ids.add(report_id({'vendor': 'DeepSeek', 'url': 'https://github.com/deepseek-ai/DeepSeek-V4',
                           'report_url': variants[1]}))
        self.assertEqual(len(ids), 1)
        self.assertNotIn(report_id({'vendor': 'Google', 'url': variants[0]}), ids)

    def test_bootstrap_entities_match_freshly_discovered_paper_aliases(self):
        path = Path(adapter.__file__).parent / 'bootstrap/china.json'
        items = json.loads(path.read_text())['items']
        ids = {report_id(item) for item in items}
        self.assertEqual(len(ids), len(items))
        for item in items:
            if item.get('paper_id'):
                discovered = {'vendor': item['vendor'], 'url': 'https://arxiv.org/abs/' + item['paper_id']}
                self.assertEqual(report_id(item), report_id(discovered))
                discovered['url'] = 'https://arxiv.org/html/' + item['paper_id'] + 'v2'
                self.assertEqual(report_id(item), report_id(discovered))


if __name__ == "__main__":
    unittest.main()
