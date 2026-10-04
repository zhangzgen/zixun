"""Local two-phase Agent reading pipeline, isolated from AI news and reports."""
import fcntl
import json
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import run_ai_news as core
import task_email
from technical_reports.common import ROOT, TZ, digest, now, write_json
from technical_reports.feishu import children, ensure_node, node, write_blocks
from technical_reports.renderer import heading, paragraph, report_blocks
from .collector import discover

DATA = ROOT / '.agent_frameworks'
MANIFEST = Path(__file__).with_name('sources.json')
WIKI_PREFIX = 'https://ocns5gikjtax.feishu.cn/wiki/'


def load(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def manifest():
    return load(MANIFEST, {})


@contextmanager
def locked():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'pipeline.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def source_blocks():
    blocks = [paragraph('官方技术文章、研究栏目和版本日志。每天北京时间24:00采集，按框架说明技术变化及用途；仅收录开始监测后的更新。')]
    for framework in manifest()['frameworks']:
        blocks += [heading(2, framework['name']), paragraph(framework['focus'])]
        for source in framework['sources']:
            elements = [core.text_run('🔗 ' + source['name'] + '：'), core.text_run(source['url'], link=source['url'])]
            if source.get('collect_url'):
                elements += [core.text_run('\n订阅入口：'), core.text_run(source['collect_url'], link=source['collect_url'])]
            blocks.append({'block_type': 12, 'bullet': core.text_data(elements)})
    return blocks


def ensure_wiki(token=None):
    token = token or core.tenant_token()
    space = node(token, core.PARENT_NODE_TOKEN)['space_id']
    root_nodes = children(token, space, '')
    directory = ensure_node(token, space, '', 'Agent Framework', root_nodes)
    child_nodes = children(token, space, directory['node_token'])
    source_doc = ensure_node(token, space, directory['node_token'], 'Agent Framework Source', child_nodes)
    state = load(DATA / 'wiki.json', {})
    source_hash = digest(json.dumps(manifest(), sort_keys=True, ensure_ascii=False))
    if state.get('source_hash') != source_hash:
        write_blocks(token, source_doc['obj_token'], source_blocks(), replace=True)
    state.update({'space_id': space, 'directory': directory, 'source_document': source_doc, 'source_hash': source_hash,
                  'directory_url': WIKI_PREFIX + directory['node_token'], 'source_url': WIKI_PREFIX + source_doc['node_token']})
    write_json(DATA / 'wiki.json', state)
    return state


def collect(day=None):
    with locked():
        state = load(DATA / 'state.json', {'version': 1, 'sources': {}})
        active = state.get('active_run')
        if active:
            old = load(DATA / 'runs' / (active + '.json'), {})
            if not old.get('finished_at'):
                return {'run_id': active, 'run_path': str(DATA / 'runs' / (active + '.json')),
                        'candidate_count': len(old.get('candidates', [])), 'recovered': True,
                        'instruction': 'Resume this incomplete run; read candidates and finalize it before starting another.'}
        stamp = now()
        # At 00:00 the named daily issue covers the preceding day. A first manual
        # run explicitly passes today's date, establishing the no-backfill floor.
        if day is None:
            from datetime import timedelta
            day = (datetime.now(TZ) - timedelta(seconds=1) if datetime.now(TZ).hour == 0 else datetime.now(TZ)).date().isoformat()
        datetime.fromisoformat(day)
        floor = state.get('monitoring_started_at') or day + 'T00:00:00+08:00'
        state['monitoring_started_at'] = floor
        uid = 'agent-framework-' + datetime.now(TZ).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        path = DATA / 'runs' / (uid + '.json')
        report = {'run_id': uid, 'day': day, 'task': 'Agent Framework 每日技术更新采集', 'report_type': 'agent_frameworks',
                  'status': 'failed', 'started_at': stamp, 'summary': '本轮采集正在进行。', 'window_start': floor,
                  'window_end': stamp, 'source_status': [], 'errors': [], 'candidates': []}
        state['active_run'] = uid
        write_json(path, report)
        write_json(DATA / 'state.json', state)
        sources = [(f['name'], s) for f in manifest()['frameworks'] for s in f['sources']]

        def work(entry):
            framework, source = entry
            try:
                rows, checkpoint, failures = discover(source, state['sources'].get(source['id'], {}), floor, stamp)
                return framework, source, rows, checkpoint, failures, None
            except Exception as exc:
                return framework, source, [], None, [], core.safe_error(exc)

        merged = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(work, sources))
        for framework, source, rows, checkpoint, failures, error in results:
            coverage = {'framework': framework, 'source_id': source['id'], 'name': source['name'], 'url': source['url'],
                        'status': 'failed' if error else ('partial' if failures else 'success'),
                        'candidate_count': len(rows), 'errors': [error] if error else failures}
            report['source_status'].append(coverage)
            if error:
                report['errors'].append(framework + ' / ' + source['name'] + '：' + error)
            for failure in failures:
                report['errors'].append(framework + '：' + failure['url'] + ' ' + failure['error'])
            if checkpoint is not None:
                state['sources'][source['id']] = {**checkpoint, 'checked_at': stamp}
                if checkpoint.get('initial_undated_baseline'):
                    coverage['baseline_note'] = f"{len(checkpoint['initial_undated_baseline'])}篇既有文章未标注日期，仅建立基线，不作为今日更新。"
            for row in rows:
                # Deduplicate the same article appearing in category and main indexes.
                identity = digest(row['url'])[:24]
                body = row.pop('fulltext')
                body_path = DATA / 'fulltext' / (identity + '-' + digest(body)[:16] + '.txt')
                body_path.parent.mkdir(parents=True, exist_ok=True)
                body_path.write_text(body, encoding='utf-8')
                row.pop('embedded_body', None)
                row.update({'id': identity, 'framework': framework, 'source': framework + ' · ' + source['name'],
                            'source_id': source['id'], 'fulltext_path': str(body_path.relative_to(ROOT)),
                            'char_count': len(body), 'content_hash': digest(body)})
                row['source_ids'] = sorted(set(merged.get(identity, {}).get('source_ids', []) + [source['id']]))
                merged[identity] = row
        # Previously unread entries remain candidates even after discovery has
        # advanced its seen set. Source failures never erase a pending original.
        for item in load(DATA / 'pending.json', []):
            merged.setdefault(item['id'], item)
        report['candidates'] = sorted(merged.values(), key=lambda i: (i.get('event_at') or '', i['id']), reverse=True)
        report['collected_at'] = now()
        report['summary'] = f"采集完成，{len(sources)}个入口，{len(report['candidates'])}个候选，待Agent阅读与判断。"
        write_json(DATA / 'pending.json', report['candidates'])
        write_json(path, report)
        write_json(DATA / 'state.json', state)
        return {'run_id': uid, 'run_path': str(path), 'candidate_count': len(report['candidates']),
                'source_count': len(sources), 'error_count': len(report['errors']),
                'instruction': 'Read each fulltext_path in full. Write decisions JSON with one item per candidate: id, decision=include|exclude|pending; include requires overview, what_changed, usefulness, summary and reading_evidence (sha256, char_count, read_chars, reader=agent, read_at). Exclusions need reason. Then finalize --run PATH --decisions PATH. Never fabricate an update when no candidates exist.'}


def validate_decisions(candidates, decisions, root=ROOT):
    if not isinstance(decisions, list):
        raise ValueError('Decisions must be a JSON array')
    by_id = {item['id']: item for item in decisions}
    if len(by_id) != len(decisions) or set(by_id) != {item['id'] for item in candidates}:
        raise ValueError('Decisions must cover every candidate exactly once')
    included, excluded, pending = [], [], []
    for candidate in candidates:
        decision = by_id[candidate['id']]
        kind = decision.get('decision')
        if kind in ('exclude', 'pending'):
            if not decision.get('reason'):
                raise ValueError('Exclusion/pending requires a reason')
            (excluded if kind == 'exclude' else pending).append({**candidate, 'reason': decision['reason']})
            continue
        if kind != 'include':
            raise ValueError('Invalid decision')
        for field in ('overview', 'what_changed', 'usefulness', 'summary'):
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                raise ValueError('Missing concise technical summary field: ' + field)
        if len(decision['overview']) > 260 or len(decision['summary']) > 1600:
            raise ValueError('Keep overview and summary concise')
        path = (Path(root) / candidate['fulltext_path']).resolve()
        if not path.is_relative_to(Path(root).resolve()):
            raise ValueError('Original must be inside the workspace')
        body = path.read_text(encoding='utf-8')
        evidence = decision.get('reading_evidence', {})
        if (not body.strip() or evidence.get('sha256') != digest(body) or evidence.get('sha256') != candidate['content_hash']
                or evidence.get('char_count') != len(body) or evidence.get('read_chars', 0) < len(body)
                or evidence.get('reader') != 'agent' or not evidence.get('read_at')):
            raise ValueError('Summary requires matching full-original Agent reading evidence')
        included.append({**candidate, **{k: decision[k] for k in ('overview', 'what_changed', 'usefulness', 'summary', 'reading_evidence')},
                         'read_status': 'full_text_read', 'published_at': candidate.get('published_at')})
    return included, excluded, pending


def retry_sources(run_path):
    """Retry incomplete sources in the same run without losing other candidates."""
    with locked():
        report = load(run_path, {})
        if report.get('finished_at'):
            raise ValueError('Cannot rerun collection after the final email; start a fresh run')
        state = load(DATA / 'state.json', {})
        failed = {s['source_id'] for s in report['source_status'] if s['status'] != 'success'}
        definitions = {s['id']: (f['name'], s) for f in manifest()['frameworks'] for s in f['sources']}
        candidates = {i['id']: i for i in report['candidates']}
        for coverage in report['source_status']:
            if coverage['source_id'] not in failed:
                continue
            framework, source = definitions[coverage['source_id']]
            try:
                rows, checkpoint, failures = discover(source, state['sources'].get(source['id'], {}), report['window_start'], report['window_end'])
                state['sources'][source['id']] = {**checkpoint, 'checked_at': report['window_end']}
                coverage.update({'status': 'partial' if failures else 'success', 'errors': failures, 'candidate_count': len(rows)})
                if checkpoint.get('initial_undated_baseline'):
                    coverage['baseline_note'] = f"{len(checkpoint['initial_undated_baseline'])}篇既有文章未标注日期，仅建立基线，不作为今日更新。"
                for row in rows:
                    body = row.pop('fulltext')
                    identity = digest(row['url'])[:24]
                    path = DATA / 'fulltext' / (identity + '-' + digest(body)[:16] + '.txt')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(body, encoding='utf-8')
                    row.pop('embedded_body', None)
                    row.update({'id': identity, 'framework': framework, 'source': framework + ' · ' + source['name'],
                                'source_id': source['id'], 'source_ids': [source['id']], 'content_hash': digest(body),
                                'fulltext_path': str(path.relative_to(ROOT)), 'char_count': len(body)})
                    candidates[identity] = row
            except Exception as exc:
                coverage.update({'status': 'failed', 'errors': [core.safe_error(exc)]})
        report['candidates'] = sorted(candidates.values(), key=lambda i: (i.get('event_at') or '', i['id']), reverse=True)
        report['errors'] = [s['framework'] + ' / ' + s['name'] + '：' + (error if isinstance(error, str) else error['url'] + ' ' + error['error'])
                            for s in report['source_status'] for error in s['errors']]
        write_json(DATA / 'state.json', state)
        write_json(DATA / 'pending.json', report['candidates'])
        write_json(run_path, report)
        return {'run_path': str(run_path), 'retried_source_count': len(failed), 'candidate_count': len(candidates), 'error_count': len(report['errors'])}


def grouped_blocks(items):
    groups = defaultdict(list)
    for item in items:
        groups[item['framework']].append(item)
    blocks = []
    for framework, values in sorted(groups.items(), key=lambda pair: max(i['event_at'] for i in pair[1]), reverse=True):
        blocks.append(heading(2, framework))
        for item in sorted(values, key=lambda i: (i['event_at'], i['id']), reverse=True):
            display = {**item, 'title': re.sub(r'\s*\(20\d{2}-\d{2}-\d{2}\)\s*$', '', item['title'])}
            blocks.extend(report_blocks(display))
    return blocks


def sync(report, included, archive):
    token = core.tenant_token()
    wiki = ensure_wiki(token)
    known = children(token, wiki['space_id'], wiki['directory']['node_token'])
    daily = ensure_node(token, wiki['space_id'], wiki['directory']['node_token'], report['day'] + ' Agent Framework 技术更新', known)
    # Re-running today's issue preserves successful earlier updates from today.
    today = [i for i in archive['items'] if i.get('collection_day') == report['day']]
    blocks = grouped_blocks(today) or [paragraph('本轮未发现可确认的新增技术更新。')]
    if report['errors']:
        blocks.insert(0, paragraph('部分来源或处理步骤存在异常，本轮覆盖尚不完整；详情已记录在采集邮件中。'))
    write_blocks(token, daily['obj_token'], blocks, replace=True, bulk=True)
    root_blocks = [paragraph('按来源汇总Agent框架技术更新。点击来源标签查看原文，展开正文查看技术变化与用途。'),
                   {'block_type': 2, 'text': core.text_data([core.text_run('🔗 Agent Framework Source', link=wiki['source_url'])])}]
    root_blocks += grouped_blocks(sorted(archive['items'], key=lambda i: (i['event_at'], i['id']), reverse=True)[:40])
    if not archive['items']:
        root_blocks.append(paragraph('今日监测已启用，暂无可确认的新增技术更新。'))
    write_blocks(token, wiki['directory']['obj_token'], root_blocks, replace=True, bulk=True)
    # Read back native block hierarchy and hyperlink destinations after publication.
    stored = core.list_doc_blocks(token, daily['obj_token'])
    previews = [b for b in stored if b.get('block_type') == 2 and b.get('text', {}).get('style', {}).get('folded')]
    if len(previews) != len(today) or any(not b.get('children') for b in previews):
        raise RuntimeError('Feishu folded summaries failed read-back verification')
    daily_url = WIKI_PREFIX + daily['node_token']
    write_json(DATA / 'verification' / (report['run_id'] + '.json'), {'at': now(), 'daily_url': daily_url,
                'preview_count': len(previews), 'default_folded': True, 'wiki': wiki, 'daily_blocks': stored})
    for item in included:
        item['feishu_url'] = daily_url
    return {'directory_url': wiki['directory_url'], 'source_url': wiki['source_url'], 'daily_url': daily_url,
            'published_count': len(today), 'verified': True}


def finish(run_path, decisions_path=None, send_email=True):
    with locked():
        run_path = Path(run_path)
        report = load(run_path, {})
        if not report.get('run_id'):
            raise ValueError('Missing current run')
        if report.get('finished_at'):
            # A mail failure is retried with the SAME run id; never rerun business.
            if send_email:
                report['email_result'] = task_email.send_report(report, task_email.config())
                write_json(run_path, report)
            return {'run_id': report['run_id'], 'status': report['status'], 'email': report.get('email_result'), 'already_finished': True}
        included, excluded, pending = [], [], []
        try:
            decisions = load(decisions_path, []) if decisions_path else []
            included, excluded, pending = validate_decisions(report.get('candidates', []), decisions)
        except Exception as exc:
            pending = report.get('candidates', [])
            report['errors'].append('Agent总结校验失败：' + core.safe_error(exc))
        archive = load(DATA / 'archive.json', {'version': 1, 'items': []})
        by_id = {i['id']: i for i in archive['items']}
        changed = []
        for item in included:
            old = by_id.get(item['id'])
            if not old or old['content_hash'] != item['content_hash']:
                changed.append(item)
            item['collection_day'] = report['day']
            item['collected_at'] = report['collected_at']
            by_id[item['id']] = item
        archive['items'] = sorted(by_id.values(), key=lambda i: (i['event_at'], i['id']), reverse=True)
        archive['updated_at'] = now()
        write_json(DATA / 'archive.json', archive)
        write_json(DATA / 'pending.json', pending)
        report['excluded'] = excluded
        report['pending'] = pending
        for item in pending:
            report['errors'].append(item['framework'] + ' / ' + item['title'] + '：' + item.get('reason', '待完整阅读与总结'))
        try:
            result = sync(report, included, archive)
            report['publication'] = result
            report['feishu'] = '已写入并读回验证：' + result['daily_url']
            write_json(DATA / 'archive.json', archive)
        except Exception as exc:
            report['errors'].append('飞书发布失败：' + core.safe_error(exc))
            report['feishu'] = '发布失败，本地总结已保存；下一轮将重新同步。'
        report['framework_updates'] = {name: [i for i in changed if i['framework'] == name] for name in sorted({i['framework'] for i in changed})}
        succeeded = sum(s['status'] == 'success' for s in report['source_status'])
        failed = sum(s['status'] != 'success' for s in report['source_status'])
        report['counts'] = {'framework_source_count': len(report['source_status']), 'framework_source_success_count': succeeded,
                            'framework_update_count': len(changed), 'framework_changed_count': len(report['framework_updates']),
                            'framework_pending_count': len(pending), 'framework_excluded_count': len(excluded)}
        report['status'] = ('failed' if not succeeded or not report.get('publication') else 'partial') if report['errors'] else 'success'
        report['summary'] = f"本轮检查{len(report['source_status'])}个来源，{len(report['framework_updates'])}个框架共{len(changed)}项技术更新；{len(pending)}项待处理，{failed}个来源覆盖异常。"
        report['finished_at'] = now()
        write_json(DATA / 'daily' / (report['day'] + '.json'), {'date': report['day'], 'run_id': report['run_id'],
                    'items': [i for i in archive['items'] if i.get('collection_day') == report['day']],
                    'source_status': report['source_status'], 'errors': report['errors']})
        write_json(run_path, report)
        state = load(DATA / 'state.json', {})
        if state.get('active_run') == report['run_id']:
            state.pop('active_run')
        state['last_finished_at'] = report['finished_at']
        write_json(DATA / 'state.json', state)
        if send_email:
            try:
                report['email_result'] = task_email.send_report(report, task_email.config())
            except Exception as exc:
                report['email_result'] = {'ok': False, 'error_type': type(exc).__name__}
                write_json(run_path, report)
                raise RuntimeError('Email delivery failed; retry finish with the same run id') from None
        write_json(run_path, report)
        return {'run_id': report['run_id'], 'status': report['status'], 'counts': report['counts'],
                'publication': report.get('publication'), 'email': report.get('email_result'), 'run_path': str(run_path)}
