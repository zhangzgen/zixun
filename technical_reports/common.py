"""Small shared helpers; never truncate report originals."""
import gzip
import hashlib
import html
import json
import re
import urllib.request
import tempfile
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))
VENDORS = ('Anthropic', 'OpenAI', 'DeepSeek', 'Kimi', 'GLM', 'Google', 'Seed')


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fetch_bytes(url, timeout=45):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        if response.headers.get('Content-Encoding') == 'gzip' or data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        return data, response.url, response.headers.get('Content-Type', '')


def fetch_html(url, timeout=45):
    return fetch_bytes(url, timeout)[0].decode('utf-8', 'replace')


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.links, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag in ('p', 'h1', 'h2', 'h3', 'h4', 'li', 'tr', 'br'):
            self.parts.append('\n')
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def strip_html(value):
    parser = HTMLText()
    parser.feed(value)
    return re.sub(r'\n\s*\n+', '\n\n', html.unescape(' '.join(parser.parts))).strip()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            handle.flush()
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
