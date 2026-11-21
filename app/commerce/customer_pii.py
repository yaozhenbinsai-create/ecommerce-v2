"""Short-lived encrypted recipient cache, never included in portable backups.

Only open merchant orders can populate this cache. Retention is deliberately
stricter than delivery+30 days: 30 days from FIRST collection, never extended by
resync, and immediate removal once the order is shipped/cancelled. Tombstones
prevent stale copies from becoming readable after deletion. This does not erase
historical external snapshots or establish host/disk protection.
"""
import hashlib
import math
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from app import security_audit, security_runtime, security_secrets
from . import store as s

LOCK = threading.RLock()
MAX_AGE = 30 * 86400
OPEN = {'ready', 'partially_shipped'}


def enabled():
    config = security_runtime.load_config()
    return (config.get('amazon_pii_enabled') is True and config.get('mfa_required') is True
            and config.get('shareholder_password_login_enabled') is not True and config.get('enabled') is True)


def require():
    if not enabled():
        raise HTTPException(422, '收件信息同步尚未启用：需先取得Direct to Consumer Shipping受限角色，并完成MFA、数据盘加密与保留清理验收')


def root():
    path = Path(s.main().DATA_ROOT) / 'customer_pii'
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise security_secrets.SecretStorageError('收件信息目录不能是链接')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def scope(target, order):
    return {'account_id':target['account_id'], 'marketplace_id':target['marketplace_id'],
            'seller_id':target['seller_id'], 'order_id':order['external_id']}


def key_for(identity):
    return hashlib.sha256(s.dump(identity).encode()).hexdigest()


def valid(value):
    return (isinstance(value, dict) and isinstance(value.get('scope'), dict)
            and isinstance(value.get('recipient'), dict)
            and all(type(value.get(k)) in (int, float) and math.isfinite(value[k]) for k in ('collected_at','expires_at'))
            and 0 <= value['expires_at'] - value['collected_at'] <= MAX_AGE)


def remove(key):
    # Write the marker first. Interrupted deletion still refuses all reads.
    folder = root()
    security_secrets.save_json(folder / (key + '.deleted'), {'deleted_at':time.time()})
    (folder / (key + '.enc')).unlink(missing_ok=True)


def protect(c, kind, row):
    if kind != 'order' or row.get('environment') != 'production' or not row.get('target_id'):
        return row
    target = s.get(c, 'target', row['target_id'])
    account = c.execute('SELECT platform FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
    if not account or account['platform'] != '亚马逊':
        return row
    recipient = row.get('recipient') or {}
    result = {**row, 'recipient':{}}
    identity = scope(target, row); key = key_for(identity)
    if row.get('pii_ref') and row['pii_ref'] != key:
        result.pop('pii_ref', None)
    with LOCK:
        folder = root(); path = folder / (key + '.enc')
        if row.get('platform_status', row.get('status')) not in OPEN:
            if path.exists() or row.get('pii_ref'):
                remove(key)
            result.update(pii_status='closed', pii_ref=key)
            return result
        if not recipient:
            return result
        require()
        if not isinstance(recipient, dict) or len(s.dump(recipient).encode()) > 32768:
            raise HTTPException(422, '平台收件信息格式或大小无效')
        if (folder / (key + '.deleted')).exists():
            result.update(pii_ref=key, pii_status='deleted')
            return result
        previous = security_secrets.load_json(path, valid) if path.exists() else {}
        now = time.time(); collected = previous.get('collected_at', now)
        expiry = min(previous.get('expires_at', collected + MAX_AGE), collected + MAX_AGE)
        if expiry <= now:
            remove(key); result.update(pii_ref=key, pii_status='expired')
            return result
        security_secrets.save_json(path, {'scope':identity, 'recipient':recipient, 'collected_at':collected, 'expires_at':expiry}, valid)
        result.update(pii_ref=key, pii_status='encrypted', pii_expires_at=expiry)
        return result


def view(c, order, role, actor=None):
    result = {**order, 'recipient':{}}
    if order.get('environment') != 'production':
        return order
    if role not in {'admin','operator'}:
        raise HTTPException(403, '当前职责不能查看收件信息')
    if not order.get('pii_ref') or not enabled():
        result['pii_message'] = '收件信息未启用或尚未同步；订单商品与数量可正常查看'
        return result
    target = s.get(c, 'target', order['target_id'])
    account = c.execute('SELECT seller_id,enabled FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
    if not account or not account['enabled'] or not target.get('enabled') or account['seller_id'] != target['seller_id']:
        raise HTTPException(403, '店铺已停用或身份变化，不能读取收件信息')
    identity = scope(target, order); key = key_for(identity)
    with LOCK:
        folder = root(); path = folder / (key + '.enc')
        if order['pii_ref'] != key or (folder / (key + '.deleted')).exists() or not path.exists():
            result['pii_message'] = '收件信息已清理或不在本机；如仍待发货，请核对同步状态或到亚马逊后台查看'
            return result
        data = security_secrets.load_json(path, valid)
        if not data or data['scope'] != identity:
            raise HTTPException(503, '收件信息无法验证')
        if data['expires_at'] <= time.time() or order.get('platform_status') not in OPEN:
            remove(key); result['pii_message'] = '收件信息已按保留规则清理'
            return result
        security_audit.emit('customer_pii.read', 'success', actor)
        result['recipient'] = data['recipient']
        result['pii_message'] = '仅用于线下发货。发货确认或取消后清理；最迟首次同步后30天清理。'
        return result


def cleanup(*, account_id=None, now=None):
    now = time.time() if now is None else now
    count = 0; failed = 0
    with LOCK:
        for path in root().glob('*.enc'):
            if path.is_symlink() or len(path.stem) != 64:
                raise security_secrets.SecretStorageError('收件信息文件路径异常')
            try:
                data = security_secrets.load_json(path, valid)
            except security_secrets.SecretStorageError:
                failed += 1
                continue
            revoked = account_id is not None and data['scope'].get('account_id') == account_id
            if revoked or data['expires_at'] <= now or path.with_suffix('.deleted').exists():
                remove(path.stem); count += 1
    if count:
        security_audit.emit('customer_pii.purge', 'success')
    if failed:
        security_audit.emit('customer_pii.purge', 'failed')
    return {'deleted_count':count, 'failed_count':failed}
