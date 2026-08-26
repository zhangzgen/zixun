#!/usr/bin/env python3
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET


APP_ID = "cli_aa9f8fe7eebb9bd0"
APP_SECRET = "t84XiS74cREDgVwIIGmjFb3FPVRFIdDa"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/b5637c8c-aeb9-49d0-8a1c-b9d3fae7fa66"
PARENT_NODE_TOKEN = "DtXbws0qciwGbkkLUXCcHIdDnOc"
PARENT_WIKI_URL = "https://ocns5gikjtax.feishu.cn/wiki/DtXbws0qciwGbkkLUXCcHIdDnOc"
JIQIZHIXIN_RSS_ENDPOINT = "https://mcp.applications.jiqizhixin.com/rss"
JIQIZHIXIN_TOKEN_FILE = Path.home() / ".codex" / "automations" / "ai" / "jiqizhixin_rss_token"
JIQIZHIXIN_CACHE_FILE = Path.home() / ".codex" / "automations" / "ai" / "jiqizhixin_rss_cache.xml"
JIQIZHIXIN_CACHE_SECONDS = 55 * 60

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS_AGO = (NOW - timedelta(days=2)).strftime("%Y-%m-%d")
SINCE = NOW - timedelta(hours=24)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, "replace")


def api_json(url, method="GET", payload=None, token=None):
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} HTTP {e.code}: {body[:500]}")


def strip_tags(s):
    s = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_rss_datetime(value):
    dt = email.utils.parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def parse_iso_datetime(value):
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(TZ)


def add_item(items, title, dt, source, url, desc=""):
    title = strip_tags(title)
    if not title or not dt or not url:
        return None
    if dt < SINCE or dt > NOW + timedelta(minutes=10):
        return None
    record = {
        "资讯标题": title,
        "发布时间": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "来源": source,
        "超链接": urllib.parse.urljoin(url, url),
        "desc": strip_tags(desc),
        "dt": dt,
    }
    items.append(record)
    return record


def collect_qbitai(items):
    xml = fetch("https://www.qbitai.com/category/%e8%b5%84%e8%ae%af/feed")
    root = ET.fromstring(xml)
    for item in root.findall("./channel/item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        desc = item.findtext("description") or ""
        add_item(items, title, parse_rss_datetime(pub), "量子位", link, desc)


def collect_xinzhiyuan(items):
    page = fetch("https://aiera.com.cn/")
    for m in re.finditer(r'href="(?P<url>https://aiera\.com\.cn/\d{4}/\d{2}/\d{2}/[^"]+)".{0,260}?datetime="(?P<dt>[^"]+)"', page, re.S):
        url = html.unescape(m.group("url"))
        dt = parse_iso_datetime(m.group("dt"))
        slug = urllib.parse.unquote(url.rstrip("/").split("/")[-1])
        title = re.sub(r"[-_]+", " ", slug).strip()
        add_item(items, title, dt, "新智元", url)


def collect_xinhua(items):
    page = fetch("https://www.news.cn/tech/")
    pattern = r'href="(?P<url>[^"]+)"[^>]*>(?P<title>[^<]{4,120})</a>\s*(?P<date>20\d{2}-\d{2}-\d{2})'
    for m in re.finditer(pattern, page):
        title = m.group("title")
        url = urllib.parse.urljoin("https://www.news.cn/tech/", html.unescape(m.group("url")))
        dt = datetime.strptime(m.group("date") + " 09:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        add_item(items, title, dt, "新华网科技", url)


def collect_aibase(items):
    page = fetch("https://news.aibase.com/zh/news")
    ids = []
    for mid in re.findall(r"news/(\d+)", page):
        if mid not in ids:
            ids.append(mid)
    for mid in ids[:12]:
        url = f"https://news.aibase.com/zh/news/{mid}"
        try:
            detail = fetch(url)
        except Exception:
            continue
        title = ""
        mt = re.search(r'<h1[^>]*>(.*?)</h1>|<title>(.*?)</title>', detail, re.S | re.I)
        if mt:
            title = strip_tags(mt.group(1) or mt.group(2)).replace(" | AIBase", "")
        md = re.search(r'(?:datetime|datePublished|publishedAt|createdAt)["\':= ]+["\']([^"\']+)', detail)
        dt = None
        if md:
            try:
                dt = parse_iso_datetime(html.unescape(md.group(1)))
            except Exception:
                pass
        if not dt:
            mdate = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", detail)
            if mdate:
                year, month, day = mdate.groups()
                dt = datetime.strptime(f"{year}-{month.zfill(2)}-{day.zfill(2)} 09:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        if title and dt:
            add_item(items, title, dt, "AIBase", url, strip_tags(detail[:5000]))


def load_jiqizhixin_rss_token():
    token = os.environ.get("JIQIZHIXIN_RSS_TOKEN", "").strip()
    if token:
        return token
    try:
        return JIQIZHIXIN_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def load_jiqizhixin_rss_cache(max_age_seconds=None):
    try:
        if max_age_seconds is not None:
            age = time.time() - JIQIZHIXIN_CACHE_FILE.stat().st_mtime
            if age > max_age_seconds:
                return ""
        return JIQIZHIXIN_CACHE_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def save_jiqizhixin_rss_cache(xml):
    JIQIZHIXIN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = JIQIZHIXIN_CACHE_FILE.with_suffix(".tmp")
    temp.write_text(xml, encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(JIQIZHIXIN_CACHE_FILE)


def fetch_jiqizhixin_rss_xml(token):
    cached = load_jiqizhixin_rss_cache(JIQIZHIXIN_CACHE_SECONDS)
    if cached:
        return cached, "rss_cache"

    query = urllib.parse.urlencode({"token": token})
    try:
        xml = fetch(f"{JIQIZHIXIN_RSS_ENDPOINT}?{query}")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            stale = load_jiqizhixin_rss_cache()
            if stale:
                return stale, "rss_cache_stale"
        raise
    save_jiqizhixin_rss_cache(xml)
    return xml, "rss"


def collect_jiqizhixin_rss(items, token):
    if not token:
        raise RuntimeError("RSS token is not configured")
    xml, mode = fetch_jiqizhixin_rss_xml(token)
    root = ET.fromstring(xml)
    if root.tag != "rss" or root.find("channel") is None:
        raise RuntimeError("unexpected RSS document")

    content_tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
    for item in root.findall("./channel/item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        desc = item.findtext("description") or ""
        content = item.findtext(content_tag) or ""
        if not pub:
            continue
        record = add_item(items, title, parse_rss_datetime(pub), "机器之心", link, desc)
        if record is not None:
            record["rss_content"] = strip_tags(content)
            record["content_source"] = "rss"
            record["member_only"] = "会员专享" in title or "pro.jiqizhixin.com" in link
    return mode


def collect_jiqizhixin_html(items):
    page = fetch("https://www.jiqizhixin.com/industry")
    pattern = (
        r'href="(?P<url>/articles/[^"]+)"[^>]*>\s*(?:<[^>]+>)*\s*'
        r'(?P<title>[^<>]{8,180})\s*(?:</[^>]+>)*.*?'
        r'(?P<dt>20\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})'
    )
    seen = set()
    for match in re.finditer(pattern, page, re.S):
        url = urllib.parse.urljoin("https://www.jiqizhixin.com/industry", match.group("url"))
        if url in seen:
            continue
        seen.add(url)
        dt = datetime.strptime(match.group("dt"), "%Y/%m/%d %H:%M").replace(tzinfo=TZ)
        add_item(items, match.group("title"), dt, "机器之心", url)


def collect_jiqizhixin(items):
    token = load_jiqizhixin_rss_token()
    if token:
        try:
            return collect_jiqizhixin_rss(items, token)
        except Exception as exc:
            fallback_reason = f"rss_error:{type(exc).__name__}"
    else:
        fallback_reason = "rss_token_missing"

    collect_jiqizhixin_html(items)
    return f"html_fallback:{fallback_reason}"


def collect_tophub(items):
    page = fetch("https://tophub.today/n/x9ozB4KoXb")
    for m in re.finditer(r'<a href="(?P<url>https://so\.toutiao\.com/search\?keyword=[^"]+)"[^>]*>(?P<title>[^<]+)</a>', page):
        title = strip_tags(m.group("title"))
        add_item(items, title, NOW, "今日头条/Tophub", html.unescape(m.group("url")))


def parse_readhub_items(page):
    """Extract Readhub news records from the Next.js flight data."""
    records = []
    flight_pattern = r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)</script>'
    for match in re.finditer(flight_pattern, page):
        try:
            chunk = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        offset = 0
        while True:
            marker = chunk.find('"items":[', offset)
            if marker < 0:
                break
            start = marker + len('"items":')
            try:
                batch, _ = json.JSONDecoder().raw_decode(chunk[start:])
            except json.JSONDecodeError:
                offset = start
                continue
            if isinstance(batch, list):
                records.extend(record for record in batch if isinstance(record, dict))
            offset = start
    return records


def collect_readhub(items):
    page = fetch("https://readhub.cn/news/ai")
    seen = set()
    for record in parse_readhub_items(page):
        title = record.get("title") or ""
        url = record.get("url") or ""
        published = record.get("publishDate") or record.get("createdAt") or ""
        if not title or not url or not published or url in seen:
            continue
        try:
            dt = parse_iso_datetime(published)
        except (TypeError, ValueError):
            continue
        seen.add(url)
        add_item(items, title, dt, "Readhub", url, record.get("summary") or "")


def dedupe(items, history_text):
    seen = set()
    out = []
    hist = re.sub(r"\s+", "", history_text or "")
    for item in sorted(items, key=lambda x: x["dt"], reverse=True):
        key = re.sub(r"\W+", "", item["资讯标题"].lower())[:40]
        if key in seen:
            continue
        if key and key in hist:
            continue
        seen.add(key)
        out.append(item)
    return out[:20]


def article_text(url):
    try:
        text = fetch(url)
    except Exception as e:
        return "内容错误", str(e)
    if re.search(r"404|403 Forbidden|Access Denied|500 Internal Server Error|Allowed memory size|URL returned status", text, re.I):
        return "内容错误", "bad status content"
    clean = strip_tags(text)
    if len(clean) < 80:
        return "内容错误", "too short"
    if re.search(
        r"请先登录|登录后查看|注册登录|扫码登录|验证码|安全验证|人机验证|验证中心|"
        r"下载App|下载 APP|App 内打开|客户端打开|Just a moment|Checking your browser",
        clean,
        re.I,
    ) and len(clean) < 1500:
        return "内容错误", "navigation/login shell"
    return clean[:4000], None


def keyword_for(title):
    for pat in ["OpenAI", "Anthropic", "Claude", "ChatGPT", "GPT-5.5", "GPT", "Gemini", "DeepSeek", "Meta", "华为", "LeCun", "李飞飞", "WAIC", "世界模型", "智能体", "机器人", "AIBase"]:
        if pat.lower() in title.lower():
            return pat
    m = re.search(r"([A-Za-z][A-Za-z0-9.-]{1,18}|[\u4e00-\u9fa5]{2,6})", title)
    return m.group(1) if m else "AI"


def summarize(item, body):
    if body == "内容错误":
        return "内容错误", ""
    kw = keyword_for(item["资讯标题"])
    title = item["资讯标题"]
    title = re.sub(r"[！!。；;].*$", "", title)
    if len(title) > 44:
        title = title[:44].rstrip() + "…"
    summary = title
    if kw and kw in summary:
        summary = summary.replace(kw, f"**{kw}**", 1)
    else:
        summary = f"**{kw}**：{summary}"
    if len(re.sub(r"[*]", "", summary)) > 60:
        summary = summary[:58] + "…"
    return summary, kw


def text_run(content, *, bold=False, link=None):
    style = {}
    if bold:
        style["bold"] = True
    if link:
        style["link"] = {"url": link}
    return {"text_run": {"content": content, "text_element_style": style}}


def text_data(elements):
    return {"elements": elements, "style": {}}


def parse_bold_summary(summary):
    elements = []
    pos = 0
    for match in re.finditer(r"\*\*(.+?)\*\*", summary):
        if match.start() > pos:
            elements.append(text_run(summary[pos:match.start()]))
        elements.append(text_run(match.group(1), bold=True))
        pos = match.end()
    if pos < len(summary):
        elements.append(text_run(summary[pos:]))
    return elements


def news_doc_blocks(items):
    blocks = [
        {"block_type": 5, "heading3": text_data([text_run("🟣 AI 资讯")])},
        {"block_type": 22, "divider": {}},
    ]
    for item in items:
        elements = [
            text_run("🔸 "),
            text_run(f"[{item['关键词']}]", bold=True, link=item["超链接"]),
            text_run(" "),
        ]
        elements.extend(parse_bold_summary(item["资讯简介"]))
        blocks.append({"block_type": 2, "text": text_data(elements)})
    return blocks


def tenant_token():
    res = api_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", "POST", {
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
    })
    if res.get("code") != 0:
        raise RuntimeError(f"tenant token failed: {res}")
    return res["tenant_access_token"]


def get_parent_node(token):
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={urllib.parse.quote(PARENT_NODE_TOKEN)}"
    res = api_json(url, token=token)
    if res.get("code") != 0:
        raise RuntimeError(f"get parent node failed: {res}")
    node = res.get("data", {}).get("node", {})
    return node


def list_child_nodes(token, space_id):
    nodes = []
    page_token = ""
    while True:
        qs = urllib.parse.urlencode({"parent_node_token": PARENT_NODE_TOKEN, "page_size": 50, "page_token": page_token})
        url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes?{qs}"
        res = api_json(url, token=token)
        if res.get("code") != 0:
            return nodes
        data = res.get("data", {})
        nodes.extend(data.get("items") or data.get("nodes") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return nodes


def get_history_text(token, space_id, title):
    for n in list_child_nodes(token, space_id):
        if n.get("title") == title:
            obj = n.get("obj_token") or n.get("obj_id")
            if not obj:
                return ""
            try:
                res = api_json(f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj}/raw_content", token=token)
                return json.dumps(res, ensure_ascii=False)
            except Exception:
                return json.dumps(n, ensure_ascii=False)
    return ""


def list_doc_blocks(token, document_id):
    items = []
    page_token = ""
    while True:
        qs = urllib.parse.urlencode({"page_size": 500, "page_token": page_token})
        res = api_json(f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks?{qs}", token=token)
        if res.get("code") != 0:
            raise RuntimeError(f"list doc blocks failed: {res}")
        data = res.get("data", {})
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token") or ""
        if not page_token:
            return items


def clear_doc_children(token, document_id):
    blocks = list_doc_blocks(token, document_id)
    root = next((b for b in blocks if b.get("block_id") == document_id), None)
    if not root:
        root = next((b for b in blocks if b.get("block_type") == 1), None)
    child_count = len(root.get("children") or []) if root else 0
    if not child_count:
        return
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children/batch_delete"
    res = api_json(url, "DELETE", {"start_index": 0, "end_index": child_count}, token=token)
    if res.get("code") != 0:
        raise RuntimeError(f"clear doc children failed: {res}")


def create_wiki_doc(token, space_id, title):
    for n in list_child_nodes(token, space_id):
        if n.get("title") == title and (n.get("obj_token") or n.get("obj_id")):
            return n
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes"
    payload = {
        "obj_type": "docx",
        "node_type": "origin",
        "parent_node_token": PARENT_NODE_TOKEN,
        "title": title,
    }
    res = api_json(url, "POST", payload, token=token)
    if res.get("code") != 0:
        raise RuntimeError(f"create wiki doc failed: {res}")
    node = res.get("data", {}).get("node", {})
    return node


def write_doc_blocks(token, document_id, items):
    clear_doc_children(token, document_id)
    children = news_doc_blocks(items)
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    last = None
    for start in range(0, len(children), 50):
        res = api_json(
            f"{url}?client_token={uuid.uuid4()}",
            "POST",
            {"children": children[start:start + 50], "index": start},
            token=token,
        )
        if res.get("code") != 0:
            raise RuntimeError(f"write doc blocks failed: {res}")
        last = res
        time.sleep(0.35)
    return last or {}


def send_card(title, card_or_markdown):
    if isinstance(card_or_markdown, dict):
        card = card_or_markdown
    else:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "purple", "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": card_or_markdown}],
        }
    payload = {
        "msg_type": "interactive",
        "card": card,
    }
    return api_json(WEBHOOK, "POST", payload)


def build_news_card(date_text, doc_url, items):
    summary_lines = []
    for i, item in enumerate(items, 1):
        plain = re.sub(r"\*\*", "", item["资讯简介"])
        summary_lines.append(f"{i}. {plain}<text_tag color='green'>{item['关键词']}</text_tag>")
    return {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "update_multi": True,
            "summary": {"content": f"{date_text} AI资讯"},
        },
        "header": {
            "template": "purple",
            "title": {"tag": "plain_text", "content": f"{date_text} 要闻"},
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 16px 16px 16px",
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": f"📖 {date_text} 要闻",
                        "text_size": "heading-1",
                        "text_align": "center",
                    },
                },
                {
                    "tag": "column_set",
                    "flex_mode": "bisect",
                    "horizontal_align": "center",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": "<text_tag color='purple'>AI资讯</text_tag>",
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [],
                        },
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "\n".join(summary_lines),
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "bisect",
                    "horizontal_align": "center",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "-More-"},
                                    "type": "primary",
                                    "behaviors": [
                                        {"type": "open_url", "default_url": doc_url}
                                    ],
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [],
                        },
                    ],
                },
            ],
        },
    }


def failure(reason):
    safe = re.sub(APP_SECRET, "***", str(reason))
    try:
        send_card("资讯警告", f"{TODAY} AI资讯推送失败：{safe[:800]}")
    except Exception:
        pass
    print(json.dumps({"ok": False, "error": safe}, ensure_ascii=False))


def main():
    workflow = Path("/Users/zzg/SignalSpace/WorkSpace/zixun/AI咨询收集工作流.yml").read_text(encoding="utf-8")
    print(f"workflow_loaded={len(workflow)} bytes")

    token = tenant_token()
    parent = get_parent_node(token)
    space_id = parent.get("space_id")
    if not space_id:
        raise RuntimeError(f"missing space_id from parent node: {parent}")

    history_titles = [f"📖 {YESTERDAY} 要闻", f"📖 {TWO_DAYS_AGO} 要闻"]
    history_parts = []
    history_status = {}
    for ht in history_titles:
        txt = get_history_text(token, space_id, ht)
        history_status[ht] = "found" if txt else "missing"
        history_parts.append(txt)
    history_text = "\n".join(history_parts)

    items = []
    source_errors = {}
    for name, fn in [
        ("量子位", collect_qbitai),
        ("新智元", collect_xinzhiyuan),
        ("AIBase", collect_aibase),
        ("新华网科技", collect_xinhua),
        ("机器之心", collect_jiqizhixin),
        ("今日头条/Tophub", collect_tophub),
        ("Readhub", collect_readhub),
    ]:
        before = len(items)
        try:
            fn(items)
            source_errors[name] = f"ok:{len(items)-before}"
        except Exception as e:
            source_errors[name] = f"error:{e}"

    selected = dedupe(items, history_text)
    final = []
    for item in selected:
        body, err = article_text(item["超链接"])
        summary, kw = summarize(item, body)
        item["正文抓取"] = "ok" if not err else err
        item["资讯简介"] = summary
        item["关键词"] = kw
        if summary != "内容错误":
            final.append(item)
        time.sleep(0.2)

    if not final:
        raise RuntimeError(f"no valid news after filtering; sources={source_errors}")

    md_lines = ["### 🟣 AI 资讯", "", "---", ""]
    for item in final:
        md_lines.append(f"   🔸 **[{item['关键词']}]({item['超链接']})** {item['资讯简介']}")
        md_lines.append("")
    markdown = "\n".join(md_lines).rstrip() + "\n"

    doc_title = f"📖 {TODAY} 要闻"
    node = create_wiki_doc(token, space_id, doc_title)
    doc_id = node.get("obj_token") or node.get("obj_id")
    doc_url = node.get("url") or f"https://ocns5gikjtax.feishu.cn/wiki/{node.get('node_token', '')}"
    if not doc_id:
        raise RuntimeError(f"created node missing document token: {node}")
    write_doc_blocks(token, doc_id, final)

    top = final[:5] if len(final) >= 5 else final[:3]
    push_res = send_card(f"{TODAY} 要闻", build_news_card(TODAY, doc_url, top))

    result = {
        "ok": True,
        "date": TODAY,
        "window_start": SINCE.strftime("%Y-%m-%d %H:%M:%S"),
        "doc_title": doc_title,
        "doc_url": doc_url,
        "selected_count": len(selected),
        "published_count": len(final),
        "history_status": history_status,
        "source_status": source_errors,
        "push_result": push_res,
        "markdown": markdown,
        "items": [{k: v for k, v in it.items() if k != "dt"} for it in final],
    }
    out = Path("/Users/zzg/SignalSpace/WorkSpace/zixun/ai_news_run_result.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ["ok", "doc_title", "doc_url", "published_count", "history_status", "source_status", "push_result"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--legacy-auto" not in sys.argv:
        print(
            "run_ai_news.py provides shared helpers for the split collection/publish flows.\n"
            "Use the workflow entrypoints instead:\n"
            "  python3 get_news_list.py --no-history --workflow AI资讯日内采集工作流.yml --output tmp_ai_news_collect_candidates.json --pretty\n"
            "  python3 archive_ai_news.py --input tmp_ai_news_filtered.json --archive-dir ai_news_daily\n"
            "  python3 prepare_ai_news_publish.py --archive-dir ai_news_daily --output tmp_ai_news_candidates.json\n"
            "  python3 get_news_details.py --input tmp_ai_news_selected.json --candidates tmp_ai_news_candidates.json --output tmp_ai_news_details.json\n"
            "  python3 publish_ai_news.py --input tmp_ai_news_publish.json --result ai_news_run_result.json\n"
            "Pass --legacy-auto only if you intentionally want the old one-shot flow."
        )
        sys.exit(2)

    sys.argv.remove("--legacy-auto")
    try:
        main()
    except Exception as e:
        failure(e)
        sys.exit(1)
