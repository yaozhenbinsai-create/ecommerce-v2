"""GTIN validation and merchant-provided Seller Central exemption evidence.

Amazon confirmed in case 22339225181 that SP-API has no GTIN exemption
application/status operation. A listing preview is never approval evidence.
"""
from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException

from . import store as s

PRODUCT_TYPE = re.compile(r'^[A-Z][A-Z0-9_]{0,99}$')
EVIDENCE_KIND = 'gtin_exemption_evidence'
EVIDENCE_STATUSES = {'needs_application', 'submitted', 'merchant_provided_approval',
                     'rejected', 'revoked'}
SCOPE_FIELDS = ('account_id', 'seller_id', 'marketplace_id', 'brand', 'product_type')
SUPPORT_CASE = '22339225181'


def valid_gtin(value: str) -> bool:
    """Validate UPC-A/EAN-13 syntax; this does not prove GS1 ownership."""
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{12}|[0-9]{13}', value):
        return False
    weighted = sum(int(digit) * (3 if offset % 2 == 0 else 1)
                   for offset, digit in enumerate(reversed(value[:-1])))
    return (10 - weighted % 10) % 10 == int(value[-1])


def scope(target: dict, brand: str, product_type: str) -> dict:
    return {'account_id': target.get('account_id'),
            'seller_id': str(target.get('seller_id') or '').strip(),
            'marketplace_id': str(target.get('marketplace_id') or '').strip(),
            'brand': str(brand or '').strip(), 'product_type': str(product_type or '').strip()}


def _valid_scope(value):
    return (type(value.get('account_id')) is int and value['account_id'] > 0
            and value.get('seller_id') and value.get('marketplace_id')
            and 0 < len(value.get('brand', '')) <= 200
            and PRODUCT_TYPE.fullmatch(value.get('product_type', '')))


def _same_scope(left, right):
    def value(row, key):
        raw = row.get(key)
        # Imports use "generic"; Seller Central's no-brand control uses
        # "Generic". This alias applies only to Amazon's unbranded name.
        if key == 'brand' and isinstance(raw, str) and raw.casefold() == 'generic':
            return 'Generic'
        return raw
    return all(value(left, key) == value(right, key) for key in SCOPE_FIELDS)


def _receipt(value, limit=2000, multiline=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and
            (not multiline or c not in '\n\r\t') for c in value):
        raise HTTPException(422, '批准回执引用格式无效')
    return value.strip()


def _has_approval_receipt(row):
    reference, attachments = row.get('reference'), row.get('attachments') or []
    if SUPPORT_CASE in s.dump([reference, attachments]):
        return False
    if isinstance(reference, str) and reference.strip():
        return True
    return isinstance(attachments, list) and any(isinstance(item, dict) and any(
        item.get(key) for key in ('path', 'url', 'reference', 'media_id')) for item in attachments)


def save_evidence(c, target, brand, product_type, payload, ident=None, actor=''):
    """Register external evidence and immutable revisions; never grant approval.

    Attachments refer to an uploaded receipt (path/media_id) or URL. This helper
    neither downloads a URL nor reads an arbitrary receipt path.
    """
    expected_scope = scope(target, brand, product_type)
    if not _valid_scope(expected_scope):
        raise HTTPException(422, '请先确定真实账号、卖家、站点、品牌和商品类型')
    if target.get('environment') != 'production':
        raise HTTPException(422, '真实 Seller Central 批准依据只能登记到正式店铺')
    if not isinstance(payload, dict):
        raise HTTPException(422, '豁免依据需要对象')
    old = s.get(c, EVIDENCE_KIND, ident) if ident is not None else None
    if old and not _same_scope(old.get('scope') or {}, expected_scope):
        raise HTTPException(409, '不能把原批准依据改到另一账号、卖家、站点、品牌或商品类型')
    if old and payload.get('revision') != old['revision']:
        raise HTTPException(409, '豁免依据已更新，请刷新')
    merged = {**(old or {}), **payload}
    status = merged.get('status', 'needs_application')
    if status not in EVIDENCE_STATUSES:
        raise HTTPException(422, '只能登记 Seller Central 办理状态或商家提供的批准依据')
    source = merged.get('source', 'seller_central')
    if source != 'seller_central':
        raise HTTPException(422, '批准依据必须来自 Seller Central 商品豁免结果，技术支持工单不是批准回执')
    reference = _receipt(merged.get('reference', ''))
    attachments = merged.get('attachments') or []
    if not isinstance(attachments, list) or len(attachments) > 20:
        raise HTTPException(422, '回执附件最多20项')
    clean_attachments = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise HTTPException(422, '回执附件引用格式无效')
        clean = {'label': _receipt(attachment.get('label', ''), 200)}
        for key in ('path', 'url', 'reference'):
            if attachment.get(key):
                clean[key] = _receipt(attachment[key])
        if 'url' in clean:
            parsed = urlparse(clean['url'])
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(422, '回执网址需要无凭据的 HTTPS 地址')
        if attachment.get('media_id'):
            if type(attachment['media_id']) is not int:
                raise HTTPException(422, '回执媒体ID无效')
            s.get(c, 'media', attachment['media_id'])
            clean['media_id'] = attachment['media_id']
        if len(clean) == 1:
            raise HTTPException(422, '请提供上传回执路径、媒体ID或回执网址')
        clean_attachments.append(clean)
    if SUPPORT_CASE in s.dump([reference, clean_attachments]):
        raise HTTPException(422, '接口技术支持工单不能作为商品 GTIN 豁免批准依据')
    approval_date = _receipt(merged.get('approval_date', ''), 10)
    approved_category = _receipt(merged.get('approved_category', ''), 500)
    if approval_date:
        try:
            date.fromisoformat(approval_date)
        except ValueError:
            raise HTTPException(422, '平台批准日期需要 YYYY-MM-DD') from None
    row = {'scope': expected_scope, 'target_id': target.get('id'), 'status': status,
           'source': source, 'source_description': '商家登记的 Seller Central 结果，未经 API 批准核验',
           'reference': reference, 'attachments': clean_attachments,
           'approval_date': approval_date, 'approved_category': approved_category,
           'notes': _receipt(merged.get('notes', ''), 4000, multiline=True), 'registered_by': str(actor),
           'registered_at': datetime.now(timezone.utc).isoformat(), 'api_verified': False}
    if status == 'merchant_provided_approval' and (
            not _has_approval_receipt(row) or not approval_date or not approved_category):
        raise HTTPException(422, '请登记实际 Seller Central 批准回执、批准日期及批准类目')
    if old:
        saved = s.save(c, EVIDENCE_KIND, old | row, expected=old['revision'])
    else:
        saved = s.put(c, EVIDENCE_KIND, uuid.uuid4().hex, row)
    s.put(c, EVIDENCE_KIND + '_version', f"{saved['id']}:{saved['revision']}",
          {'evidence_id': saved['id'], 'evidence_revision': saved['revision'], 'snapshot': copy.deepcopy(saved)})
    s.audit(c, 'gtin_exemption_evidence_registered', saved['id'],
            s.dump({'actor': str(actor), 'status': status, 'scope': expected_scope}))
    return saved


def evidence_view(c, target, brand, product_type):
    expected_scope = scope(target, brand, product_type)
    matching = [row for row in s.all_rows(c, EVIDENCE_KIND)
                if _same_scope(row.get('scope') or {}, expected_scope)]
    matching.sort(key=lambda row: (row.get('registered_at', ''), row['id']), reverse=True)
    latest = matching[0] if matching else None
    usable = bool(target.get('environment') == 'production' and _valid_scope(expected_scope)
                  and latest and latest.get('status') == 'merchant_provided_approval'
                  and latest.get('source') == 'seller_central' and _has_approval_receipt(latest)
                  and latest.get('approval_date') and latest.get('approved_category'))
    validations = [row for row in s.all_rows(c, 'identifier_listing_validation')
                   if _same_scope(row.get('scope') or {}, expected_scope)]
    validations.sort(key=lambda row: (row.get('checked_at', ''), row['id']), reverse=True)
    return {'scope': expected_scope, 'status': latest['status'] if latest else 'not_recorded',
            'usable': usable, 'api_supported': False, 'api_verified': False,
            'application_status': 'merchant_registered' if latest else 'not_registered',
            'evidence': latest, 'matching_evidence': matching,
            'latest_listing_validation': validations[0] if validations else None,
            'next_action': 'validate_listing',
            'message': ('已登记范围匹配的 Seller Central 批准依据；仍需完整刊登预检'
                        if usable else '批准回执尚未留存；系统会随实际刊登自动验证无 GTIN 是否可用。申请和批准结果在 Seller Central 办理'),
            'next_steps': [{'url': 'https://sellercentral.amazon.com/', 'title': '前往 Seller Central 办理商品标识豁免'}]}


def check_exemption(target: dict, brand: str, product_type: str) -> dict:
    """Compatibility endpoint: local evidence and guidance, without an Amazon call."""
    with s.main().db() as c:
        return evidence_view(c, target, brand, product_type)




def require_no_withdrawal(c, target, brand, product_type):
    """A preview cannot override an explicitly registered rejection/withdrawal."""
    report = evidence_view(c, target, brand, product_type)
    if report['status'] in {'rejected', 'revoked'}:
        raise HTTPException(422, '本范围已有拒绝或撤回记录，请核对 Seller Central 的最新结果后更新依据')
    return report


def payload_hashes(bodies):
    return {sku: hashlib.sha256(s.dump(body).encode('utf-8')).hexdigest()
            for sku, body in bodies.items()}


def record_listing_validation(c, target, brand, product_type, listing_id, version, bodies, responses):
    """Save exact-payload API validation separately from Seller Central approval.

    This is never an exemption grant or a reusable approval for another product.
    Every actual submit still performs fresh previews for all payloads.
    """
    expected_scope = scope(target, brand, product_type)
    if target.get('environment') != 'production' or not _valid_scope(expected_scope):
        raise HTTPException(422, '刊登标识验证必须绑定真实店铺和商品范围')
    results = {}
    for sku, response in responses.items():
        body = response.get('body') or {}
        issues = body.get('issues') or []
        results[sku] = {'status': body.get('status'), 'request_id': response.get('request_id', ''),
                        'issues': copy.deepcopy(issues)}
    rejected = any(row['status'] != 'VALID' or any(
        isinstance(issue, dict) and issue.get('severity') == 'ERROR' for issue in row['issues'])
        for row in results.values())
    complete = bool(bodies) and set(results) == set(bodies)
    status = 'rejected' if rejected else 'validated' if complete else 'pending'
    row = {'scope': expected_scope, 'target_id': target['id'], 'listing_id': listing_id,
           'draft_version': version, 'payload_hashes': payload_hashes(bodies), 'results': results,
           'status': status, 'usable_for_listing': status == 'validated',
           'source': 'amazon_listing_validation_preview', 'approval_status_verified': False,
           'checked_at': datetime.now(timezone.utc).isoformat()}
    return s.put(c, 'identifier_listing_validation', uuid.uuid4().hex, row)


def require_listing_validation(c, target, brand, product_type, listing_id, version, bodies, validation):
    if target.get('environment') != 'production':
        raise HTTPException(422, '真实平台刊登验证仅适用于正式店铺')
    require_no_withdrawal(c, target, brand, product_type)
    saved = s.get(c, 'identifier_listing_validation', validation['id']) if validation else None
    if not (saved and _same_scope(saved.get('scope') or {}, scope(target, brand, product_type))
            and saved.get('listing_id') == listing_id and saved.get('draft_version') == version
            and saved.get('status') == 'validated'
            and saved.get('source') == 'amazon_listing_validation_preview'
            and saved.get('payload_hashes') == payload_hashes(bodies)
            and set(saved.get('results') or {}) == set(bodies)
            and all(row.get('status') == 'VALID' and not any(
                isinstance(issue, dict) and issue.get('severity') == 'ERROR' for issue in row.get('issues', []))
                for row in saved['results'].values())):
        raise HTTPException(409, '当前无 GTIN 刊登尚未完成范围和资料版本匹配的平台预检')
    return saved
