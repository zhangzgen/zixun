"""Idempotent vendor/report wiki publication; never sends a chat message."""
import json
import time
import urllib.parse
import uuid
from pathlib import Path

import run_ai_news as core
from .common import ROOT, VENDORS, digest, now, write_json
from .renderer import section_blocks
from .store import ARCHIVE, export_search, load, latest, is_fully_read, omitted_pdf_pages

REPORT_PARENT = 'BPXvwO7TyiT8kukRTHecuViMnZb'
STATE = ROOT / '.technical_reports' / 'feishu_state.json'


def api(token, route, method='GET', body=None):
    result = core.api_json('https://open.feishu.cn/open-apis/' + route, method, body, token=token)
    if result.get('code') != 0:
        raise RuntimeError(f"Feishu {route.split('?')[0]}: code={result.get('code')} msg={result.get('msg')}")
    return result.get('data', {})


def node(token, key):
    return api(token, 'wiki/v2/spaces/get_node?token=' + urllib.parse.quote(key))['node']


def children(token, space, parent):
    result, cursor = [], ''
    while True:
        query = urllib.parse.urlencode({'parent_node_token': parent, 'page_size': 50, 'page_token': cursor})
        data = api(token, f'wiki/v2/spaces/{space}/nodes?{query}')
        result.extend(data.get('items') or data.get('nodes') or [])
        if not data.get('has_more'):
            return result
        cursor = data.get('page_token')
        if not cursor:
            raise RuntimeError('Wiki pagination returned has_more without page_token')


def ensure_node(token, space, parent, title, known):
    found = next((item for item in known if item.get('title') == title), None)
    if found:
        return found
    data = api(token, f'wiki/v2/spaces/{space}/nodes', 'POST', {
        'obj_type': 'docx', 'node_type': 'origin', 'parent_node_token': parent, 'title': title})
    found = data['node']
    known.append(found)
    return found


def write_blocks(token, document, blocks, replace=False):
    old_count = 0
    if replace:
        existing = core.list_doc_blocks(token, document)
        root = next(b for b in existing if b['block_id'] == document)
        old_count = len(root.get('children', []))
    results = []
    for start in range(0, len(blocks), 40):
        data = api(token, f'docx/v1/documents/{document}/blocks/{document}/children?client_token={uuid.uuid4()}',
                   'POST', {'children': blocks[start:start + 40], 'index': -1})
        results.extend(data.get('children', []))
        time.sleep(.2)
    # Keep the previous content until the entire replacement has been accepted.
    if replace and old_count:
        api(token, f'docx/v1/documents/{document}/blocks/{document}/children/batch_delete', 'DELETE',
            {'start_index': 0, 'end_index': old_count})
    return results


def update_daily_section(token, document, reports):
    """Only replace our H3 section, preserving all existing news blocks."""
    blocks = core.list_doc_blocks(token, document)
    root = next(b for b in blocks if b['block_id'] == document)
    by_id = {b['block_id']: b for b in blocks}
    ids = root.get('children', [])
    ranges, start = [], None
    for index, key in enumerate(ids):
        block = by_id.get(key, {})
        if block.get('block_type') == 5:
            if start is not None:
                ranges.append((start, index))
                start = None
            text = ''.join(e.get('text_run', {}).get('content', '') for e in block['heading3'].get('elements', []))
            if text == '🟢 技术报告':
                start = index
    if start is not None:
        ranges.append((start, len(ids)))
    write_blocks(token, document, section_blocks(reports))
    # A previous interrupted append may have left more than one section.
    # Remove every older copy backwards, leaving unrelated sections in place.
    for start, end in reversed(ranges):
        api(token, f'docx/v1/documents/{document}/blocks/{document}/children/batch_delete', 'DELETE',
            {'start_index': start, 'end_index': end})


def reconcile_retired_documents(token, space, archive, state):
    """Label retained related articles and exclusions without deleting evidence."""
    from .renderer import heading, paragraph, report_blocks
    current = {i['id']: i for i in archive['items']}
    errors = []
    for uid, primary in archive.get('aliases', {}).items():
        old = archive.get('merged_reports', {}).get(uid, {})
        cached = state.get('reports', {}).get(uid) or state.get('retired_reports', {}).get(uid, {})
        target = current.get(primary, {})
        document = old.get('feishu_document_id') or cached.get('document')
        if not document or document == target.get('feishu_document_id') or not target.get('feishu_url'):
            continue
        title = ('【关联发布文章·已合并】' + old.get('title', uid))[:220]
        blocks = [heading(3, '关联发布文章'),
                  {'block_type': 2, 'text': core.text_data([
                      core.text_run('同一报告的正式版本已归入：'),
                      core.text_run(target.get('title', primary), link=target['feishu_url'])])},
                  paragraph('此处保留发布文章的原有概述和阅读总结；技术报告目录及每日最新15份按正式版本计数。')]
        if old.get('overview') and old.get('summary'):
            blocks.extend(report_blocks(old))
        notice_hash = digest(json.dumps([title, blocks], ensure_ascii=False, sort_keys=True))
        if cached.get('retirement_render_hash') == notice_hash:
            continue
        try:
            if old.get('feishu_node_token'):
                api(token, f"wiki/v2/spaces/{space}/nodes/{old['feishu_node_token']}/update_title", 'POST', {'title': title})
            write_blocks(token, document, blocks, replace=True)
            state.setdefault('retired_reports', {})[uid] = {**cached, 'document': document,
                'merged_into': primary, 'retirement_render_hash': notice_hash, 'synced_at': now()}
            state.get('reports', {}).pop(uid, None)
            write_json(STATE, state)
        except Exception as exc:
            errors.append({'id': uid, 'error': str(exc)[:300]})
    for uid, item in archive.get('excluded_reports', {}).items():
        cached = state.get('excluded_reports', {}).get(uid) or state.get('reports', {}).get(uid, {})
        document = item.get('feishu_document_id') or cached.get('document')
        if not document:
            continue
        title = ('【非技术报告·已排除】' + item.get('title', uid))[:220]
        blocks = [heading(3, '已排除的公告'),
                  paragraph(item.get('excluded_reason') or '经原文核查，该内容不属于技术报告。'),
                  paragraph('保留此文档用于来源审计；已从技术报告目录、检索和每日最新15份中移除。'),
                  heading(4, item.get('title', uid), link=item.get('url'))]
        if item.get('overview') and item.get('summary'):
            blocks.extend(report_blocks(item))
        notice_hash = digest(json.dumps([title, blocks], ensure_ascii=False, sort_keys=True))
        if cached.get('exclusion_render_hash') == notice_hash:
            continue
        try:
            if item.get('feishu_node_token'):
                api(token, f"wiki/v2/spaces/{space}/nodes/{item['feishu_node_token']}/update_title", 'POST', {'title': title})
            write_blocks(token, document, blocks, replace=True)
            state.setdefault('excluded_reports', {})[uid] = {**cached, 'document': document,
                'exclusion_render_hash': notice_hash, 'needs_label': False, 'synced_at': now()}
            state.get('reports', {}).pop(uid, None)
            write_json(STATE, state)
        except Exception as exc:
            errors.append({'id': uid, 'error': str(exc)[:300]})
    return errors


def label_incomplete_original(token, item, state):
    cached = state.get('reports', {}).get(item['id'], {})
    document = item.get('feishu_document_id') or cached.get('document')
    if not document or not (item.get('original_quality_error') or omitted_pdf_pages(item)):
        return False
    from .renderer import paragraph
    notice = paragraph('⚠️ 全文阅读待补齐：原文图表或页面尚未完整核对。以下为此前暂存内容，不能视为已完成全文阅读的总结。')
    notice_hash = digest(json.dumps(notice, ensure_ascii=False, sort_keys=True))
    if cached.get('pending_quality_hash') == notice_hash:
        return False
    api(token, f'docx/v1/documents/{document}/blocks/{document}/children?client_token={uuid.uuid4()}',
        'POST', {'children': [notice], 'index': 0})
    state.setdefault('reports', {})[item['id']] = {**cached, 'document': document,
        'pending_quality_hash': notice_hash, 'quality_labelled_at': now()}
    return True


def sync(path=ARCHIVE, update_today=True):
    archive = load(path)
    token = core.tenant_token()
    parent = node(token, REPORT_PARENT)
    space = parent['space_id']
    existing_vendors = children(token, space, REPORT_PARENT)
    state = json.loads(STATE.read_text()) if STATE.exists() else {'vendors': {}, 'reports': {}}
    errors, published, pending_labelled = [], 0, 0
    for vendor in VENDORS:
        directory = ensure_node(token, space, REPORT_PARENT, vendor + ' 技术报告', existing_vendors)
        state['vendors'][vendor] = directory
        write_json(STATE, state)
        report_nodes = children(token, space, directory['node_token'])
        records = [i for i in archive['items'] if i['vendor'] == vendor]
        for item in records:
            if not is_fully_read(item):
                try:
                    if label_incomplete_original(token, item, state):
                        pending_labelled += 1
                        write_json(STATE, state)
                except Exception as exc:
                    errors.append({'id': item['id'], 'vendor': vendor, 'error': str(exc)[:300]})
                continue
            try:
                title = f"{item['published_at'][:10]} · {item['title']}"[:220]
                cached = state['reports'].get(item['id'], {})
                target = next((n for n in report_nodes if n.get('node_token') == item.get('feishu_node_token')
                               or n.get('obj_token') == (item.get('feishu_document_id') or cached.get('document'))), None)
                if target:
                    if target.get('title') != title:
                        api(token, f"wiki/v2/spaces/{space}/nodes/{target['node_token']}/update_title", 'POST', {'title': title})
                        target['title'] = title
                else:
                    target = ensure_node(token, space, directory['node_token'], title, report_nodes)
                item['feishu_node_token'] = target['node_token']
                item['feishu_document_id'] = target['obj_token']
                item['feishu_url'] = 'https://ocns5gikjtax.feishu.cn/wiki/' + target['node_token']
                rendered = section_blocks([item], folded=False)
                # Layout changes (including folding) must invalidate the cache.
                content_hash = digest(json.dumps(rendered, ensure_ascii=False, sort_keys=True))
                if cached.get('render_hash') != content_hash or cached.get('document') != target['obj_token'] or cached.get('pending_quality_hash'):
                    write_blocks(token, target['obj_token'], rendered, replace=True)
                    state['reports'][item['id']] = {'document': target['obj_token'], 'render_hash': content_hash, 'synced_at': now()}
                    published += 1
                    write_json(STATE, state)
                    write_json(path, archive)
            except Exception as exc:
                errors.append({'id': item['id'], 'vendor': vendor, 'error': str(exc)[:300]})
        # The vendor directory is a complete catalogue, including unresolved originals.
        from .renderer import heading, paragraph
        catalogue = [paragraph(f"已收录 {len(records)} 份；其中 {sum(is_fully_read(i) for i in records)} 份已完成全文阅读。")]
        for item in records:
            catalogue.append(heading(4, f"{item['published_at'][:10]} · {item['title']}", link=item.get('feishu_url') or item['url']))
            catalogue.append(paragraph(item.get('overview') if is_fully_read(item) else '已发现报告；原文完整读取（包括图表）和总结尚待完成。'))
        index_hash = digest(json.dumps(catalogue, ensure_ascii=False, sort_keys=True))
        if state.get('index_hashes', {}).get(vendor) != index_hash:
            write_blocks(token, directory['obj_token'], catalogue, replace=True)
            state.setdefault('index_hashes', {})[vendor] = index_hash
            write_json(STATE, state)
    errors.extend(reconcile_retired_documents(token, space, archive, state))
    write_json(path, archive)
    export_search(path)
    today_url = None
    if update_today:
        news_parent = core.get_parent_node(token)
        target = next((n for n in core.list_child_nodes(token, news_parent['space_id']) if n.get('title') == f'📖 {core.TODAY} 要闻'), None)
        if target:
            update_daily_section(token, target['obj_token'], latest(15, path))
            today_url = 'https://ocns5gikjtax.feishu.cn/wiki/' + target['node_token']
    result = {'ok': not errors, 'updated_at': now(), 'published': published, 'pending_labelled': pending_labelled, 'catalogue_count': len(archive['items']),
              'latest_count': len(latest(15, path)), 'errors': errors, 'today_url': today_url,
              'directories': {v: 'https://ocns5gikjtax.feishu.cn/wiki/' + n['node_token'] for v, n in state['vendors'].items()}}
    write_json(ROOT / 'technical_reports' / 'sync_result.json', result)
    return result
