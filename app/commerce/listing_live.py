"""Read-only UI facts about live goods, separate from editable material versions."""

from __future__ import annotations

from copy import deepcopy


def live_view(listing, published_draft=None, target=None):
    draft = published_draft if published_draft is not None else listing.get('draft') or {}
    scope = (draft.get('source') or {}).get('target_scope') or {}
    environment = (target or {}).get('environment') or scope.get('environment') or listing.get('environment')
    marketplace = (target or {}).get('marketplace_id') or scope.get('marketplace_id')
    scope_ok = bool(scope and marketplace)
    if target is not None:
        scope_ok = scope_ok and listing.get('target_id') == target.get('id') and all(
            scope.get(field) == target.get(key) for field, key in (
                ('target_id', 'id'), ('account_id', 'account_id'), ('seller_id', 'seller_id'),
                ('marketplace_id', 'marketplace_id'), ('environment', 'environment'), ('region', 'region')))
    rows = []
    receipts = listing.get('published_skus') or {}
    for sku in draft.get('skus') or []:
        name = sku.get('seller_sku')
        observation = (listing.get('platform_observed') or {}).get(name) or {}
        response = observation.get('response') or {}
        body = response.get('body') or {}
        summaries = [row for row in body.get('summaries') or []
                     if row.get('marketplaceId') == marketplace]
        asin = observation.get('confirmed_asin')
        confirmed = bool(scope_ok and observation.get('identity_status') == 'confirmed'
                         and response.get('http_status') == 200 and body.get('sku') == name
                         and asin and {row.get('asin') for row in summaries} == {asin})
        statuses = {value for row in summaries for value in row.get('status') or []}
        errors = [issue for issue in body.get('issues') or []
                  if isinstance(issue, dict) and issue.get('severity') == 'ERROR']
        buyable = confirmed and 'BUYABLE' in statuses
        state, label = 'waiting', '等待同步平台状态'
        if observation.get('identity_status') == 'identity_conflict' or observation.get('status') == 'identity_conflict':
            state, label = 'identity_conflict', '商品身份不一致，需处理'
        elif observation and not scope_ok:
            state, label = 'unverified', '店铺或站点已变化，需重新同步'
        elif observation.get('status') == 'not_found':
            state, label = 'not_found', '平台暂未查到本店刊登'
        elif confirmed:
            if any(str(issue.get('code')) != '100521' for issue in errors):
                state, label = 'issues', '在售，有平台问题' if buyable else '平台有待处理问题'
            elif 'SEARCH_SUPPRESSED' in statuses:
                state, label = 'suppressed', '搜索展示受限'
            elif errors:
                state, label = 'review', '亚马逊审核中'
            elif buyable:
                state, label = 'buyable', '在售'
            else:
                state, label = 'not_buyable', '当前不可售'
        elif observation:
            state, label = 'unverified', '平台结果尚未确认商品身份'
        rows.append({'seller_sku': name, 'sku_id': sku.get('sku_id'), 'asin': asin if confirmed else '',
                     'status': state, 'label': label, 'buyable': bool(buyable), 'confirmed': confirmed,
                     'submitted': bool(name in receipts or listing.get('published_version'))})
    total = len(rows)
    count = sum(row['buyable'] for row in rows)
    submitted = sum(row['submitted'] for row in rows)
    problematic = any(row['status'] in {'identity_conflict', 'issues', 'suppressed'} for row in rows)
    if environment != 'production':
        state = 'sample'
        label = '本地模拟结果' if environment == 'mock' else '沙盒或未配置正式店铺'
    elif (listing.get('retirement_verified_at') and listing.get('status') == 'not_submitted'
          and not listing.get('published_version') and not receipts):
        state, label = 'not_submitted', '未上架（本店刊登已删除）'
    elif (listing.get('publication_cycle_id') and not listing.get('published_version')
          and not receipts and not listing.get('platform_observed')):
        state, label = 'not_submitted', '未上架（重新刊登草稿）'
    elif (listing.get('platform_parent_observed') or {}).get('status')=='identity_conflict':
        state, label = 'issues', '父体目录身份不一致，需处理'
    elif problematic:
        state, label = 'issues', f'亚马逊待处理 · {count}/{total} 个销售选项在售'
    elif total and all(row['status'] == 'buyable' for row in rows):
        state, label = 'buyable', f'亚马逊在售 · {count}/{total} 个销售选项'
    elif count:
        state, label = 'partial_buyable', f'部分在售 · {count}/{total} 个销售选项'
    elif any(row['status'] == 'review' for row in rows):
        state, label = 'review', '亚马逊审核中'
    elif any(row['status'] == 'not_buyable' for row in rows):
        state, label = 'not_buyable', '亚马逊当前不可售'
    elif not submitted and (not listing.get('platform_observed') or all(row['status']=='not_found' for row in rows)):
        state, label = 'not_submitted', '尚未提交亚马逊'
    else:
        state, label = 'waiting', '等待同步亚马逊状态'
    return {'status': state, 'label': label, 'total': total, 'buyable': count,
            'submitted': submitted, 'confirmed': sum(row['confirmed'] for row in rows),
            'checked_at': listing.get('checked_at'), 'rows': rows,
            'needs_attention': state != 'buyable'}


def _content(draft, without_relationship=False):
    value = deepcopy(draft)
    value.pop('source', None)
    if without_relationship:
        for field in ('variation_theme', 'parent_sku'):
            value.pop(field, None)
        for sku in value.get('skus') or []:
            sku.pop('variation_attributes', None)
    return value


def _removed_relationship_matches(listing, draft, published, live):
    """Only an existing supported removal receipt and matching readback resolve this difference."""
    change = ((draft.get('source') or {}).get('variation_family') or {}).get('existing_relationship_change') or {}
    if (change.get('status') != 'confirmed_removed' or not published.get('variation_theme')
            or draft.get('variation_theme') or draft.get('parent_sku')
            or change.get('parent_sku') != published.get('parent_sku')
            or change.get('marketplace_id') != ((published.get('source') or {}).get('target_scope') or {}).get('marketplace_id')
            or change.get('published_version') != listing.get('published_version')
            or not live['total'] or live['confirmed'] != live['total']):
        return False
    receipts = {row.get('seller_sku'): row.get('asin') for row in change.get('receipts') or []}
    if receipts != {row['seller_sku']: row['asin'] for row in live['rows']}:
        return False
    for row in live['rows']:
        body = listing['platform_observed'][row['seller_sku']]['response']['body']
        relations = body.get('relationships')
        if not isinstance(relations, list):
            return False
        for site in relations:
            if site.get('marketplaceId') != change.get('marketplace_id'):
                return False
            if not isinstance(site.get('relationships'), list) or any(
                    relation.get('type') == 'VARIATION' for relation in site['relationships']):
                return False
    return _content(draft, True) == _content(published, True)


def local_view(listing, published_draft, live, submission, validation):
    pending = listing.get('draft_version') != listing.get('published_version')
    draft = listing.get('draft') or {}
    status, label, changed = 'submitted', '本地资料与已提交版本一致', False
    if not listing.get('published_version'):
        status, label, changed = 'not_submitted', '本地资料尚未提交', True
    elif pending:
        if published_draft is not None and _content(draft) == _content(published_draft):
            status, label = 'record_only', '仅本地记录更新，无商品内容待提交'
        elif published_draft is not None and _removed_relationship_matches(listing, draft, published_draft, live):
            status, label = 'reconciled', '商品拆分已同步，无需重新提交商品'
        else:
            status, label, changed = 'changed', '本地另有修改尚未提交；线上商品状态见上方', True
    active = submission['state'] in {'paused', 'unknown', 'queued', 'running', 'retry_wait'}
    preview_failed = changed and validation['status'] in {'paused', 'unknown', 'failed', 'queued', 'running', 'retry_wait'}
    return {'status': status, 'label': label, 'changed': changed,
            'active': active, 'needs_attention': bool(changed or active or preview_failed),
            'error': submission['error'] or (validation.get('error', '') if preview_failed else '')}
