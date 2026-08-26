#!/usr/bin/env python3
"""Merge an Agent-cleaned collection batch into a per-day local archive."""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import run_ai_news as core


REQUIRED_FIELDS = ("资讯标题", "发布时间", "来源", "超链接")
TRACKING_QUERY_FIELDS = {
    "from",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def input_items(payload):
    if isinstance(payload, list):
        return payload
    for field in ("items", "selected_items", "filtered_items"):
        if field in payload:
            if not isinstance(payload[field], list):
                raise RuntimeError(f"{field} must be an array")
            return payload[field]
    raise RuntimeError("archive input must contain items, selected_items, filtered_items, or be a JSON array")


def normalized_url(value):
    value = (value or "").strip()
    parts = urlsplit(value)
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_FIELDS and not key.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalized_title(value):
    return re.sub(r"\W+", "", (value or "").lower())


def item_keys(item):
    url = normalized_url(item.get("超链接"))
    title = normalized_title(item.get("资讯标题"))
    keys = []
    if url:
        keys.append(f"url:{url}")
    if title:
        keys.append(f"title:{title}")
    return keys


def validate_item(item, index):
    if not isinstance(item, dict):
        raise RuntimeError(f"item {index} must be an object")
    missing = [field for field in REQUIRED_FIELDS if not str(item.get(field, "")).strip()]
    if missing:
        raise RuntimeError(f"item {index} missing required fields: {', '.join(missing)}")
    url = normalized_url(item["超链接"])
    if not re.match(r"^https?://", url, re.I):
        raise RuntimeError(f"item {index} has invalid HTTP(S) URL: {item['超链接']}")
    try:
        datetime.strptime(item["发布时间"], "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise RuntimeError(f"item {index} has invalid 发布时间: {item['发布时间']}") from exc
    clean = dict(item)
    clean["超链接"] = url
    clean.pop("dt", None)
    clean.pop("id", None)
    return clean


def archive_id(date_text, item):
    identity = normalized_url(item.get("超链接")) or normalized_title(item.get("资讯标题"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"A{date_text.replace('-', '')}-{digest}"


def merge_archive(existing, incoming, date_text, collected_at):
    merged = []
    key_to_index = {}

    def add_keys(item, index):
        for key in item_keys(item):
            key_to_index[key] = index

    for raw in existing:
        item = dict(raw)
        index = len(merged)
        merged.append(item)
        add_keys(item, index)

    added = 0
    updated = 0
    skipped = 0
    for item in incoming:
        match = next((key_to_index[key] for key in item_keys(item) if key in key_to_index), None)
        if match is None:
            item["archive_id"] = item.get("archive_id") or archive_id(date_text, item)
            item["first_seen_at"] = collected_at
            item["last_seen_at"] = collected_at
            index = len(merged)
            merged.append(item)
            add_keys(item, index)
            added += 1
            continue

        current = merged[match]
        changed = False
        for field, value in item.items():
            if value and not current.get(field):
                current[field] = value
                changed = True
        current["last_seen_at"] = collected_at
        updated += 1 if changed else 0
        skipped += 0 if changed else 1
        add_keys(current, match)

    merged.sort(key=lambda item: (item.get("发布时间", ""), item.get("资讯标题", "")), reverse=True)
    return merged, added, updated, skipped


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Append Agent-cleaned AI news to today's local archive.")
    parser.add_argument("--input", default="tmp_ai_news_filtered.json", help="Agent-cleaned JSON batch.")
    parser.add_argument("--archive-dir", default="ai_news_daily", help="Directory for YYYY-MM-DD.json archives.")
    parser.add_argument("--date", help="Archive date in YYYY-MM-DD; defaults to Asia/Shanghai today.")
    parser.add_argument("--result", default="ai_news_collect_result.json", help="Collection result JSON path.")
    parser.add_argument("--cleanup", nargs="*", default=[], help="Files to delete after a successful merge.")
    args = parser.parse_args()

    date_text = args.date or core.TODAY
    datetime.strptime(date_text, "%Y-%m-%d")
    collected_at = datetime.now(core.TZ).strftime("%Y-%m-%d %H:%M:%S")
    payload = load_json(args.input)
    cleaned = [validate_item(item, index) for index, item in enumerate(input_items(payload), 1)]

    archive_path = Path(args.archive_dir) / f"{date_text}.json"
    if archive_path.exists():
        archive_payload = load_json(archive_path)
        existing = archive_payload.get("items", [])
        runs = archive_payload.get("runs", [])
    else:
        existing = []
        runs = []

    merged, added, updated, skipped = merge_archive(existing, cleaned, date_text, collected_at)
    runs.append({
        "collected_at": collected_at,
        "input_count": len(cleaned),
        "added_count": added,
        "updated_count": updated,
        "skipped_duplicate_count": skipped,
        "selection_reason": payload.get("selection_reason", "") if isinstance(payload, dict) else "",
        "source_status": payload.get("source_status", {}) if isinstance(payload, dict) else {},
    })
    archive = {
        "version": 1,
        "date": date_text,
        "timezone": "Asia/Shanghai",
        "updated_at": collected_at,
        "item_count": len(merged),
        "runs": runs,
        "items": merged,
    }
    write_json_atomic(archive_path, archive)

    cleanup_removed = []
    for value in [args.input, *args.cleanup]:
        target = Path(value)
        if target.exists() and target.resolve() != archive_path.resolve():
            target.unlink()
            cleanup_removed.append(str(target))

    result = {
        "ok": True,
        "date": date_text,
        "archive_file": str(archive_path),
        "input_count": len(cleaned),
        "added_count": added,
        "updated_count": updated,
        "skipped_duplicate_count": skipped,
        "archive_count": len(merged),
        "cleanup_removed": cleanup_removed,
    }
    write_json_atomic(Path(args.result), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
