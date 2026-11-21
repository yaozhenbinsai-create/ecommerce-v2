"""Platform-neutral support inbox, regional FAQ decisions and durable human handoff.

Remote IO is always outside SQLite write transactions. A persisted sending receipt
is a one-way boundary: interrupted sends may only be reconciled, never retried.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from fastapi import APIRouter, HTTPException, Request
from . import store as s, locales, environment, support_ai, support_channels
from . import support_archive, support_cases

router = APIRouter(prefix='/support')
router.include_router(support_archive.router)
router.include_router(support_cases.router)
IDENTITY = ('target_id', 'platform', 'account_id', 'seller_id', 'marketplace_id', 'environment')


def _text(value, name, maximum=1000, optional=False):
    if optional and value in (None, ''):
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise HTTPException(422, name + '格式无效')
    return value.strip()


def _revision(payload):
    value = payload.get('revision')
    if type(value) is not int or value < 0:
        raise HTTPException(422, '请提供当前资料版本')
    return value


def _target(c, ident, active=True):
    target = s.get(c, 'target', ident)
    account = c.execute('SELECT id,platform,account_name,seller_id,enabled FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
    if not account:
        raise HTTPException(409, '客服所属账号不存在')
    if active:
        environment.require(target['environment'])
        if not account['enabled'] or not target.get('enabled'):
            raise HTTPException(409, '客服所属账号或地区已停用')
        if account['seller_id'] != target.get('seller_id'):
            raise HTTPException(409, '店铺身份已变化，请重新核实账号地区')
    return dict(target, platform=account['platform'], account_name=account['account_name'])


def _identity(target):
    return {key: target.get('id') if key == 'target_id' else target.get(key) for key in IDENTITY}


def _assert_identity(thread, target):
    if any(thread.get(key) != value for key, value in _identity(target).items()):
        raise HTTPException(409, '会话所属平台、账号或地区身份已变化，请人工核实')


def _key(*values):
    return hashlib.sha256(s.dump(values).encode('utf-8')).hexdigest()


def _policy(c, target):
    saved = s.by_key(c, 'support_policy', str(target['id']))
    if saved:
        return saved
    try:
        language = locales.resolve(target)
    except HTTPException:
        language = ''
    return {'target_id': target['id'], 'revision': 0, 'auto_reply': False, 'auto_sync': False, 'plan_id': None,
            'language': language, 'identity': _identity(target), 'knowledge': []}


def _policy_current(policy, target):
    return policy.get('identity') == _identity(target) and policy.get('language') == locales.resolve(target)


def _caps(target):
    try:
        result = support_channels.resolve(target).capabilities(target)
        if not isinstance(result, dict):
            raise ValueError('能力格式无效')
        from .data_egress import amazon_context
        return {k: result.get(k) is True for k in ('receive', 'send', 'reconcile')} | {
            'reason': str(result.get('reason', '')), 'platform': target['platform'],
            'analysis': not amazon_context({'identity': _identity(target)})}
    except Exception:
        return {'receive': False, 'send': False, 'reconcile': False,
                'reason': '客服渠道未配置或店铺身份待核实', 'platform': target.get('platform', '')}


def _messages(c, ident):
    return [s.decode(row) for row in c.execute("SELECT * FROM commerce_records WHERE kind='support_message' AND json_extract(data,'$.thread_id')=? ORDER BY id", (ident,))]


def _pending(c, ident):
    return [m for m in _messages(c, ident) if m.get('direction') == 'outbound' and m.get('delivery_status') in {'sending', 'unknown'}]


def _human(c, thread, reason):
    thread.update(status='human', human_owned=True, last_reason=reason,
                  ownership_version=thread.get('ownership_version', 0) + 1)
    thread = s.save(c, 'support_thread', thread)
    key = f"support:{thread['id']}:{thread['inbound_version']}"
    s.put(c, 'notice', key, {'title': '客服需人工处理：' + reason[:180], 'object_id': thread['id'],
                            'object_kind': 'support_thread', 'support_thread_id': thread['id'],
                            'target_id': thread['target_id'], 'read': False})
    s.audit(c, 'support_handoff', thread['id'], reason)
    return thread


def detail(ident):
    with s.main().db() as c:
        thread = s.get(c, 'support_thread', ident)
        target = _target(c, thread['target_id'], active=False)
        messages = _messages(c, ident)
        policy = _policy(c, target)
        case_contacts = support_cases.contacts(c, thread) if thread.get('source_type') == 'amazon_support_api' else []
        case_connection = support_cases.public(target, c) if thread.get('source_type') == 'amazon_support_api' else None
    capabilities = _caps(target) if case_connection is None else {
        'receive': case_connection['status'] in {'connected', 'syncing'}, 'send': False, 'reconcile': False, 'analysis': False,
        'source_type': 'amazon_support_api', 'source_status': case_connection['status'],
        'reason': '官方 Case API 读取卖家账号的支持工单与沟通记录；平台回复和 Case 操作请到亚马逊办理。'}
    return {'thread': thread, 'messages': messages, 'case_contacts': case_contacts, 'capabilities': capabilities, 'policy': policy,
            'source_url': support_archive.original_url(target, thread)}


@router.get('/threads')
def list_threads():
    with s.main().db() as c:
        threads = s.all_rows(c, 'support_thread')
        targets = []
        for row in s.all_rows(c, 'target'):
            try:
                target = _target(c, row['id'], active=False)
            except HTTPException:
                continue
            entry = {k: target.get(k) for k in ('id', 'name', 'account_name', 'account_id', 'platform', 'marketplace_id', 'environment', 'enabled')}
            try:
                entry['language'] = locales.resolve(target)
            except HTTPException:
                entry['language'] = ''
            entry['policy'] = _policy(c, target)
            entry['capabilities'] = _caps(target)
            entry['case_connection'] = support_cases.public(target, c)
            if target['platform'] in {'亚马逊', 'Amazon', 'amazon'}:
                entry['buyer_api'] = {'status': 'unsupported', 'receive': False,
                                      'reason': '公开Messaging API没有完整买家收件箱读取接口'}
            targets.append(entry)
    stats = {status: sum(t['status'] == status for t in threads) for status in ('human', 'ai_pending', 'tracking', 'awaiting_customer', 'closed')}
    return {'threads': threads, 'targets': targets, 'stats': {'total': len(threads), **stats}}


@router.get('/threads/{ident}')
def get_thread(ident: int):
    return detail(ident)


@router.get('/targets/{ident}/policy')
def get_policy(ident: int):
    with s.main().db() as c:
        return _policy(c, _target(c, ident, active=False))


def save_policy(ident, payload):
    revision = _revision(payload)
    auto_reply = payload.get('auto_reply', False)
    auto_sync = payload.get('auto_sync', False)
    if type(auto_reply) is not bool or type(auto_sync) is not bool:
        raise HTTPException(422, '自动回复和自动同步选项必须为布尔值')
    plan_id = payload.get('plan_id') or None
    if plan_id is not None and (type(plan_id) is not int or plan_id <= 0):
        raise HTTPException(422, '客服方案无效')
    knowledge = payload.get('knowledge', [])
    if not isinstance(knowledge, list) or len(knowledge) > 200:
        raise HTTPException(422, '每个地区最多保存200条客服知识')
    with s.transaction() as c:
        target = _target(c, ident)
        old = _policy(c, target)
        if old['revision'] != revision:
            raise HTTPException(409, '客服策略已更新，请刷新后再保存')
        language = locales.resolve(target)
        caps = _caps(target)
        if auto_reply and not caps['send']:
            raise HTTPException(422, '当前渠道未接入实际回复能力，不能启用自动回复')
        if auto_sync and not caps['receive']:
            raise HTTPException(422, '当前渠道未接入收件能力，不能启用自动同步')
        clean, seen = [], set()
        for row in knowledge:
            if not isinstance(row, dict):
                raise HTTPException(422, '客服知识格式无效')
            kid = _text(row.get('id'), '知识编号', 100)
            if kid in seen:
                raise HTTPException(422, '客服知识编号重复')
            seen.add(kid)
            enabled = row.get('enabled', True)
            if type(enabled) is not bool:
                raise HTTPException(422, '知识启用选项必须为布尔值')
            lang = locales.normalize(row.get('language') or language)
            answer = _text(row.get('answer'), '知识答案', 6000)
            if enabled and not support_ai.language_matches(answer, lang):
                raise HTTPException(422, '已启用的知识答案需使用所标注的客户语言')
            clean.append({'id': kid, 'question': _text(row.get('question'), '知识问题', 1000),
                          'answer': answer, 'enabled': enabled, 'language': lang})
        if plan_id:
            plan = c.execute('SELECT model_ids FROM plans WHERE id=? AND enabled=1', (plan_id,)).fetchone()
            if not plan:
                raise HTTPException(422, '客服方案不存在或已停用')
            import json
            model_ids = json.loads(plan['model_ids'])
            if not any(r['id'] in model_ids for r in c.execute("SELECT id FROM models WHERE capability='文本' AND enabled=1")):
                raise HTTPException(422, '客服方案需要启用的文本模型')
        result = s.put(c, 'support_policy', str(ident), {'target_id': ident, 'auto_reply': auto_reply, 'auto_sync': auto_sync,
                       'plan_id': plan_id, 'knowledge': clean, 'identity': _identity(target), 'language': language},
                       expected=revision if revision else None)
        s.audit(c, 'support_policy_saved', ident, f"自动回复={auto_reply}；知识条目={len(clean)}")
        return result


@router.put('/targets/{ident}/policy')
def put_policy(ident: int, payload: dict, request: Request):
    if getattr(request.state, 'commerce_role', None) != 'admin':
        raise HTTPException(403, '客服策略和知识配置需要管理员账号')
    return save_policy(ident, payload)


def _enqueue_ai(c, thread, manual=False):
    key = f"support-ai:{thread['id']}:{thread['inbound_version']}"
    if manual:
        key += ':' + uuid.uuid4().hex
    return s.enqueue(c, 'support_ai', key, {'thread_id': thread['id'], 'target_id': thread['target_id'],
                      'inbound_version': thread['inbound_version'], 'manual': manual})


def _ingest(c, target, payload, trusted=False):
    external_thread = _text(payload.get('external_thread_id'), '平台会话编号', 300)
    external_message = _text(payload.get('external_message_id'), '平台消息编号', 300)
    text = _text(payload.get('text'), '客户消息', 12000)
    subject = _text(payload.get('subject'), '主题', 500, optional=True)
    external_order = _text(payload.get('external_order_id'), '平台订单编号', 300, optional=True)
    preferred = payload.get('buyer_locale') if trusted else payload.get('preferred_language')
    try:
        region_language = locales.resolve(target)
    except HTTPException:
        region_language = ''
    unsupported_locale = ''
    if preferred:
        preferred = _text(preferred, '客户首选语言', 60)
        try:
            language = locales.normalize(preferred)
        except HTTPException:
            if not trusted:
                raise
            # A valid channel message must not disappear (or block its entire
            # page) merely because this app cannot yet answer in its language.
            language, unsupported_locale = '', preferred
    else:
        language = region_language
    language_source = 'buyer_preference' if preferred else 'target_region'
    identity = _identity(target)
    key = _key(identity, external_message)
    direction = payload.get('direction', 'inbound') if trusted else 'inbound'
    if direction not in {'inbound', 'outbound'}:
        raise HTTPException(422, '原始对话方向无法确认')
    fingerprint = _key(external_thread, external_message, text, subject, external_order, preferred or '')
    if direction == 'outbound':
        fingerprint = _key(fingerprint, 'observed_outbound')
    existing = s.by_key(c, 'support_message', key)
    if existing:
        if existing.get('fingerprint') != fingerprint:
            raise HTTPException(409, '同一平台消息编号内容冲突，已拒绝覆盖')
        return s.get(c, 'support_thread', existing['thread_id'])
    thread_key = _key(identity, external_thread)
    thread = s.by_key(c, 'support_thread', thread_key)
    created = not thread
    was_tracking = thread and thread.get('status') == 'tracking'
    if not thread:
        thread_type = payload.get('thread_type', 'buyer') if trusted else 'buyer'
        if thread_type not in {'buyer', 'seller_case', 'source_issue'}:
            raise HTTPException(422, '消息来源类型无效')
        thread = s.put(c, 'support_thread', thread_key, {**identity, 'account_name': target['account_name'],
                       'external_thread_id': external_thread, 'external_order_id': external_order,
                       'order_binding_verified': bool(trusted and external_order), 'subject': subject or '客户咨询',
                       'status': 'tracking' if direction == 'outbound' else 'ai_pending', 'human_owned': False, 'assignee': '', 'last_reason': '',
                       'inbound_version': 0, 'last_message': '', 'language': language, 'language_source': language_source,
                       'region_language': region_language, 'ownership_version': 0, 'ai_draft': None,
                       'unsupported_buyer_locale': unsupported_locale,
                       'thread_type': thread_type, 'platform_case_id': payload.get('platform_case_id', '') if trusted else ''})
    elif external_order and thread.get('external_order_id') not in ('', external_order):
        raise HTTPException(409, '会话关联订单不一致，已拒绝合并')
    if direction == 'outbound':
        # Platform seller history is not a send by this system and never clears
        # human ownership, an uncertain delivery, or the owner's local conclusion.
        thread = s.save(c, 'support_thread', {**thread, 'last_message': text})
        s.put(c, 'support_message', key, {'thread_id': thread['id'], 'target_id': target['id'],
              'direction': 'outbound', 'external_message_id': external_message, 'text': text,
              'source': 'channel', 'delivery_status': 'observed', 'fingerprint': fingerprint,
              'received_at': _text(payload.get('received_at'), '平台记录时间', 100, optional=True),
              'language': thread['language'], 'provenance': payload.get('provenance')})
        return thread
    if preferred or thread.get('language_source') != 'buyer_preference':
        thread.update(language=language, language_source=language_source, unsupported_buyer_locale=unsupported_locale)
    if external_order and not thread.get('external_order_id'):
        thread.update(external_order_id=external_order, order_binding_verified=bool(trusted))
    thread.update(inbound_version=thread['inbound_version'] + 1, last_message=text,
                  ai_draft=None, region_language=region_language)
    thread['status'] = 'human' if thread.get('human_owned') else 'ai_pending'
    thread = s.save(c, 'support_thread', thread)
    provenance = payload.get('provenance') if trusted and isinstance(payload.get('provenance'), dict) else None
    s.put(c, 'support_message', key, {'thread_id': thread['id'], 'target_id': target['id'], 'direction': 'inbound',
          'external_message_id': external_message, 'text': text, 'source': 'channel' if trusted else 'manual',
          'delivery_status': 'received', 'fingerprint': fingerprint,
          'received_at': _text(payload.get('received_at'), '接收时间', 100, optional=True),
          'language': thread['language'], 'provenance': provenance})
    if provenance and provenance.get('channel') == 'amazon_browser':
        if thread.get('thread_type') == 'seller_case' and (created or was_tracking) and not thread.get('human_owned'):
            thread = s.save(c, 'support_thread', {**thread, 'status': 'tracking',
                            'last_reason': '已同步亚马逊原Case对话；平台状态单独展示，本地结论由人工登记'})
        else:
            thread = _human(c, thread, '已同步亚马逊原始对话，等待人工处理；未发送客户内容到外部AI')
    elif thread.get('unsupported_buyer_locale'):
        thread = _human(c, thread, '客户首选语言暂不支持自动回复，请人工处理：' + thread['unsupported_buyer_locale'])
    elif not region_language:
        thread = _human(c, thread, '店铺地区语言尚未配置，请人工处理并补齐地区语言')
    elif _pending(c, thread['id']):
        thread = _human(c, thread, '收到新消息，前次发送结果仍需核实')
    elif thread.get('human_owned'):
        thread = _human(c, thread, '人工接管会话收到新消息')
    else:
        from .data_egress import amazon_context
        if amazon_context({'identity': identity}):
            thread = _human(c, thread, '亚马逊客户数据未授权发送给外部AI服务，已转人工处理')
        else:
            _enqueue_ai(c, thread)
    return thread


def ingest_message(payload, trusted=False, expected_identity=None):
    target_id = payload.get('target_id')
    if type(target_id) is not int or target_id < 1:
        raise HTTPException(422, '请选择消息所属店铺地区')
    with s.transaction() as c:
        target = _target(c, target_id)
        if expected_identity is not None and expected_identity != _identity(target):
            raise HTTPException(409, '同步期间店铺身份变化，消息未导入')
        thread = _ingest(c, target, payload, trusted=trusted)
    return detail(thread['id'])


@router.post('/messages')
def receive_message(payload: dict):
    # Browser entry is an authenticated operator import, not a public platform webhook.
    return ingest_message(payload)


@router.post('/threads/{ident}/ai', status_code=202)
def request_ai(ident: int):
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        target = _target(c, thread['target_id'])
        _assert_identity(thread, target)
        from .data_egress import amazon_context
        if amazon_context({'identity': _identity(target)}):
            raise HTTPException(409, '此来源的客户数据未授权发送给外部AI，请在人工工单处理')
        if thread['status'] == 'closed' or _pending(c, ident):
            raise HTTPException(409, '会话已关闭或发送结果待核实')
        active = c.execute("SELECT id FROM commerce_jobs WHERE kind='support_ai' AND json_extract(payload,'$.thread_id')=? AND status IN ('queued','running','retry_wait')", (ident,)).fetchone()
        job = s.job(c, active[0]) if active else _enqueue_ai(c, thread, manual=True)
    return {'job': job, 'thread': thread}


def _context(c, thread, target, policy):
    knowledge = [x for x in policy.get('knowledge', []) if x.get('enabled') and x.get('language', policy.get('language')) == thread['language']]
    order = None
    if thread.get('order_binding_verified') and thread.get('external_order_id'):
        matches = [o for o in s.all_rows(c, 'order') if o.get('target_id') == target['id']
                   and o.get('account_id') == target['account_id'] and o.get('marketplace_id') == target['marketplace_id']
                   and o.get('environment') == target['environment'] and o.get('platform') == target['platform']
                   and o.get('external_id') == thread['external_order_id'] and not o.get('sync_issues')
                   and type(o.get('last_sync_at')) in {int, float} and 0 <= time.time() - o['last_sync_at'] <= 3600]
        if len(matches) == 1:
            row = matches[0]
            order = {'external_order_id': row['external_id'], 'platform_status': row.get('platform_status'), 'verified_at': row['last_sync_at']}
    history = [{'direction': x['direction'], 'text': x['text']} for x in _messages(c, thread['id'])
               if x['direction'] == 'inbound' or x.get('delivery_status') == 'sent'][-12:]
    return {'identity': _identity(target), 'language': thread['language'], 'language_source': thread['language_source'],
            'latest_message': thread['last_message'], 'history': history, 'knowledge': knowledge, 'verified_order': order}


def process_ai(job):
    payload = job['payload']
    ident = payload['thread_id']
    with s.main().db() as c:
        thread = s.get(c, 'support_thread', ident)
        if thread['inbound_version'] != payload['inbound_version'] or thread['status'] == 'closed':
            return {'thread_id': ident, 'status': 'stale'}
        if thread.get('human_owned') and not payload.get('manual'):
            return {'thread_id': ident, 'status': 'human'}
        revision = thread['revision']
        pending = _pending(c, ident)
    if pending:
        with s.transaction() as c:
            _human(c, s.get(c, 'support_thread', ident), '前次发送结果尚未核实，自动回复已停止，禁止重发')
        return {'thread_id': ident, 'status': 'human'}
    try:
        with s.main().db() as c:
            target = _target(c, thread['target_id'])
            _assert_identity(thread, target)
            policy = _policy(c, target)
            if not _policy_current(policy, target) or thread.get('region_language') != locales.resolve(target):
                raise ValueError('店铺地区或知识策略已变更，请重新确认')
            context = _context(c, thread, target, policy)
        from .data_egress import local_support_decision
        decision = local_support_decision(context)
        if decision is None:
            decision = support_ai.decide(context, policy)
        # Validate even injected/custom decision providers at the trust boundary.
        decision = support_ai.validate(s.dump(decision), context)
        error = ''
    except Exception:
        decision = None
        error = 'AI客服未能生成有依据的回复，请人工处理或检查地区知识和模型配置'
    with s.transaction() as c:
        current = s.get(c, 'support_thread', ident)
        if current['revision'] != revision:
            return {'thread_id': ident, 'status': 'stale'}
        if error:
            current = _human(c, current, error)
            return {'thread_id': ident, 'status': current['status']}
        try:
            latest_target = _target(c, current['target_id'])
            _assert_identity(current, latest_target)
            latest_policy = _policy(c, latest_target)
            if latest_policy['revision'] != policy['revision'] or not _policy_current(latest_policy, latest_target) or locales.resolve(latest_target) != current['region_language']:
                raise ValueError('客服策略已更新')
        except Exception:
            current = _human(c, current, 'AI处理期间地区身份或客服策略已变化，请重新确认')
            return {'thread_id': ident, 'status': current['status']}
        current['ai_draft'] = decision
        current = s.save(c, 'support_thread', current)
        if decision['action'] == 'escalate':
            current = _human(c, current, decision['reason'])
        elif current.get('human_owned') or not policy.get('auto_reply'):
            current = _human(c, current, 'AI建议已准备，等待人工审核回复')
        else:
            return_send = (current['revision'], decision['reply'], current['inbound_version'], policy['revision'])
            current = None
    if current is not None:
        return {'thread_id': ident, 'status': current['status']}
    revision, text, version, policy_revision = return_send
    try:
        result = deliver(ident, {'revision': revision, 'text': text, 'request_id': f'ai-{ident}-{version}'},
                         automatic=True, policy_revision=policy_revision)
    except HTTPException:
        with s.transaction() as c:
            latest = s.get(c, 'support_thread', ident)
            if latest['inbound_version'] == version and latest['status'] != 'closed':
                _human(c, latest, '发信前会话或店铺配置已变化，回复已停止，请人工核实')
        return {'thread_id': ident, 'status': 'stale'}
    return {'thread_id': ident, 'status': result['thread']['status']}


def _finish_send(ident, request_id, receipt, error='', unknown=False):
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        message = s.by_key(c, 'support_message', _key('outbound', ident, request_id))
        if message.get('delivery_status') == 'sent':
            return
        valid = isinstance(receipt, dict) and receipt.get('status') == 'sent' and isinstance(receipt.get('external_message_id'), str) and receipt['external_message_id'].strip()
        if valid:
            message.update(delivery_status='sent', external_message_id=receipt['external_message_id'], error='')
            s.save(c, 'support_message', message)
            if (thread['inbound_version'] == message['inbound_version']
                    and thread.get('ownership_version', 0) == message.get('ownership_version', 0)):
                thread.update(status='awaiting_customer', last_reason='人工已回复，等待客户消息' if message['source'] == 'human' else '')
                s.save(c, 'support_thread', thread)
            s.audit(c, 'support_message_sent', ident, request_id)
        else:
            message.update(delivery_status='unknown' if unknown else 'failed', error=error)
            s.save(c, 'support_message', message)
            _human(c, thread, error or '客服消息发送失败，请人工处理')


def deliver(ident, payload, automatic=False, policy_revision=None):
    revision = _revision(payload)
    text = _text(payload.get('text'), '客服回复', 6000)
    request_id = _text(payload.get('request_id'), '回复请求编号', 160)
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        key = _key('outbound', ident, request_id)
        old = s.by_key(c, 'support_message', key)
        if old:
            if old.get('text') != text or old.get('source') != ('ai' if automatic else 'human'):
                raise HTTPException(409, '回复请求编号已用于其他内容')
            if old.get('delivery_status') in {'sending', 'unknown'}:
                _human(c, thread, '该次发送结果待核实，禁止重复发送')
            existing = True
            error = ''
        else:
            existing = False
            if thread['revision'] != revision:
                raise HTTPException(409, '会话已有新消息或已被其他人员处理，请刷新')
            if thread['status'] == 'closed':
                raise HTTPException(409, '已关闭的会话不能回复')
            if _pending(c, ident):
                raise HTTPException(409, '前次发送结果待核实，禁止再次发送')
            target = _target(c, thread['target_id'])
            _assert_identity(thread, target)
            if thread.get('region_language') != locales.resolve(target):
                raise HTTPException(409, '店铺地区语言已变化，请重新确认会话')
            policy = _policy(c, target)
            if automatic and (thread.get('human_owned') or not policy.get('auto_reply') or policy['revision'] != policy_revision or not _policy_current(policy, target)):
                _human(c, thread, '客服策略已变化或会话已被人工接管，自动回复已停止')
                error = '自动回复已停止'
            else:
                try:
                    adapter = support_channels.resolve(target)
                    capabilities = adapter.capabilities(target)
                    if not isinstance(capabilities, dict):
                        raise ValueError('渠道能力无效')
                except Exception:
                    adapter = None
                    capabilities = {'send': False, 'reason': '客服渠道配置或店铺身份待核实'}
                settings = s.by_key(c, 'settings', 'global') or {}
                error = ('正式写入尚未启用，客服消息未发送' if settings.get('live_writes_enabled') is not True else
                         (capabilities.get('reason') or '当前渠道不支持客服回复') if capabilities.get('send') is not True else '')
                message = {'thread_id': ident, 'target_id': thread['target_id'], 'direction': 'outbound', 'text': text,
                           'source': 'ai' if automatic else 'human', 'request_id': request_id,
                           'external_message_id': '', 'delivery_status': 'failed' if error else 'sending',
                           'inbound_version': thread['inbound_version'], 'identity': _identity(target),
                           'ownership_version': thread.get('ownership_version', 0),
                           'language': thread['language'], 'error': error}
                s.put(c, 'support_message', key, message)
                if error:
                    _human(c, thread, error)
                elif not automatic:
                    thread.update(human_owned=True)
                    s.save(c, 'support_thread', thread)
                s.audit(c, 'support_send_checkpoint', ident, request_id)
    if existing or error:
        return detail(ident)
    try:
        # Revalidate the persisted account snapshot at the IO boundary, while
        # retaining the already-bound adapter so identity changes cannot reroute.
        support_channels.resolve(target)
        receipt = adapter.send(target, thread, text, request_id)
        if not isinstance(receipt, dict) or receipt.get('status') != 'sent' or not isinstance(receipt.get('external_message_id'), str) or not receipt['external_message_id'].strip():
            _finish_send(ident, request_id, None, '渠道未返回可核实的发送回执，请人工核对，禁止重发', unknown=True)
        else:
            _finish_send(ident, request_id, receipt)
    except support_channels.ChannelError as exc:
        _finish_send(ident, request_id, None,
                     '发送结果未知，请在渠道核实，禁止重发' if exc.unknown else '渠道发送失败，请人工处理', unknown=exc.unknown)
    except Exception:
        # An unexpected transport exception may occur after the remote mutation.
        _finish_send(ident, request_id, None, '发送中断，结果未知，请核实原请求，禁止重发', unknown=True)
    return detail(ident)


@router.post('/threads/{ident}/reply')
def reply_thread(ident: int, payload: dict):
    return deliver(ident, payload)


@router.post('/threads/{ident}/claim')
def claim_thread(ident: int, payload: dict):
    revision = _revision(payload)
    assignee = _text(payload.get('assignee'), '处理人', 100)
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        if thread['revision'] != revision or thread['status'] == 'closed':
            raise HTTPException(409, '会话已更新或关闭，请刷新')
        thread['assignee'] = assignee
        _human(c, thread, '人工已领取会话')
    return detail(ident)


@router.post('/threads/{ident}/escalate')
def escalate_thread(ident: int, payload: dict):
    revision = _revision(payload)
    reason = _text(payload.get('reason') or '客服手动转人工处理', '转人工理由', 1000)
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        if thread['revision'] != revision or thread['status'] == 'closed':
            raise HTTPException(409, '会话已更新或关闭，请刷新')
        _human(c, thread, reason)
    return detail(ident)


@router.post('/threads/{ident}/close')
def close_thread(ident: int, payload: dict):
    revision = _revision(payload)
    resolution = _text(payload.get('resolution'), '处理结论', 2000)
    with s.transaction() as c:
        thread = s.get(c, 'support_thread', ident)
        if thread['revision'] != revision:
            raise HTTPException(409, '会话已更新，请刷新')
        if _pending(c, ident):
            raise HTTPException(409, '发送结果尚未核实，不能关闭会话')
        thread.update(status='closed', human_owned=True, resolution=resolution, last_reason=resolution)
        s.save(c, 'support_thread', thread)
        s.audit(c, 'support_closed', ident, resolution)
    return detail(ident)


@router.post('/threads/{ident}/reconcile')
def reconcile_thread(ident: int):
    with s.main().db() as c:
        thread = s.get(c, 'support_thread', ident)
        target = _target(c, thread['target_id'])
        _assert_identity(thread, target)
        pending = _pending(c, ident)
    if not pending:
        return detail(ident)
    capabilities = _caps(target)
    if not capabilities['reconcile']:
        with s.transaction() as c:
            _human(c, s.get(c, 'support_thread', ident), '当前渠道不能核实原发送回执，请在平台联系人工支持；禁止重发')
        return detail(ident)
    for message in pending:
        if message['identity'] != _identity(target):
            raise HTTPException(409, '原发送请求身份不一致，不能核实')
        try:
            receipt = support_channels.resolve(target).reconcile(target, thread, message['request_id'])
        except Exception:
            receipt = None
        if isinstance(receipt, dict) and receipt.get('status') == 'sent' and isinstance(receipt.get('external_message_id'), str) and receipt['external_message_id'].strip():
            _finish_send(ident, message['request_id'], receipt)
        else:
            _finish_send(ident, message['request_id'], None, '渠道尚未确认原请求已发送，继续人工核实，禁止重发', unknown=True)
    return detail(ident)


@router.post('/sync', status_code=202)
def request_sync(payload: dict):
    ids = payload.get('target_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i < 1 for i in ids):
        raise HTTPException(422, '请明确选择要同步的账号地区')
    jobs = []
    with s.transaction() as c:
        for ident in dict.fromkeys(ids):
            target = _target(c, ident)
            if not _caps(target)['receive']:
                raise HTTPException(422, _caps(target).get('reason') or '所选店铺尚未接通收件API')
            state_key = _key('cursor', _identity(target))
            sync_state = s.by_key(c, 'support_sync', state_key) or {}
            if sync_state.get('needs_rescan'):
                raise HTTPException(409, '当前同步位置需要核实，请检查连接后重试；原记录保留')
            active = c.execute("SELECT id FROM commerce_jobs WHERE kind='support_sync' AND json_extract(payload,'$.target_id')=? AND status IN ('queued','running','retry_wait')", (ident,)).fetchone()
            jobs.append(s.job(c, active[0]) if active else s.enqueue(c, 'support_sync', 'support-sync:' + uuid.uuid4().hex,
                        {'target_id': ident, 'identity': _identity(target)}))
    return {'jobs': jobs}


@router.get('/sync-jobs/{ident}')
def sync_job(ident: int):
    with s.main().db() as c:
        job = s.job(c, ident)
        if job['kind'] != 'support_sync':
            raise HTTPException(404, '收件任务不存在')
        target = _target(c, job['payload']['target_id'])
        if job['payload'].get('identity') != _identity(target):
            raise HTTPException(409, '收件任务所属店铺身份已变化')
    return {key: job[key] for key in ('id', 'status', 'error', 'result')}


def sync_messages(job):
    ident = job['payload']['target_id']
    if job['payload'].get('source_generation'):
        return {'target_id': ident, 'status': 'source_retired', 'received': 0,
                'reason': '网页收件来源已停用；旧任务未读取来源，原记录和位置保留'}
    try:
        with s.main().db() as c:
            target = _target(c, ident)
            identity = _identity(target)
            if job['payload'].get('identity') != identity:
                raise ValueError('客服同步目标身份已变化')
            state_key = _key('cursor', identity)
            state = s.by_key(c, 'support_sync', state_key) or {'revision': 0, 'cursor': None}
            if job['payload'].get('automatic'):
                policy = _policy(c, target)
                if not policy.get('auto_sync') or not _policy_current(policy, target):
                    return {'target_id': ident, 'status': 'paused', 'reason': '自动收件已关闭，未读取来源'}
        adapter = support_channels.resolve(target)
        capabilities = adapter.capabilities(target)
        if not capabilities.get('receive'):
            raise support_channels.ChannelError(capabilities.get('reason') or '收件API当前不可用；原记录保留')
        result = adapter.fetch(target, state.get('cursor'))
        if not isinstance(result, dict) or not isinstance(result.get('messages'), list) or len(result['messages']) > 1000:
            raise ValueError('渠道消息响应无效')
        if result.get('source_type') == 'amazon_browser' or result.get('source_generation'):
            raise support_channels.ChannelError('网页收件来源已停用，旧来源响应未写入；原记录和位置保留')
        with s.transaction() as c:
            current = _target(c, ident)
            if _identity(current) != identity:
                raise ValueError('同步过程中店铺身份已变化')
            latest = s.by_key(c, 'support_sync', state_key) or {'revision': 0}
            if latest['revision'] != state['revision']:
                return {'target_id': ident, 'status': 'stale', 'received': 0}
            received = 0
            for message in result['messages']:
                if not isinstance(message, dict):
                    raise ValueError('渠道消息格式无效')
                if not s.by_key(c, 'support_message', _key(identity, message.get('external_message_id'))):
                    received += 1
                _ingest(c, current, message, trusted=True)
            observations = result.get('observations', [])
            if not isinstance(observations, list) or len(observations) > 50:
                raise ValueError('来源状态格式无效')
            for observed in observations:
                thread = s.by_key(c, 'support_thread', _key(identity, observed.get('external_thread_id')))
                if thread:
                    s.save(c, 'support_thread', {**thread, 'platform_observed': {key: observed.get(key) for key in
                           ('status', 'checked_at', 'source_url', 'history_complete')}})
            s.put(c, 'support_sync', state_key, {'target_id': ident, 'identity': identity,
                  'cursor': result.get('cursor'), 'last_sync_at': time.time(), 'last_error': '',
                  'last_attempt_at': time.time(), 'received': received,
                  'has_more': result.get('has_more') is True, 'retry_at': 0, 'coverage': result.get('coverage', {})})
        return {'target_id': ident, 'status': 'completed', 'received': received, 'has_more': result.get('has_more') is True}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, support_channels.ChannelError) else str(exc.detail) if isinstance(exc, HTTPException) else '客服消息同步未完成：请检查API授权、来源身份或消息编号冲突；原消息与同步位置已保留'
        with s.transaction() as c:
            if 'state_key' in locals():
                previous = s.by_key(c, 'support_sync', state_key) or {}
                s.put(c, 'support_sync', state_key, {**previous, 'target_id': ident, 'last_error': reason,
                      'last_attempt_at': time.time(), 'retry_at': time.time() + 300,
                      'needs_rescan': previous.get('needs_rescan', False)})
            s.put(c, 'notice', f'support-sync:{ident}', {'title': reason, 'object_id': ident,
                  'object_kind': 'support_target', 'target_id': ident, 'read': False})
            for thread in s.all_rows(c, 'support_thread'):
                if thread['target_id'] == ident and thread['status'] == 'ai_pending':
                    _human(c, thread, reason)
        return {'target_id': ident, 'status': 'human', 'reason': reason}


HANDLERS = {'support_ai': process_ai, 'support_sync': sync_messages, support_cases.JOB: support_cases.run}


def schedule(c):
    """Called by the existing minute scheduler; adapters without inbox support stay idle."""
    queued = support_cases.schedule(c)
    now = time.time()
    for policy in s.all_rows(c, 'support_policy'):
        if not policy.get('auto_sync'):
            continue
        try:
            target = _target(c, policy['target_id'])
            if not _policy_current(policy, target) or not _caps(target)['receive']:
                continue
            active = c.execute("SELECT id FROM commerce_jobs WHERE kind='support_sync' AND json_extract(payload,'$.target_id')=? AND status IN ('queued','running','retry_wait')", (target['id'],)).fetchone()
            if active:
                continue
            identity = _identity(target)
            state_key = _key('cursor', identity)
            state = s.by_key(c, 'support_sync', state_key) or {}
            if state.get('retry_at', 0) > now or state.get('needs_rescan'):
                continue
            if now - state.get('last_sync_at', 0) < 60:
                continue
            queued.append(s.enqueue(c, 'support_sync', f"support-auto-sync:{target['id']}:{int(now // 60)}",
                          {'target_id': target['id'], 'identity': identity, 'automatic': True}))
        except HTTPException:
            continue
    return queued
