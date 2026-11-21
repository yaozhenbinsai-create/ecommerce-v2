"""Durable, read-only Seller Support API intake, independent of buyer login.

Each job performs one official read, then commits its data and next position
atomically. Native case/contact identities are account scoped. Local ownership
and conclusions are never inferred from Amazon's case status.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from app import security_secrets
from . import store as s, support_case_api as api

router = APIRouter()
JOB = 'support_cases'
AMAZON = {'亚马逊', 'Amazon', 'amazon'}
ACTIVE = ('queued', 'running', 'retry_wait')


def identity(target):
    return {key: target[key] for key in ('account_id', 'seller_id', 'environment')}


def _key(*values):
    return hashlib.sha256(s.dump(values).encode()).hexdigest()


def _target(c, ident, active=True):
    from .support import _target as read
    target = read(c, ident, active=active)
    if target['platform'] not in AMAZON or target['environment'] != 'production':
        raise HTTPException(422, 'Case API 仅连接已配置的正式亚马逊账号')
    return target


def _admin(request):
    if getattr(request.state, 'commerce_role', None) != 'admin':
        raise HTTPException(403, 'Case API 连接设置需要管理员账号')


def config(target, c):
    saved = s.by_key(c, 'support_case_source', _key(identity(target))) or {}
    return saved if saved.get('identity') == identity(target) else {}


def _state(c, cfg):
    return s.by_key(c, 'support_case_sync', cfg.get('generation', '')) or {'revision': 0}


def _active(c, cfg, exclude=None):
    row = c.execute("SELECT id FROM commerce_jobs WHERE kind=? AND json_extract(payload,'$.generation')=? "
                    "AND json_extract(payload,'$.source_revision')=? AND status IN ('queued','running','retry_wait') AND id!=? ORDER BY id LIMIT 1",
                    (JOB, cfg.get('generation', ''), cfg.get('revision', 0), exclude or -1)).fetchone()
    return s.job(c, row[0]) if row else None


def public(target, c=None):
    if c is None:
        with s.main().db() as conn:
            return public(target, conn)
    if target.get('platform') not in AMAZON or target.get('environment') != 'production':
        return {'status': 'unsupported', 'enabled': False, 'revision': 0, 'scope': 'seller_account'}
    cfg = config(target, c)
    state = _state(c, cfg)
    binding_changed = False
    if cfg:
        from .support import _identity
        try:
            bound = _target(c, cfg['target_id'])
            binding_changed = cfg.get('binding') != _identity(bound)
        except HTTPException:
            binding_changed = True
    status = ('paused' if not cfg.get('enabled') else state.get('status', 'unverified')) if cfg else 'unconfigured'
    if binding_changed:
        status = 'identity_changed'
    return {'status': status,
            'enabled': cfg.get('enabled', False), 'auto_sync': cfg.get('auto_sync', False),
            'revision': cfg.get('revision', 0), 'scope': 'seller_account',
            'managed_by_target_id': cfg.get('target_id', target['id']),
            'busy': _active(c, cfg) is not None and not binding_changed,
            'phase': '连接所属账号地区已变化，原记录保留；请核实原连接' if binding_changed else state.get('phase', ''),
            'last_error': state.get('last_error', ''), 'last_checked_at': state.get('last_checked_at'),
            'completed_at': state.get('completed_at'), 'has_more': bool(state.get('cursor')),
            'case_count': state.get('case_count', 0), 'contact_count': state.get('contact_count', 0),
            'http_status': state.get('http_status', 0), 'request_id': state.get('request_id', ''),
            'next_retry_at': state.get('retry_at', 0),
            'required_role': api.ROLE, 'send': False}


def _enqueue(c, target, cfg, *, exclude=None, manual=False):
    active = _active(c, cfg, exclude)
    if active:
        return active
    state = _state(c, cfg)
    return s.enqueue(c, JOB, 'support-case-api:' + uuid.uuid4().hex,
                     {'target_id': target['id'], 'identity': identity(target), 'generation': cfg['generation'],
                      'source_revision': cfg['revision'], 'state_revision': state['revision'], 'manual': manual})


@router.get('/targets/{ident}/case-api')
def read_connection(ident: int):
    with s.main().db() as c:
        return public(_target(c, ident, active=False), c)


@router.post('/targets/{ident}/case-api/sync', status_code=202)
def request_sync(ident: int, payload: dict, request: Request):
    _admin(request)
    with s.transaction() as c:
        target = _target(c, ident)
        cfg = config(target, c)
        if cfg and cfg['target_id'] != ident:
            raise HTTPException(409, '此卖家账号的 Case 已由其他地区连接管理，请打开该连接；不会重复建立地区副本')
        if cfg:
            from .support import _identity
            if cfg.get('binding') != _identity(target):
                raise HTTPException(409, 'Case 连接所属账号地区已变化，请核实原连接；未借用旧授权')
        if type(payload.get('revision')) is not int or payload['revision'] != cfg.get('revision', 0):
            raise HTTPException(409, 'Case API 连接已变化，请刷新后重试')
        active = _active(c, cfg)
        if active:
            return {'job': active, 'connection': public(target, c)}
        if not cfg.get('enabled'):
            from .support import _identity
            cfg = s.put(c, 'support_case_source', _key(identity(target)),
                        {'identity': identity(target), 'binding': _identity(target), 'target_id': ident,
                         'enabled': True, 'auto_sync': True, 'generation': cfg.get('generation') or uuid.uuid4().hex})
        state = _state(c, cfg)
        s.put(c, 'support_case_sync', cfg['generation'], {**state, 'read_failures': 0, 'retry_at': 0,
              'status': 'queued', 'phase': '已排队核验Case API权限并读取原记录', 'last_error': '',
              'previous_error': state.get('last_error') or state.get('previous_error', '')})
        job = _enqueue(c, target, cfg, manual=True)
        s.audit(c, 'support_case_api_read_requested', ident, '官方只读 Case API；账号范围；未申请或修改远端权限')
        return {'job': job, 'connection': public(target, c)}


@router.put('/targets/{ident}/case-api')
def change_connection(ident: int, payload: dict, request: Request):
    _admin(request)
    if type(payload.get('enabled')) is not bool or type(payload.get('auto_sync')) is not bool:
        raise HTTPException(422, 'Case 同步开关必须为布尔值')
    with s.transaction() as c:
        target = _target(c, ident, active=False)
        cfg = config(target, c)
        if not cfg or cfg['target_id'] != ident or type(payload.get('revision')) is not int or payload['revision'] != cfg['revision']:
            raise HTTPException(409, 'Case API 连接已变化，请重新打开原连接')
        s.save(c, 'support_case_source', {**cfg, 'enabled': payload['enabled'], 'auto_sync': payload['auto_sync']}, expected=cfg['revision'])
        state = _state(c, cfg)
        if state['revision'] and (state.get('cursor') or state.get('status') == 'retry_wait'):
            s.save(c, 'support_case_sync', {**state, 'status': 'paused', 'retry_at': 0,
                   'phase': '读取设置已变化；原位置保留，未完成部分等待下一次同步'})
        s.audit(c, 'support_case_api_settings_changed', ident, '仅修改本系统读取；历史数据保留')
        return public(target, c)


@router.post('/targets/{ident}/case-api/rescan', status_code=202)
def rescan(ident: int, payload: dict, request: Request):
    _admin(request)
    from .support import _identity
    with s.transaction() as c:
        target = _target(c, ident)
        cfg = config(target, c)
        if (not cfg.get('enabled') or cfg.get('target_id') != ident or cfg.get('binding') != _identity(target)
                or type(payload.get('revision')) is not int or payload['revision'] != cfg['revision']):
            raise HTTPException(409, 'Case 连接已暂停或变化，请重新核实当前连接')
        state = _state(c, cfg)
        if state['revision']:
            s.put(c, 'support_case_scan_checkpoint', cfg['generation'] + ':' + str(state['revision']),
                  {**state, 'original_revision': state['revision'], 'generation': cfg['generation']})
        # A new source revision revokes queued and in-flight reads. Native data,
        # originals and the previous position remain recoverable.
        cfg = s.save(c, 'support_case_source', cfg, expected=cfg['revision'])
        s.put(c, 'support_case_sync', cfg['generation'], {**state, 'cursor': None, 'status': 'unverified',
              'last_error': '', 'read_failures': 0, 'retry_at': 0, 'phase': '已保存原读取位置，正在从首页重新核实Case'})
        job = _enqueue(c, target, cfg, manual=True)
        s.audit(c, 'support_case_api_rescan', ident, '保存旧位置后重新只读核对；未删除工单或原始回执')
        return {'job': job, 'connection': public(target, c)}


def _current(c, job):
    from .support import _identity
    payload = job['payload']
    target = _target(c, payload['target_id'])
    cfg = config(target, c)
    if (not cfg.get('enabled') or cfg.get('target_id') != target['id'] or cfg.get('binding') != _identity(target)
            or identity(target) != payload.get('identity') or cfg.get('generation') != payload.get('generation')
            or cfg.get('revision') != payload.get('source_revision')
            or not payload.get('manual') and not cfg.get('auto_sync')):
        raise HTTPException(409, 'Case API 连接已暂停或身份变化；旧读取不写入')
    state = _state(c, cfg)
    if state['revision'] != payload.get('state_revision'):
        raise HTTPException(409, 'Case API 同步位置已推进；旧读取不重复写入')
    return target, cfg, state


def _archive(source_identity, case_id, contact_id, raw):
    envelope = {'identity': source_identity, 'case_id': case_id, 'contact_id': contact_id, 'raw': raw}
    digest = _key(envelope)
    folder = s.main().DATA_ROOT / 'support_case_receipts'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + '.enc')
    if path.is_symlink():
        raise api.CaseApiError('Case 原始回执路径异常，未覆盖')
    if path.exists():
        if _key(security_secrets.load_json(path)) != digest:
            raise api.CaseApiError('Case 原始回执校验失败，原数据保留')
    else:
        security_secrets.save_json(path, envelope)
    return digest


def _semantic(raw):
    if isinstance(raw, dict):
        return {k: _semantic(v) for k, v in raw.items() if k != 'downloadUrl'}
    if isinstance(raw, list):
        return [_semantic(v) for v in raw]
    return raw


def _epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def project_contact(raw):
    """Display native communications without inventing direction or body text."""
    channel = raw['channel']
    sections, attachments = [], []
    content = raw.get({'EMAIL': 'emailContent', 'PHONE': 'phoneContent', 'CHAT': 'chatContent'}[channel])
    if content is not None and not isinstance(content, dict):
        raise api.CaseApiError('Case 沟通正文格式无效')
    content = content or {}
    def section(label, body):
        if body is not None and not isinstance(body, str):
            raise api.CaseApiError('Case 沟通正文不是文本')
        if body:
            sections.append({'label': label, 'text': body})
    def participant(value):
        if not isinstance(value, dict):
            return '发送方未提供'
        role = {'AMAZON': '亚马逊支持', 'SELLING_PARTNER': '卖家'}.get(value.get('role'), '发送方待核对')
        return role + (' · ' + value['displayName'] if isinstance(value.get('displayName'), str) else '')
    if channel == 'EMAIL':
        section(participant(content.get('sender')), content.get('message'))
        attachments = content.get('attachments', [])
    elif channel == 'PHONE':
        section('电话沟通纪要', content.get('callNotes'))
    else:
        events = content.get('events', [])
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise api.CaseApiError('Case 聊天事件格式无效')
        for event in events:
            section(participant(event.get('participant')) + ' · ' + str(event.get('timestamp', '')) +
                    ' · ' + str(event.get('type', '')), event.get('message'))
        if not sections:
            section('聊天记录原文（双方沟通）', content.get('transcript'))
    if not isinstance(attachments, list) or any(not isinstance(x, dict) for x in attachments):
        raise api.CaseApiError('Case 附件元数据格式无效')
    return {'sections': sections, 'attachments': [x.get('name', '未命名附件') for x in attachments],
            'availability': 'expired' if raw.get('status') == 'EXPIRED' else 'available' if sections else 'unavailable',
            'channel': channel, 'contact_id': raw['contactId'], 'received_at': raw['createdDate']}


def _case_thread(c, target, raw, *, full=False):
    from .support import _identity, _assert_identity
    case_id = raw['caseId']
    key = _key('amazon_support_api', identity(target), case_id)
    thread = s.by_key(c, 'support_thread', key)
    if not thread:
        # Adopt only a uniquely matching pre-existing browser Case, retaining
        # every local conclusion and original receipt. Ambiguity is not a merge.
        matches = [t for t in s.all_rows(c, 'support_thread') if t.get('thread_type') == 'seller_case'
                   and t.get('platform_case_id') == case_id and all(t.get(k) == v for k, v in identity(target).items())]
        if len(matches) > 1 or matches and matches[0].get('target_id') != target['id']:
            raise api.CaseApiError('原 Case 存在多个或其他地区的本地归属，请人工核对；原记录未合并')
        thread = matches[0] if matches else None
    if thread:
        _assert_identity(thread, target)
    else:
        thread = s.put(c, 'support_thread', key, {**_identity(target), 'account_name': target['account_name'],
                       'external_thread_id': 'case:' + case_id, 'platform_case_id': case_id, 'thread_type': 'seller_case',
                       'external_order_id': '', 'order_binding_verified': False, 'status': 'tracking',
                       'human_owned': False, 'assignee': '', 'last_reason': '', 'last_message': '',
                       'inbound_version': 0, 'ownership_version': 0, 'ai_draft': None,
                       'language': '', 'region_language': '', 'language_source': 'not_provided'})
    old = thread.get('platform_observed', {})
    complete = old.get('history_complete', False) if old.get('last_updated_date') == raw['lastUpdatedDate'] else False
    values = {**thread, 'subject': raw['subject'], 'source_type': 'amazon_support_api', 'source_scope': 'seller_account',
              'case_api_identity': identity(target),
              'platform_observed': {'status': raw['status'], 'checked_at': time.time(), 'history_complete': complete,
                                    'last_updated_date': raw['lastUpdatedDate'], 'created_date': raw['createdDate']}}
    if full:
        values['case_receipt_sha256'] = _archive(identity(target), case_id, '', raw)
    return s.save(c, 'support_thread', values)


def _contact(c, target, thread, raw):
    key = _key(identity(target), thread['platform_case_id'], raw['contactId'])
    old = s.by_key(c, 'support_case_contact', key)
    if old and (old['channel'] != raw['channel'] or old['received_at'] != raw['createdDate']):
        raise api.CaseApiError('原沟通编号的类型或时间发生冲突，原记录保留')
    fingerprint = _key(_semantic(raw))
    if old and old['fingerprint'] == fingerprint:
        return False
    view = project_contact(raw)
    digest = _archive(identity(target), thread['platform_case_id'], raw['contactId'], raw)
    history = list(old.get('receipt_history', [])) if old else []
    if old and old['receipt_sha256'] not in history:
        history.append(old['receipt_sha256'])
    preview = '\n\n'.join(x['label'] + '\n' + x['text'] for x in view['sections'])
    s.put(c, 'support_case_contact', key, {'thread_id': thread['id'], 'identity': identity(target),
          'case_id': thread['platform_case_id'], 'contact_id': raw['contactId'], 'channel': raw['channel'],
          'received_at': raw['createdDate'], 'availability': view['availability'], 'attachments': view['attachments'],
          'text': preview[:12000], 'body_truncated': len(preview) > 12000, 'fingerprint': fingerprint,
          'receipt_sha256': digest, 'receipt_history': history})
    # Native contact time, rather than retrieval order, governs the excerpt.
    current = s.get(c, 'support_thread', thread['id'])
    if preview and (not current.get('last_case_contact_at') or _epoch(raw['createdDate']) >= _epoch(current['last_case_contact_at'])):
        s.save(c, 'support_thread', {**current,
                                   'last_message': preview[:400], 'last_case_contact_at': raw['createdDate']})
    return old is None


def contacts(c, thread):
    rows = [row for row in s.all_rows(c, 'support_case_contact') if row.get('thread_id') == thread['id']]
    return sorted(rows, key=lambda row: (_epoch(row['received_at']), row['contact_id']))


@router.get('/case-contacts/{ident}/original')
def original_contact(ident: int, request: Request):
    if getattr(request.state, 'commerce_role', None) not in {'admin', 'operator'}:
        raise HTTPException(403, '读取原始对话需要客服账号')
    from .support import _assert_identity
    with s.main().db() as c:
        contact = s.get(c, 'support_case_contact', ident)
        thread = s.get(c, 'support_thread', contact['thread_id'])
        target = _target(c, thread['target_id'], active=False)
        _assert_identity(thread, target)
    digest = contact.get('receipt_sha256', '')
    path = s.main().DATA_ROOT / 'support_case_receipts' / (digest + '.enc')
    if not re.fullmatch(r'[a-f0-9]{64}', digest) or path.is_symlink() or not path.is_file():
        raise HTTPException(409, 'Case 原始回执缺失，请在原平台核对；本地记录保留')
    try:
        envelope = security_secrets.load_json(path)
        if (_key(envelope) != digest or envelope.get('identity') != identity(target)
                or envelope.get('case_id') != thread['platform_case_id'] or envelope.get('contact_id') != contact['contact_id']):
            raise ValueError()
        return {**project_contact(envelope['raw']), 'receipt_sha256': digest}
    except (OSError, ValueError, security_secrets.SecretStorageError, api.CaseApiError):
        raise HTTPException(409, 'Case 原始回执未通过身份或完整性校验') from None


def _seen(cursor, token, key):
    seen = cursor.get(key, [])
    if token is None:
        return []
    digest = _key(token)
    if digest in seen or len(seen) >= 2000:
        raise api.CaseApiError('Case API 分页重复或超出有界范围，请人工核对；原同步位置保留')
    return [*seen, digest]


def run(job):
    try:
        with s.main().db() as c:
            target, cfg, state = _current(c, job)
        cursor = state.get('cursor') or {'phase': 'list', 'token': None, 'pending': []}
        phase = cursor['phase']
        if phase == 'list':
            response = api.list_cases(target, next_token=cursor.get('token'))
        elif phase == 'detail':
            response = api.get_case(target, cursor['pending'][0])
        elif phase == 'contacts':
            response = api.list_contacts(target, cursor['pending'][0], next_token=cursor.get('contact_token'), max_results=50)
        else:
            raise api.CaseApiError('Case API 同步阶段无效；原位置保留')
        with s.transaction() as c:
            target, cfg, state = _current(c, job)
            added_contacts = 0
            if phase == 'list':
                seen = _seen(cursor, response['next_token'], 'seen_lists')
                pending = []
                for raw in response['cases']:
                    _case_thread(c, target, raw)
                    if raw['caseId'] not in pending:
                        pending.append(raw['caseId'])
                cursor = {'phase': 'detail' if pending else 'list', 'pending': pending,
                          'token': response['next_token'], 'list_more': response['has_more'], 'seen_lists': seen}
            else:
                case_id = cursor['pending'][0]
                if phase == 'detail':
                    _case_thread(c, target, response['case'], full=True)
                    cursor = {**cursor, 'phase': 'contacts', 'contact_token': None, 'seen_contacts': []}
                else:
                    seen = _seen(cursor, response['next_token'], 'seen_contacts')
                    matches = [t for t in s.all_rows(c, 'support_thread') if t.get('case_api_identity') == identity(target)
                               and t.get('platform_case_id') == case_id]
                    if len(matches) != 1:
                        raise api.CaseApiError('Case 原始归属无法唯一确认，原记录保留')
                    thread = matches[0]
                    for raw in response['contacts']:
                        added_contacts += _contact(c, target, thread, raw)
                    if response['has_more']:
                        cursor = {**cursor, 'contact_token': response['next_token'], 'seen_contacts': seen}
                    else:
                        current = s.get(c, 'support_thread', thread['id'])
                        s.save(c, 'support_thread', {**current, 'platform_observed':
                               {**current['platform_observed'], 'history_complete': True, 'history_checked_at': time.time()}})
                        remaining = cursor['pending'][1:]
                        cursor = {**cursor, 'pending': remaining, 'phase': 'detail' if remaining else 'list',
                                  'contact_token': None, 'seen_contacts': []}
            more = bool(cursor.get('pending') or cursor.get('list_more'))
            phase_text = ('正在读取 Case ' + cursor['pending'][0] + (' 的沟通记录' if cursor['phase'] == 'contacts' else ' 的详情')) if cursor.get('pending') else '正在读取下一页 Case'
            s.put(c, 'support_case_sync', cfg['generation'], {**state, 'cursor': cursor if more else None,
                  'status': 'syncing' if more else 'connected', 'phase': phase_text if more else 'Case 列表、详情和沟通分页读取完成',
                  'last_checked_at': time.time(), 'completed_at': state.get('completed_at') if more else time.time(),
                  'last_error': '', 'http_status': 200, 'request_id': response['request_id'], 'retry_at': 0, 'read_failures': 0,
                  'case_count': sum(t.get('case_api_identity') == identity(target) for t in s.all_rows(c, 'support_thread')),
                  'contact_count': sum(t.get('identity') == identity(target) for t in s.all_rows(c, 'support_case_contact'))})
            if more:
                _enqueue(c, target, cfg, exclude=job['id'], manual=job['payload'].get('manual', False))
            return {'status': 'progress' if more else 'completed', 'received': added_contacts, 'has_more': more}
    except HTTPException as exc:
        # Revocation/identity/cursor races must not change current source state.
        return {'status': 'stale', 'reason': str(exc.detail)}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, api.CaseApiError) else 'Case API 读取或原始回执保存未完成；原记录与同步位置保留'
        with s.transaction() as c:
            try:
                target, cfg, state = _current(c, job)
            except HTTPException:
                return {'status': 'stale', 'reason': '连接已变化，旧失败未覆盖当前状态'}
            denied = getattr(exc, 'status', 0) == 403
            failures = state.get('read_failures', 0) + 1
            transient = getattr(exc, 'status', 0) in {408, 429} or 500 <= getattr(exc, 'status', 0) <= 599
            retry = transient and failures < 5
            delay = max(getattr(exc, 'retry_after', 0), min(300, 5 * 2 ** failures)) if retry else 0
            status = 'retry_wait' if retry else 'permission_required' if denied else 'needs_attention'
            s.put(c, 'support_case_sync', cfg['generation'], {**state,
                  'status': status, 'last_error': reason,
                  'phase': '临时读取失败，按平台冷却时间有界重试' if retry else '需申请销售伙伴支持权限并更新应用授权' if denied else '读取已停止，请核实后重试',
                  'http_status': getattr(exc, 'status', 0), 'request_id': getattr(exc, 'request_id', ''),
                  'last_attempt_at': time.time(), 'retry_at': time.time() + delay if retry else 0, 'read_failures': failures})
            if retry:
                queued = _enqueue(c, target, cfg, exclude=job['id'], manual=job['payload'].get('manual', False))
                c.execute('UPDATE commerce_jobs SET next_at=? WHERE id=?', (time.time() + delay, queued['id']))
        return {'status': status, 'reason': reason}


def schedule(c):
    queued = []
    for cfg in s.all_rows(c, 'support_case_source'):
        if not cfg.get('enabled') or not cfg.get('auto_sync'):
            continue
        try:
            target = _target(c, cfg['target_id'])
            from .support import _identity
            if cfg.get('identity') != identity(target) or cfg.get('binding') != _identity(target):
                continue
            state = _state(c, cfg)
            if state.get('status') in {'permission_required', 'needs_attention'} or _active(c, cfg):
                continue
            if state.get('cursor') or time.time() - state.get('completed_at', 0) >= 300:
                queued.append(_enqueue(c, target, cfg))
        except HTTPException:
            continue
    return queued
