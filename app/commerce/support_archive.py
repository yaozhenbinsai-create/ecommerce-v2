"""Read-only access to historical Amazon conversation archives; no intake executor."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app import security_secrets
from . import store as s

router = APIRouter()
MARKETS = {
    'ATVPDKIKX0DER': ('sellercentral.amazon.com', ('美国', 'United States')),
    'A2EUQ1WTGCTBG2': ('sellercentral.amazon.ca', ('加拿大', 'Canada')),
    'A1AM78C64UM0Y8': ('sellercentral.amazon.com.mx', ('墨西哥', 'Mexico')),
    'A2Q3Y263D00KWC': ('sellercentral.amazon.com.br', ('巴西', 'Brazil')),
    'A1F83G8C2ARO7P': ('sellercentral.amazon.co.uk', ('英国', 'United Kingdom')),
    'A1PA6795UKMFR9': ('sellercentral.amazon.de', ('德国', 'Germany')),
    'A13V1IB3VIYZZH': ('sellercentral.amazon.fr', ('法国', 'France')),
    'APJ6JRA9NG5V4': ('sellercentral.amazon.it', ('意大利', 'Italy')),
    'A1RKKUPIHCS9HS': ('sellercentral.amazon.es', ('西班牙', 'Spain')),
    'A1VC38T7YXB528': ('sellercentral.amazon.co.jp', ('日本', 'Japan')),
    'A39IBJ37TRP1C6': ('sellercentral.amazon.com.au', ('澳大利亚', 'Australia')),
    'A19VAU5U5O7RUS': ('sellercentral.amazon.sg', ('新加坡', 'Singapore')),
}


def identity(target):
    from .support import _identity
    return _identity(target)


def portal(target):
    return 'https://' + MARKETS[target['marketplace_id']][0]


@router.get('/messages/{ident}/original')
def original_message(ident: int, request: Request):
    if getattr(request.state, 'commerce_role', None) not in {'admin', 'operator'}:
        raise HTTPException(403, '读取原始对话需要客服账号')
    from .support import _target as read_target, _assert_identity
    with s.main().db() as c:
        message = s.get(c, 'support_message', ident)
        thread = s.get(c, 'support_thread', message['thread_id'])
        target = read_target(c, thread['target_id'], active=False)
        _assert_identity(thread, target)
    provenance = message.get('provenance') or {}
    digest = provenance.get('receipt_sha256', '')
    if provenance.get('channel') != 'amazon_browser' or len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
        raise HTTPException(404, '此记录没有直接后台来源归档')
    path = s.main().DATA_ROOT / 'support_browser_receipts' / (digest + '.enc')
    if path.is_symlink() or not path.is_file():
        raise HTTPException(409, '来源归档缺失，请核实原平台；本地记录保留')
    try:
        envelope = security_secrets.load_json(path)
    except security_secrets.SecretStorageError:
        raise HTTPException(503, '来源归档无法解密；原数据保留，请核实服务账户') from None
    if (envelope.get('identity') != identity(target) or envelope.get('message_id') != message['external_message_id']
            or hashlib.sha256(s.dump(envelope).encode()).hexdigest() != digest):
        raise HTTPException(409, '来源归档与消息身份不一致，未展示错误归属的正文')
    return {**envelope['raw'], 'receipt_sha256': digest, 'has_attachments': provenance.get('has_attachments', False)}


def original_url(target, thread):
    if (target.get('platform') not in {'亚马逊', 'Amazon', 'amazon'} or target.get('marketplace_id') not in MARKETS
            or any(thread.get(key) != value for key, value in identity(target).items())):
        return ''
    origin = portal(target)
    if thread.get('thread_type') == 'seller_case':
        case_id = thread.get('platform_case_id', '')
        if isinstance(case_id, str) and case_id and len(case_id) <= 300 and not any(ord(x) < 32 for x in case_id):
            return origin + '/cu/case-dashboard/view-case?caseID=' + quote(case_id, safe='')
        return ''
    conversation = thread.get('external_thread_id', '').removeprefix('buyer:')
    return origin + '/messaging/inbox-v3' + ('?cc=' + conversation if re.fullmatch(r'[A-Za-z0-9-]{8,120}', conversation) else '')
