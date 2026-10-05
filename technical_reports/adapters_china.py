"""Official Chinese model vendors' research discovery (standard library only).

Dates come from research indices or the paper's initial submission, never from
GitHub repository activity. A README link alone is insufficient attribution:
we accept its own citation / leading paper link and reject bibliography links.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import hashlib
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ORG = {"DeepSeek": "deepseek-ai", "Kimi": "MoonshotAI", "GLM": "zai-org"}
INDICES = {"DeepSeek": "https://www.deepseek.com/news/",
           "Kimi": "https://www.kimi.com/en/blog/",
           "GLM": "https://www.zhipuai.cn/zh/research"}
HEADERS = {"User-Agent": "OfficialTechnicalReportsCollector/1.0 (+public research metadata)",
           "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
_CACHE_FALLBACKS = {}


def _get(url):
    cache = Path(__file__).resolve().parents[1] / ".technical_reports" / "cache" / "china"
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".txt")
    if path.exists() and time.time() - path.stat().st_mtime < 3600:
        return path.read_text(encoding="utf-8")
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=35) as response:
            result = response.read().decode("utf-8", "replace")
        cache.mkdir(parents=True, exist_ok=True)
        path.write_text(result, encoding="utf-8")
        _CACHE_FALLBACKS.pop(url, None)
        return result
    except Exception as exc:
        # Cached snapshots preserve discovered records during short outages;
        # callers still retry the current source once the one-hour cache expires.
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            _CACHE_FALLBACKS[url] = str(exc)
            return path.read_text(encoding="utf-8")
        raise


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def next_objects(markup):
    """Decode server-rendered Next.js flight data, including non-anchor lists."""
    fragments = []
    for match in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", markup):
        try:
            entry = json.loads(match[1])
            if len(entry) > 1 and isinstance(entry[1], str):
                fragments.append(entry[1])
        except (ValueError, TypeError):
            continue
    result = []
    stream = "".join(fragments)
    decoder = json.JSONDecoder()
    # Flight T records contain raw multiline text and may not end in a newline;
    # parse framed JSON records directly instead of splitting by lines.
    for match in re.finditer(r"[0-9a-f]+:(?=[\[{])", stream):
        try:
            value, _ = decoder.raw_decode(stream, match.end())
            result.extend(_walk(value))
        except ValueError:
            pass
    return result


def lexical_text(node):
    """Preserve all text, table cells, headings and links in Z.ai rich text."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "linebreak":
        return "\n"
    text = "".join(lexical_text(x) for x in node.get("children", []))
    if node.get("type") in {"paragraph", "heading", "listitem", "tablerow", "tablecell"}:
        text += "\n"
    return text


def extract_fulltext(url, markup):
    """Return GLM's complete embedded article body, or None for other pages."""
    wanted = re.search(r"/research/(\d+)", url)
    for obj in next_objects(markup):
        if "content_zh" in obj and (not wanted or str(obj.get("id")) == wanted[1]):
            body = obj.get("content_zh") or obj.get("content_en")
            if body and isinstance(body, dict):
                return lexical_text(body.get("root", body)).strip()
    return None


def _paper_metadata(paper_id):
    markup = _get("https://arxiv.org/abs/" + paper_id)
    fields = dict(re.findall(r'<meta\s+name="(citation_[^"]+)"\s+content="([^"]*)"', markup))
    title = unescape(fields.get("citation_title", ""))
    published = fields.get("citation_date", "").replace("/", "-")[:10]
    dates = re.findall(r"\[v\d+\]\s*(?:[A-Z][a-z]{2},\s*)?(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})", re.sub(r"<[^>]+>", "", markup))
    revisions = []
    for date in dates:
        try:
            revisions.append(datetime.strptime(date, "%d %b %Y").date().isoformat())
        except ValueError:
            pass
    return {"title": title, "published_at": published, "paper_id": paper_id,
            "url": "https://arxiv.org/abs/" + paper_id,
            "report_url": "https://arxiv.org/pdf/" + paper_id,
            "revised_at": max(revisions) if revisions else None,
            "content_type": "research_paper", "language": "en"}


def _official_paper_ids(readme):
    # First-class project paper links and BibTeX citations, excluding references.
    head = readme[:3500]
    ids = set()
    for link in re.finditer(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", head):
        context = head[max(0, link.start() - 120):link.end() + 180]
        if re.search(r"\[(?:paper|technical report|arxiv[^\]]*)\]|alt=[\"']arxiv|arXiv-\d|badge/(?:arxiv|arXiv)|>[^<]*(?:Paper|Technical Report)[^<]*</a>", context, re.I):
            ids.add(link[1])
    citation = re.split(r"(?im)^#{1,4}\s*(?:[\W_]*)(?:citation|cite us|引用)\b", readme)[-1]
    if citation == readme:
        citation = ""
    for bib in re.findall(r"@(?:misc|article|inproceedings)\{[\s\S]*?\n\}", citation):
        ids.update(re.findall(r"(?:eprint\s*=\s*\{|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})", bib))
    # DeepSpec supports third-party algorithms; only its own citation is owned.
    if "# DeepSpec" in readme:
        ids = set(re.findall(r"eprint\s*=\s*\{(\d{4}\.\d{4,5})", readme))
    # DeepSeek Harness cites the external Cordis framework rather than a report.
    if "# DeepSeek Harness" in readme:
        ids.clear()
    return ids


def _own_pdf(readme, base):
    """Only a labeled primary report link, never a comparative reference PDF."""
    head = readme[:5000]
    match = re.search(r'\[(?:Full Report|Technical Report|Paper|论文|技术报告)\]\(([^)]+\.pdf)\)', head, re.I)
    if not match:
        match = re.search(r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>\s*(?:Full Report|Technical Report|Paper|论文|技术报告)\s*</a>', head, re.I)
    if not match:
        return None
    target = urljoin(base, match[1])
    if "github.com/" in target and "/blob/" in target:
        target = target.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
    return target


def _model_card_publication(card):
    """Use the document cover date, distinct from the model release date."""
    from .common import digest, fetch_bytes
    from .acquire import pdf_text
    raw, final_url, _ = fetch_bytes(card)
    if not raw.startswith(b"%PDF"):
        raise ValueError("model card source did not return a PDF")
    text, _ = pdf_text(raw, digest(card)[:16])
    return _model_card_date(text, final_url)


def _model_card_date(text, url):
    cover = text.split("[Page 2]", 1)[0]
    match = re.search(r"发布日期\s*[:：]\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[,，]?\s*(\d{4})", cover)
    if match:
        month, day, year = map(int, match.groups())
    else:
        match = re.search(r"发布日期\s*[:：]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", cover)
        if not match:
            raise ValueError("document cover has no unambiguous first-publication date")
        year, month, day = map(int, match.groups())
    date = datetime(year, month, day).date().isoformat()
    return {"published_at": date, "published_at_raw": match[0],
            "date_basis": "pdf_cover_first_publication", "date_source_url": url,
            "date_source_page": 1}


def discover(vendor, start_date, end_date, known=None):
    """Discover the whole date window, returning items, errors and coverage."""
    if vendor not in ORG:
        raise ValueError("unsupported vendor: " + str(vendor))
    start_date, end_date = str(start_date)[:10], str(end_date)[:10]
    items, errors, visited, paper_sources = [], [], [], {}
    def add(item):
        item.update(vendor=vendor, source=vendor + " 官方研究", date_precision="day")
        if start_date <= item.get("published_at", "") <= end_date:
            items.append(item)
    try:
        index = INDICES[vendor]
        if vendor == "GLM":
            page = 1
            while True:
                url = index + ("?page=" + str(page) if page > 1 else "")
                markup = _get(url)
                visited.append(url)
                payload = next((o for o in next_objects(markup) if "blogsItems" in o), None)
                if payload is None:
                    raise ValueError("research pagination payload missing")
                for article in payload["blogsItems"]:
                    title = article.get("title_zh") or article.get("title_en")
                    if not title:
                        continue
                    body = article.get("content_zh") or article.get("content_en") or {}
                    text = lexical_text(body.get("root", body))
                    # Research list includes model launch posts; accept only those
                    # that contain methods, evaluations or independent engineering.
                    if not re.search(r"评测|基准|训练|推理|架构|benchmark|evaluation|training", text, re.I):
                        continue
                    add({"title": title, "published_at": str(article.get("createAt", ""))[:10],
                         "url": "https://www.zhipuai.cn/zh/research/" + str(article["id"]),
                         "content_type": "technical_blog", "language": "zh"})
                if not payload.get("hasMore"):
                    break
                page = int(payload.get("nextPage", page + 1))
                if page > 100:
                    raise ValueError("pagination exceeded 100 pages")
        elif vendor == "Kimi":
            markup = _get(index)
            visited.append(index)
            for obj in next_objects(markup):
                if "title" in obj and "date" in obj and "id" in obj:
                    date = str(obj["date"])[:10]
                    if start_date <= date <= end_date:
                        add({"title": obj["title"], "published_at": date,
                             "url": "https://www.kimi.com/en/blog/" + str(obj["id"]),
                             "content_type": "technical_blog", "language": "en"})
        else:
            markup = _get(index)
            visited.append(index)
            for match in re.finditer(r'<a[^>]+href="(https://arxiv\.org/abs/(\d{4}\.\d{4,5}))"[^>]*>([\s\S]*?)</a>', markup):
                paper_sources[match[2]] = index
            for obj in next_objects(markup):
                if obj.get("paperUrl") and obj.get("date"):
                    target = obj["paperUrl"]
                    pid = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", target)
                    if pid:
                        paper_sources[pid[1]] = index
                    else:
                        add({"title": obj["title"].split("：")[0] + " Technical Report",
                             "published_at": obj["date"], "url": urljoin(index, obj["slug"] + "/"),
                             "report_url": target.replace("/blob/", "/resolve/"),
                             "content_type": "technical_report", "language": "en"})
            transparency_url = "https://www.deepseek.com/transparency/"
            transparency = _get(transparency_url)
            visited.append(transparency_url)
            # The table describes model releases; a report can be published later.
            for model, year, month, day, card in re.findall(
                    r'(DeepSeek-[A-Za-z0-9.]+)[\s\S]{0,800}?(\d{4})年(\d{1,2})月(\d{1,2})日[\s\S]{0,900}?href="([^"]+model-card[^"]+\.pdf)"',
                    transparency):
                if known and known({'vendor': vendor, 'url': card, 'report_url': card}):
                    continue
                try:
                    publication = _model_card_publication(card)
                except Exception as exc:
                    errors.append({"source": card, "error": "model-card publication date unavailable: " + str(exc)})
                    continue
                add({"title": model + " 模型卡", **publication,
                     "model_released_at": f"{year}-{int(month):02d}-{int(day):02d}",
                     "url": card, "report_url": card, "related_urls": [transparency_url],
                     "content_type": "model_card", "language": "zh"})
    except Exception as exc:
        errors.append({"source": INDICES[vendor], "error": str(exc)})

    repos, page = [], 1
    try:
        while True:
            url = "https://api.github.com/orgs/" + ORG[vendor] + "/repos?per_page=100&type=public&page=" + str(page)
            batch = json.loads(_get(url))
            visited.append(url)
            if not isinstance(batch, list):
                raise ValueError("GitHub repo list invalid")
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    except Exception as exc:
        errors.append({"source": "https://github.com/" + ORG[vendor], "error": str(exc)})

    def read_repo(repo):
        base = "https://raw.githubusercontent.com/" + ORG[vendor] + "/" + repo["name"] + "/" + repo["default_branch"] + "/"
        for name in ("README.md", "readme.md", "README.MD"):
            try:
                return repo, _get(base + name), base
            except Exception:
                continue
        return repo, None, base
    read_count = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        for repo, readme, base in executor.map(read_repo, repos):
            if readme is None:
                # Organization profile, branding and static site repos are not
                # research sources; absent README files do not impair coverage.
                if repo["name"] not in {".github", "moonshotai.github.io", "Branding-Guide"}:
                    errors.append({"source": repo["html_url"], "error": "no readable README"})
                continue
            read_count += 1
            for pid in _official_paper_ids(readme):
                paper_sources.setdefault(pid, repo["html_url"])
            if vendor == "Kimi":
                model_name = re.sub(r"[^a-z0-9]", "", repo["name"].lower())
                existing = next((i for i in items if re.sub(r"[^a-z0-9]", "", i["title"].lower()) == model_name), None)
                report = _own_pdf(readme, base)
                if existing and report:
                    existing.update(report_url=report, content_type="technical_report",
                                    related_urls=[repo["html_url"]])
            if vendor == "Kimi" and repo["name"] == "FlashKDA":
                for date, path in re.findall(r"\*\*(\d{4}-\d{2}-\d{2})\*\*[^\n]*?\]\((docs/[^)]+\.md)\)", readme):
                    if known and known({'vendor': vendor, 'url': repo['html_url'] + '/blob/' + repo['default_branch'] + '/' + path,
                                        'report_url': base + path}):
                        continue
                    announced = date
                    try:
                        body = _get(base + path)
                        original_date = re.search(r"^\*(\d{4}-\d{2}-\d{2})\*\s*$", body, re.M)
                        if original_date:
                            date = original_date[1]
                    except Exception:
                        pass
                    add({"title": "FlashKDA v1 Deep Dive", "published_at": date,
                         "announcement_at": announced,
                         "url": repo["html_url"] + "/blob/" + repo["default_branch"] + "/" + path,
                         "report_url": base + path, "content_type": "engineering_blog", "language": "en"})
            # Non-arXiv conference papers retain authoritative publisher dates.
            if vendor == "GLM" and repo["name"] == "SurveyReview":
                doi = re.search(r"https://doi\.org/([\d.]+/[^\s}]+)", readme)
                if doi:
                    if known and known({'vendor': vendor, 'url': 'https://doi.org/' + doi[1]}):
                        continue
                    try:
                        metadata = json.loads(_get("https://api.crossref.org/works/" + doi[1]))["message"]
                        date = metadata.get("published-online", metadata.get("published", {}))["date-parts"][0]
                        if len(date) != 3:
                            raise ValueError("publisher date lacks day precision")
                        publisher_date = "%04d-%02d-%02d" % tuple(date)
                        # Verified open preprint for the publisher's DOI; preserve
                        # both the initial paper and conference publication dates.
                        if doi[1] == "10.1145/3770855.3817505":
                            record = _paper_metadata("2608.07641")
                            if re.sub(r"\W", "", record["title"]).lower() != re.sub(r"\W", "", metadata["title"][0]).lower():
                                raise ValueError("DOI and preprint titles differ")
                            record.update(doi=doi[1], publisher_published_at=publisher_date,
                                          related_urls=[repo["html_url"], "https://doi.org/" + doi[1]])
                            add(record)
                        else:
                            add({"title": metadata["title"][0], "published_at": publisher_date,
                                 "url": "https://doi.org/" + doi[1], "paper_id": doi[1],
                                 "related_urls": [repo["html_url"]], "content_type": "research_paper", "language": "en"})
                    except Exception as exc:
                        errors.append({"source": "https://doi.org/" + doi[1], "error": str(exc)})

    # Validate publication metadata for every owned paper. Source metadata and
    # identifier months can differ; the identifier is never a date filter.
    candidates = [(pid, source) for pid, source in paper_sources.items()
                  if not known or not known({'vendor': vendor, 'url': 'https://arxiv.org/abs/' + pid, 'paper_id': pid})]
    def paper(job):
        pid, source = job
        try:
            result = _paper_metadata(pid)
            result["related_urls"] = [source]
            return result, None
        except Exception as exc:
            return None, {"source": "https://arxiv.org/abs/" + pid, "error": str(exc)}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for result, error in executor.map(paper, candidates):
            if error:
                errors.append(error)
            else:
                add(result)
    unique = {}
    for item in items:
        key = item.get("paper_id") or item["url"]
        unique.setdefault(key, item)
    # The PerceptionBench blog is an explanatory carrier of the same paper.
    if vendor == "Kimi":
        paper = unique.get("2607.24957")
        if paper:
            blog_url = "https://www.kimi.com/en/blog/perception-bench"
            blog = unique.pop(blog_url, None)
            if blog:
                paper.setdefault("related_urls", []).append(blog_url)
                paper["announcement_at"] = blog["published_at"]
    return {"items": sorted(unique.values(), key=lambda i: (i["published_at"], i["title"]), reverse=True),
            "errors": errors,
            "coverage": {"vendor": vendor, "start_date": start_date, "end_date": end_date,
                         "index_urls": visited, "github_repositories": len(repos),
                         "github_readmes_read": read_count,
                         "cached_sources_after_fetch_failure": [
                             {"url": url, "error": error} for url, error in list(_CACHE_FALLBACKS.items())
                             if ORG[vendor] + "/" in url or url.startswith(INDICES[vendor])],
                         "policy": "all official research index pages plus own papers linked in official repository READMEs; excludes third-party references and undated candidates"}}
