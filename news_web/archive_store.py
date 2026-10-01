"""Incremental, disposable SQLite index. Source JSON archives are never written."""

import hashlib
import html
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit


def plain(value, limit=16000):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))[:limit].strip()


def tokens(value):
    """Bigrams preserve Chinese search without an external segmentation service."""
    result = []
    for word in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", value.lower()):
        if re.search(r"[\u3400-\u9fff]", word) and len(word) > 1:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


def valid_date(value):
    if value:
        date.fromisoformat(value)
    return value


class ArchiveStore:
    def __init__(self, archive_dir: Path, database_path: Path):
        self.archive_dir = archive_dir
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.last_check = 0.0
        self.warnings = []
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files (
                    name TEXT PRIMARY KEY, signature TEXT NOT NULL, archive_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS news (
                    id TEXT PRIMARY KEY, file TEXT NOT NULL, archive_date TEXT NOT NULL,
                    title TEXT NOT NULL, published_at TEXT NOT NULL, source TEXT NOT NULL,
                    url TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL,
                    content_kind TEXT NOT NULL, search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS news_file ON news(file);
                CREATE INDEX IF NOT EXISTS news_date ON news(published_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(search_text);
                CREATE TRIGGER IF NOT EXISTS news_ai AFTER INSERT ON news BEGIN
                    INSERT INTO news_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
                END;
                CREATE TRIGGER IF NOT EXISTS news_ad AFTER DELETE ON news BEGIN
                    DELETE FROM news_fts WHERE rowid = old.rowid;
                END;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def refresh(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_check < 10:
                return
            warnings = []
            paths = sorted(p for p in self.archive_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
            with self.connect() as db:
                signatures = {row["name"]: row["signature"] for row in db.execute("SELECT * FROM files")}
                for path in paths:
                    stat = path.stat()
                    signature = f"{stat.st_mtime_ns}:{stat.st_size}"
                    if signatures.get(path.name) == signature:
                        continue
                    try:
                        valid_date(path.stem)
                        data = json.loads(path.read_text(encoding="utf-8"))
                        items = data if isinstance(data, list) else data["items"]
                        if not isinstance(items, list):
                            raise ValueError("invalid items")
                        rows = []
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            title = plain(item.get("资讯标题"), 1000)
                            url = str(item.get("超链接", "")).strip()
                            parts = urlsplit(url)
                            if not title or parts.scheme not in ("http", "https") or not parts.netloc:
                                continue
                            uid = str(item.get("archive_id") or "A" + path.stem.replace("-", "") + "-" + hashlib.sha256(url.encode()).hexdigest()[:12])
                            summary = plain(item.get("desc") or item.get("资讯简介"), 4000)
                            body = plain(item.get("正文") or item.get("rss_content") or item.get("content"))
                            source = plain(item.get("来源"), 200)
                            published_at = plain(item.get("发布时间"), 40)
                            if not re.match(r"\d{4}-\d{2}-\d{2}", published_at):
                                published_at = path.stem + " 00:00:00"
                            rows.append((uid, path.name, path.stem, title, published_at, source, url, summary, body or summary,
                                         "正文" if body else "摘要", " ".join(tokens(title + " " + source + " " + summary + " " + body))))
                    except (ValueError, KeyError, TypeError, OSError):
                        warnings.append({"file": path.name, "reason": "归档暂不可读，保留上次索引"})
                        continue
                    # Replace a complete day's index in one transaction, never partially.
                    db.execute("DELETE FROM news WHERE file = ?", (path.name,))
                    db.executemany("INSERT OR REPLACE INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (path.name, signature, path.stem))
                existing = {p.name for p in paths}
                for removed in set(signatures) - existing:
                    db.execute("DELETE FROM news WHERE file = ?", (removed,))
                    db.execute("DELETE FROM files WHERE name = ?", (removed,))
            self.warnings = warnings
            self.last_check = time.monotonic()

    @staticmethod
    def serialize(row, detail=False):
        result = {key: row[key] for key in ("id", "archive_date", "title", "published_at", "source", "url", "summary", "content_kind")}
        if detail:
            result["content"] = row["content"]
        return result

    def stats(self):
        with self.connect() as db:
            meta = dict(db.execute("SELECT COUNT(*) AS archive_days, MIN(archive_date) AS first_date, MAX(archive_date) AS last_date FROM files").fetchone())
            meta.update(dict(db.execute("SELECT COUNT(*) AS record_count, COUNT(DISTINCT url) AS article_count FROM news").fetchone()))
            meta["sources"] = [dict(row) for row in db.execute("SELECT source AS name, COUNT(DISTINCT url) AS count FROM news GROUP BY source ORDER BY count DESC")]
            meta["timeline"] = [dict(row) for row in db.execute("SELECT archive_date AS date, COUNT(*) AS count FROM news GROUP BY archive_date ORDER BY archive_date")]
        meta["warnings"] = self.warnings
        return meta

    def search(self, query="", start_date=None, end_date=None, source=None, limit=12, offset=0):
        valid_date(start_date)
        valid_date(end_date)
        if start_date and end_date and start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        # None requests the complete matching set for the chat's link list.
        limit = max(1, min(int(limit), 30)) if limit is not None else None
        offset = max(0, min(int(offset), 10000))
        clauses, params = [], []
        query_tokens = tokens(query)[:32]
        if query_tokens:
            clauses.append("n.rowid IN (SELECT rowid FROM news_fts WHERE news_fts MATCH ?)")
            params.append(" AND ".join('"' + word + '"' + ("*" if re.fullmatch(r"[a-z0-9_]+", word) else "") for word in query_tokens))
        elif query.strip():
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        if start_date:
            clauses.append("n.published_at >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("n.published_at < ?")
            params.append((date.fromisoformat(end_date) + timedelta(days=1)).isoformat())
        if source:
            clauses.append("n.source = ?")
            params.append(source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # One canonical result per URL, keeping the latest archived version.
        cte = f"""WITH ranked AS (
            SELECT n.*, ROW_NUMBER() OVER (PARTITION BY url ORDER BY archive_date DESC, published_at DESC, id DESC) AS duplicate
            FROM news n {where}
        ), unique_news AS (SELECT * FROM ranked WHERE duplicate = 1) """
        with self.connect() as db:
            total = db.execute(cte + "SELECT COUNT(*) FROM unique_news", params).fetchone()[0]
            rows = db.execute(cte + "SELECT * FROM unique_news ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?", params + [limit if limit is not None else -1, offset]).fetchall()
        return {"items": [self.serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def read(self, ids):
        ids = list(dict.fromkeys(ids))[:8]
        if not ids:
            return {"items": [], "missing_ids": []}
        with self.connect() as db:
            rows = db.execute("SELECT * FROM news WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
        indexed = {row["id"]: self.serialize(row, detail=True) for row in rows}
        return {"items": [indexed[uid] for uid in ids if uid in indexed], "missing_ids": [uid for uid in ids if uid not in indexed]}
