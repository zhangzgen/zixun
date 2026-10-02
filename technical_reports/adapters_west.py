"""Official Anthropic/OpenAI report discovery (public HTML/RSS, stdlib only).

The server-rendered public page data contains the complete publication list,
including rows hidden by the site's initial 'See more' presentation. Sitemap
lastmod is deliberately never used as a publication date.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor


class _ReportLinks(HTMLParser):
    """Read visible links and their paragraph context, excluding navigation."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.metadata, self.visible = [], {}, []
        self.anchor, self.paragraph, self.heading = None, None, None
        self.skip, self.navigation, self.position = 0, 0, 0
        self.references = False
        self.current_heading = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"nav", "footer"}:
            self.navigation += 1
        if tag == "meta":
            self.metadata[attrs.get("name", attrs.get("property", ""))] = attrs.get("content", "")
        if self.skip or self.navigation:
            return
        if tag == "p":
            self.paragraph = {"text": [], "links": []}
        if tag in {"h1", "h2", "h3", "h4"}:
            self.heading = []
        if tag == "a" and attrs.get("href"):
            self.anchor = {"href": attrs["href"], "text": [], "position": self.position,
                           "references": self.references, "heading": self.current_heading,
                           "before": "".join(self.visible)[-160:]}
            if self.paragraph is not None:
                self.paragraph["links"].append(self.anchor)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"nav", "footer"}:
            self.navigation = max(0, self.navigation - 1)
        if self.skip or self.navigation:
            return
        if tag == "a" and self.anchor is not None:
            self.anchor["text"] = " ".join("".join(self.anchor["text"]).split())
            self.links.append(self.anchor)
            self.anchor = None
        if tag == "p" and self.paragraph is not None:
            context = " ".join("".join(self.paragraph["text"]).split())
            for link in self.paragraph["links"]:
                link["context"] = context
            self.paragraph = None
        if tag in {"h1", "h2", "h3", "h4"} and self.heading is not None:
            heading = " ".join("".join(self.heading).split()).lower()
            self.references = heading in {"references", "bibliography", "related work", "citations"}
            self.current_heading = heading
            self.heading = None

    def handle_data(self, text):
        if self.skip or self.navigation:
            return
        self.visible.append(text)
        self.position += len(text)
        if self.anchor is not None:
            self.anchor["text"].append(text)
        if self.paragraph is not None:
            self.paragraph["text"].append(text)
        if self.heading is not None:
            self.heading.append(text)


def _document_url(base, href):
    url = urllib.parse.urljoin(base, html.unescape(href))
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    host = (parts.hostname or "").lower()
    if host in {"arxiv.org", "www.arxiv.org"}:
        if re.match(r"/(?:abs|pdf|html)/\d{4}\.\d+(?:v\d+)?", parts.path):
            url = re.sub(r"/(?:abs|html)/", "/pdf/", url)
            return url.split("#", 1)[0]
    if ".pdf" in parts.path.lower() or ".pdf" in parts.query.lower():
        return url.split("#", 1)[0]
    if host in {"alignment.anthropic.com", "transformer-circuits.pub"} and re.match(r"/20\d\d/[^/]+", parts.path):
        return url.split("#", 1)[0]
    if host in {"biorxiv.org", "www.biorxiv.org", "medrxiv.org", "www.medrxiv.org"} and parts.path.startswith("/content/"):
        return url.split("#", 1)[0]
    return None


def primary_original(url, page):
    """Resolve explicit own-paper links; never select the first cited PDF.

    This pure resolver returns acquisition metadata. It recognizes named report
    buttons, scholarly citation_pdf_url metadata and explicitly labelled
    supplementary documents. An article with no such link remains an HTML
    original. Citation/reference links alone never establish a primary report.
    """
    parser = _ReportLinks()
    parser.feed(page)
    result = {"supplementary_urls": [], "related_urls": [], "evidence": []}
    primary, supplements = [], []
    labels = {
        "view pdf", "download pdf", "download the pdf", "read pdf", "read in pdf",
        "read the paper", "read our paper", "read the full paper", "full paper",
        "read the report", "read the full report", "read our report", "full report",
        "download the report", "download report", "download paper", "download the paper",
        "read the research", "read our research", "extended blog post", "new research",
    }
    for link in parser.links:
        destination = _document_url(url, link["href"])
        if not destination or destination.rstrip("/") == url.split("#", 1)[0].rstrip("/"):
            continue
        label = re.sub(r"[^\w\s-]", "", link["text"].lower()).strip()
        label = " ".join(label.split())
        context = link.get("context", "")
        if link["references"]:
            continue
        context_words = set(re.findall(r"[a-z]+", context.lower()))
        button_like = not context or (len(context) < 200 and context_words <= {
            "paper", "pdf", "read", "view", "download", "the", "our", "full", "report",
            "research", "code", "data", "dataset", "datasets", "model", "models", "github",
            "technical", "system", "card", "supplementary", "material", "materials", "new",
            "extended", "blog", "post",
        })
        # "Risk Report Appendix 2.25" and an inline citation to another
        # paper's "Appendix" are references, not this report's attachments.
        attachment_label = bool(re.fullmatch(
            r"(?:(?:download|read|view|see) )?(?:(?:the|our) )?"
            r"(?:supplement(?:ary|al)?(?: information| material| materials| appendix)?|supporting information|appendix|appendices)(?: pdf)?",
            label, re.I))
        own_attachment = bool(re.search(
            r"\b(?:our|this (?:paper|report|study)s?) (?:supplement(?:ary|al)?(?: information| material| materials)?|appendix|appendices)\b",
            context, re.I))
        supplementary = attachment_label and (button_like or own_attachment)
        if label in {"here", "download", "pdf"}:
            heading_declares_attachment = bool(re.match(r"^(?:appendix|appendices|supplementary|supplemental|supporting information)\b", link["heading"], re.I))
            paragraph_declares_attachment = bool(re.match(
                r"^(?:(?:our|the) )?(?:supplement(?:ary|al)?(?: information| material| materials)?|supporting information)\b.{0,140}(?:available|provided|download|found)",
                context, re.I))
            supplementary = (heading_declares_attachment or paragraph_declares_attachment) and bool(re.search(r"available|provided|download|found|here", context, re.I))
        if supplementary:
            supplements.append((destination, link))
            continue
        own_context = bool(re.search(r"\b(?:our|this) (?:full |complete |new )?(?:paper|report|research|study)\b", context, re.I))
        explicit = label in labels and (button_like or own_context)
        if label in {"new research", "extended blog post", "read the research"}:
            explicit = urllib.parse.urlsplit(destination).hostname in {"alignment.anthropic.com", "transformer-circuits.pub"}
        # Team pages put a plain Paper button beside their title/byline. A
        # body/reference citation titled "paper" is not equivalent to that CTA.
        if label in {"paper", "pdf", "technical report", "system card", "model card"}:
            explicit = (link["position"] < 5000 and button_like) or own_context
        if label in {"our paper", "our report"}:
            explicit = True
        if label == "here" and re.search(r"(?:our|the) (?:full |complete )?(?:paper|report).*?here", context, re.I):
            explicit = True
        if link["position"] < 2500 and re.search(r"(?:arxiv|paper|report) link:\s*$", link["before"], re.I):
            explicit = True
        if explicit:
            primary.append((destination, link))
    metadata = parser.metadata.get("citation_pdf_url")
    # Publication metadata can point at an older manuscript version. An
    # explicit download button for the linked version takes precedence.
    if metadata and parser.metadata.get("citation_title") and not primary:
        destination = _document_url(url, metadata)
        if destination:
            primary.append((destination, {"text": "citation_pdf_url", "position": 0}))
    if primary:
        result["report_url"] = primary[0][0]
        result["related_urls"] = [url]
        result["evidence"].append({"kind": "primary_original", "url": primary[0][0],
                                   "label": primary[0][1]["text"], "method": "explicit_report_link"})
    for destination, link in supplements:
        if destination != result.get("report_url") and destination not in result["supplementary_urls"]:
            result["supplementary_urls"].append(destination)
            result["evidence"].append({"kind": "supplementary_original", "url": destination,
                                       "label": link["text"], "method": "explicit_supplementary_link"})
    return result


TEAM_MAIN_SLUGS = {
    "https://alignment.anthropic.com/2026/automated-alignment-researchers/": "automated-researchers-mitigate-alignment-failures",
    "https://alignment.anthropic.com/2026/modular-pretraining/": "off-switch-dual-use",
    "https://alignment.anthropic.com/2026/teaching-claude-why/": "teaching-claude-why",
    "https://alignment.anthropic.com/2026/automated-w2s-researcher/": "automated-alignment-researchers",
    "https://transformer-circuits.pub/2026/workspace/index.html": "global-workspace",
    "https://transformer-circuits.pub/2026/nla/index.html": "natural-language-autoencoders",
    "https://transformer-circuits.pub/2026/emotions/index.html": "emotion-concepts-function",
}


def _team_index(page, base):
    """Both official team indexes are fully rendered, grouped by month."""
    month = None
    pattern = r'<div\s+class="date">([^<]+)</div>|<a\s+([^>]*href="[^"]+"[^>]*)>(.*?)</a>'
    for match in re.finditer(pattern, page, re.S):
        if match.group(1):
            try:
                month = dt.datetime.strptime(match.group(1).strip(), "%B %Y").date().isoformat()
            except ValueError:
                month = None
            continue
        attrs, body = match.group(2), match.group(3)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", body, re.S)
        if not month or not title:
            continue
        href = re.search(r'href="([^"]+)"', attrs).group(1)
        if not re.match(r"20\d\d/", href):
            continue
        exact = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', attrs)
        description = re.search(r'<div\s+class="description">(.*?)</div>', body, re.S)
        clean = lambda value: " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
        yield {"url": urllib.parse.urljoin(base, href), "title": clean(title.group(1)),
               "published_at": exact.group(1) if exact else month,
               "date_precision": "day" if exact else "month",
               "date_source": base, "discovery_overview": clean(description.group(1)) if description else ""}


def _team_publication_date(page):
    # Only the byline is a publication-date source; dates in experiments and
    # bibliography must not be mistaken for the publication day.
    byline = re.search(r'<div\s+class="section-authors"[^>]*>', page)
    if not byline:
        return None
    depth, end = 1, byline.end()
    for token in re.finditer(r"<div\b[^>]*>|</div\s*>", page[byline.end():], re.I):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            end = byline.end() + token.start()
            break
    text = re.sub(r"<[^>]+>", " ", page[byline.end():end])
    match = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d\d)\b", text)
    if match:
        return dt.datetime.strptime(" ".join(match.groups()), "%B %d %Y").date().isoformat()
    return None


def _anthropic_team_sites(start, end, existing=()):
    items, errors, inspected, candidates, duplicates = [], [], [], [], []
    existing_by_slug = {item["url"].rstrip("/").rsplit("/", 1)[-1]: item for item in existing}
    sources = [("https://alignment.anthropic.com/", "Anthropic Alignment Science"),
               ("https://transformer-circuits.pub/", "Anthropic Transformer Circuits")]
    for base, source in sources:
        try:
            index = _fetch(base)
            inspected.append(base)
            for row in _team_index(index, base):
                date = row["published_at"]
                if row["date_precision"] == "day":
                    in_window = start <= date <= end
                else:
                    in_window = start[:7] <= date[:7] <= end[:7]
                if not in_window:
                    continue
                existing_item = existing_by_slug.get(TEAM_MAIN_SLUGS.get(row["url"], ""))
                if existing_item:
                    duplicates.append({"team_url": row["url"], "canonical_url": existing_item["url"]})
                    existing_item.setdefault("related_urls", []).append(row["url"])
                    continue
                candidates.append({**row, "source": source, "vendor": "Anthropic", "language": "en",
                                   "content_type": "research_note" if "Updates" in row["title"] else "research",
                                   "read_status": "pending_full_text"})
        except Exception as exc:
            errors.append({"source": base, "error": type(exc).__name__ + ": " + str(exc)})

    def verify(row):
        try:
            page = _fetch(row["url"])
            date = _team_publication_date(page)
            if date and row["date_precision"] != "day":
                row.update(published_at=date, published_at_raw=date, date_precision="day", date_source=row["url"])
            original = primary_original(row["url"], page)
            row.update({key: value for key, value in original.items() if value})
            # A month-only date touching a partial boundary cannot establish
            # inclusion. The linked paper's official publication metadata can.
            if row["date_precision"] == "month" and ((row["published_at"][:7] == start[:7] and start[8:] != "01") or row["published_at"][:7] == end[:7]):
                report = row.get("report_url", "")
                if re.search(r"arxiv\.org/pdf/", report):
                    abstract_url = report.replace("/pdf/", "/abs/")
                    abstract = _fetch(abstract_url)
                    parsed = _ReportLinks(); parsed.feed(abstract)
                    raw_date = parsed.metadata.get("citation_date", "").replace("/", "-")
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                        row.update(published_at=raw_date, published_at_raw=raw_date, date_precision="day", date_source=abstract_url)
                if row["date_precision"] == "month":
                    raise ValueError("publication month overlaps a partial date boundary; exact publication day unavailable")
            if row["date_precision"] == "day" and not start <= row["published_at"] <= end:
                return None, None
            row.setdefault("published_at_raw", row["published_at"][:7] if row["date_precision"] == "month" else row["published_at"])
            return row, None
        except Exception as exc:
            return None, {"source": row["url"], "error": type(exc).__name__ + ": " + str(exc)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, error in pool.map(verify, candidates):
            if row:
                items.append(row)
            if error:
                errors.append(error)
    return {"items": items, "errors": errors,
            "coverage": {"sources": inspected, "team_items_added": len(items), "duplicates": duplicates,
                         "method": "Complete official team indexes; explicit data-date or original byline; month precision retained when no day is published; partial boundary months require exact evidence.",
                         "complete": not errors}}


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (official research archive)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _next_data(page):
    pieces = []
    for match in re.finditer(r"self\.__next_f\.push\((.*?)\)</script>", page, re.S):
        try:
            entry = json.loads(match.group(1))
            if len(entry) > 1 and isinstance(entry[1], str):
                pieces.append(entry[1])
        except (ValueError, TypeError):
            continue
    payload = "".join(pieces)
    decoder = json.JSONDecoder()
    # RSC text records are length-prefixed and may abut a JSON record directly.
    # raw_decode avoids dropping the large transparency record in this case.
    for match in re.finditer(r"[0-9a-f]+:([\[{])", payload):
        try:
            value, _ = decoder.raw_decode(payload, match.start(1))
        except ValueError:
            continue
        yield from _walk(value)


def _spans(value):
    return "".join(x.get("text", "") for x in _walk(value) if x.get("_type") == "span")


def _astro(value):
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return _astro(value[1])
    if isinstance(value, list):
        return [_astro(child) for child in value]
    if isinstance(value, dict):
        return {key: _astro(child) for key, child in value.items()}
    return value


ANTHROPIC_EXCLUDE = {
    "donating-open-source-petri", "anthropic-institute-agenda",
    "economic-index-survey-announcement", "enabling-independent-research",
    "2028-ai-leadership", "AI-enabled-cyber-threats-mitre-attack",
}

OPENAI_EXCLUDE = {
    "introducing-openai-safety-fellowship", "teen-development-research-grants",
    "introducing-child-safety-blueprint", "introducing-trusted-contact-in-chatgpt",
    "advancing-youth-safety-in-emea", "our-commitment-to-community-safety",
    "why-teens-deserve-access-safe-ai", "bio-bug-bounty",
    "gpt-6-astra", "introducing-gpt-rosalind",
    "research-acceleration-view-inside-openai", "an-alien-mind",
}


def _anthropic(start, end):
    items, errors, inspected, excluded = [], [], [], []
    nodes_by_source = {}
    for name in ("research", "engineering", "news", "transparency"):
        url = "https://www.anthropic.com/" + name
        try:
            page = _fetch(url)
            nodes_by_source[name] = list(_next_data(page))
            inspected.append(url)
        except Exception as exc:
            errors.append({"source": url, "error": str(exc)})
    news_posts = {}
    for node in nodes_by_source.get("news", []):
        if node.get("publishedOn") and node.get("slug"):
            slug = node["slug"].get("current", "")
            news_posts[slug.lower()] = node
    for name in ("research", "engineering"):
        for node in nodes_by_source.get(name, []):
            if node.get("_type") not in {"post", "engineeringArticle"} or not node.get("publishedOn"):
                continue
            date = node["publishedOn"][:10]
            if not start <= date <= end:
                continue
            slug = (node.get("slug") or {}).get("current", "")
            if not slug:
                continue
            if slug in ANTHROPIC_EXCLUDE or (name == "engineering" and slug not in {"infrastructure-noise", "how-we-contain-claude", "claude-code-auto-mode", "managed-agents"}):
                excluded.append(slug)
                continue
            directories = node.get("directories") or []
            directory = name
            if name == "research" and not any("research" in str(d) for d in directories):
                directory = "news" if slug in news_posts else name
            items.append({"title": node.get("title", slug).strip(), "vendor": "Anthropic", "source": "Anthropic Research" if name == "research" else "Anthropic Engineering", "published_at": date, "published_at_raw": node["publishedOn"], "date_precision": "day", "url": "https://www.anthropic.com/" + directory + "/" + slug, "content_type": "research" if name == "research" else "engineering_research", "language": "en", "discovery_overview": node.get("summary", ""), "read_status": "pending_full_text"})
    card_urls = set()
    for node in nodes_by_source.get("transparency", []):
        if node.get("_type") != "table" or "Summary Table" not in str(node.get("title", "")):
            continue
        model = node["title"].replace(" Summary Table", "")
        rows = node.get("rows") or []
        release = ""
        primary = None
        related = []
        for row in rows:
            label = _spans((row.get("cells") or [{}])[0])
            if label == "Release date":
                release = _spans(row)[len(label):].strip()
            if label == "Benchmarked Capabilities":
                primary = next((x.get("href") for x in _walk(row) if x.get("_type") == "link" and ".pdf" in x.get("href", "")), None)
        if not primary:
            continue
        try:
            month_date = dt.datetime.strptime(release, "%B %Y").date().isoformat()
        except ValueError:
            continue
        # Recover the exact model publication day from the official news index.
        model_slug = model.lower().replace(".", "-").replace(" ", "-")
        news = news_posts.get(model_slug)
        if not news:
            news = next((p for p in news_posts.values() if model.lower() in p.get("title", "").lower()), None)
        date = news["publishedOn"][:10] if news else month_date
        in_window = start <= date <= end if news else start[:7] <= date[:7] <= end[:7]
        if not in_window or primary in card_urls:
            continue
        card_urls.add(primary)
        related = list(dict.fromkeys(x.get("href") for x in _walk(node) if x.get("_type") == "link" and ".pdf" in x.get("href", "")))
        title = model + " System Card"
        if model in {"Claude Fable 5", "Claude Mythos 5"}:
            title = "Claude Fable 5 & Claude Mythos 5 System Card"
        if "Fable%20" in primary:
            title = urllib.parse.unquote(primary.rsplit("/", 1)[-1]).removesuffix(".pdf")
        items.append({"title": title, "vendor": "Anthropic", "source": "Anthropic Transparency Hub", "published_at": date, "published_at_raw": news["publishedOn"] if news else release, "date_precision": "day" if news else "month", "url": primary, "report_url": primary, "related_urls": related, "content_type": "system_card", "language": "en", "read_status": "pending_full_text"})
    team = _anthropic_team_sites(start, end, items)
    items.extend(team["items"])
    errors.extend(team["errors"])
    inspected.extend(team["coverage"]["sources"])
    unique = {item["url"]: item for item in items}
    return {"items": sorted(unique.values(), key=lambda item: (item["published_at"], item["title"]), reverse=True), "errors": errors, "coverage": {"start_date": start, "end_date": end, "sources": inspected, "method": "Complete embedded public publication lists; transparency model tables and exact official news publication dates; complete Alignment Science and Transformer Circuits indexes with original publication evidence.", "team_sites": team["coverage"], "excluded_slugs": sorted(set(excluded)), "complete": not errors}}


def _openai(start, end):
    items, errors, inspected, excluded = [], [], [], []
    safety_titles = set()
    hub = "https://deploymentsafety.openai.com/"
    try:
        page = _fetch(hub)
        inspected.append(hub)
        for match in re.finditer(r'<astro-island[^>]+props="([^"]+)"', page):
            data = _astro(json.loads(html.unescape(match.group(1))))
            for node in data.get("items", []):
                if not isinstance(node, dict) or not node.get("published") or not start <= node.get("date", "") <= end:
                    continue
                safety_titles.add(node["title"].lower())
                items.append({"title": node["title"], "vendor": "OpenAI", "source": "OpenAI Deployment Safety Hub", "published_at": node["date"], "date_precision": "day", "url": urllib.parse.urljoin(hub, node["href"]), "content_type": "system_card", "language": "en", "discovery_overview": node.get("excerpt") if isinstance(node.get("excerpt"), str) else "", "read_status": "pending_full_text"})
    except Exception as exc:
        errors.append({"source": hub, "error": str(exc)})
    rss = "https://openai.com/news/rss.xml"
    scanned = 0
    try:
        root = ET.fromstring(_fetch(rss))
        inspected.append(rss)
        for node in root.findall("./channel/item"):
            scanned += 1
            raw_date = node.findtext("pubDate", "")
            try:
                date = parsedate_to_datetime(raw_date).date().isoformat()
            except (ValueError, TypeError):
                continue
            if not start <= date <= end:
                continue
            categories = {x.text for x in node.findall("category")}
            title, url = node.findtext("title", ""), node.findtext("link", "")
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            if not categories.intersection({"Research", "Publication", "Engineering", "Safety", "Security"}):
                continue
            is_card = bool(re.search(r"system card|model card", title, re.I))
            is_technical = bool(categories.intersection({"Research", "Publication", "Engineering"}))
            if categories.intersection({"Safety", "Security"}):
                is_technical = is_technical or slug in {"towards-safety-cases-for-frontier-ai-training", "model-misalignment-reporting-framework", "third-party-cyber-evaluations-involving-openai-models", "unlocking-self-improvement-gpt-red", "safety-alignment-long-horizon-models", "chatgpt-recognize-context-in-sensitive-conversations", "running-codex-safely"}
            if slug in OPENAI_EXCLUDE or not (is_card or is_technical):
                excluded.append(slug)
                continue
            if title.lower() in safety_titles:
                matching = next(item for item in items if item["title"].lower() == title.lower())
                matching.setdefault("related_urls", []).append(url)
                continue
            content_type = "system_card" if is_card else "engineering_research" if "Engineering" in categories else "research"
            items.append({"title": title, "vendor": "OpenAI", "source": "OpenAI Research", "published_at": date, "published_at_raw": raw_date, "date_precision": "day", "url": url, "content_type": content_type, "language": "en", "discovery_overview": node.findtext("description", ""), "read_status": "pending_full_text"})
    except Exception as exc:
        errors.append({"source": rss, "error": str(exc)})
    unique = {item["url"]: item for item in items}
    return {"items": sorted(unique.values(), key=lambda item: (item["published_at"], item["title"]), reverse=True), "errors": errors, "coverage": {"start_date": start, "end_date": end, "sources": inspected, "rss_items_scanned": scanned, "method": "Complete RSS plus the full public deployment safety list (including items behind View more); research/engineering articles selected for technical methods or evaluations; commercial and program announcements excluded.", "excluded_slugs": sorted(set(excluded)), "complete": not errors}}


def discover(vendor, start_date, end_date):
    """Return items/errors/coverage; dates are inclusive ISO calendar dates."""
    vendor = vendor.lower().replace(" ", "")
    if vendor == "anthropic":
        return _anthropic(str(start_date), str(end_date))
    if vendor == "openai":
        return _openai(str(start_date), str(end_date))
    raise ValueError("adapters_west supports Anthropic and OpenAI")
