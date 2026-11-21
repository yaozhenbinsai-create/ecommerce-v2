"""Read actual family relationships independently of local material version counters."""
import time


CHANGE_ISSUE = '已发布父子关系需先在平台解除或调整并回读；新分组尚未改变线上关系'


def current_scope(listing, target):
    draft = listing.get('draft') or {}
    scope = (draft.get('source') or {}).get('target_scope') or {}
    checked = listing.get('checked_at')
    return bool(target and listing.get('target_id') == target.get('id')
                and isinstance(checked, (int, float)) and time.time()-900 <= checked <= time.time()+5
                and all(scope.get(key) == target.get(value) and scope.get(key) is not None
                        for key, value in (('target_id', 'id'), ('account_id', 'account_id'),
                                           ('seller_id', 'seller_id'), ('marketplace_id', 'marketplace_id'),
                                           ('environment', 'environment'))))


def confirmed_identity(observed, sku, marketplace):
    response = observed.get('response') or {}
    body = response.get('body') or {}
    summaries = [row for row in body.get('summaries') or [] if row.get('marketplaceId') == marketplace]
    asin = observed.get('confirmed_asin')
    return bool(observed.get('identity_status') == 'confirmed' and response.get('http_status') == 200
                and response.get('request_id') and body.get('sku') == sku['seller_sku'] and asin
                and {row.get('asin') for row in summaries} == {asin})


def family_readback(listing, target=None):
    draft = listing.get('draft') or {}
    parent, theme = draft.get('parent_sku'), draft.get('variation_theme')
    marketplace = ((draft.get('source') or {}).get('target_scope') or {}).get('marketplace_id')
    skus = draft.get('skus') or []
    pending_version = listing.get('draft_version') != listing.get('published_version')
    strict = pending_version
    if strict and not current_scope(listing, target):
        return {'status': 'pending_submission', 'label': '本地规格方案已更新，等待同店铺商品身份和关系的完整回读'}
    confirmed, missing, conflicting = [], [], []
    if not parent or not theme or not marketplace or not skus:
        return {'status': 'unverified', 'label': '缺少父体或站点身份，尚不能核验规格关系'}
    for sku in skus:
        name = sku['seller_sku']
        observed = (listing.get('platform_observed') or {}).get(name) or {}
        body = (observed.get('response') or {}).get('body') or {}
        relations = [relation for site in body.get('relationships') or []
                     if site.get('marketplaceId') == marketplace
                     for relation in site.get('relationships') or [] if relation.get('type') == 'VARIATION']
        if (body.get('sku') != name or observed.get('identity_status') != 'confirmed' or not relations
                or strict and not confirmed_identity(observed, sku, marketplace)):
            missing.append(name)
            continue
        parents = {p for relation in relations for p in relation.get('parentSkus', [])}
        themes = {relation.get('variationTheme', {}).get('theme') or relation.get('variationTheme', {}).get('name')
                  for relation in relations} - {None, ''}
        dimensions = {tuple(sorted(str(v).lower() for v in relation.get('variationTheme', {}).get('attributes', [])))
                      for relation in relations if relation.get('variationTheme', {}).get('attributes')}
        expected = tuple(sorted(v.lower() for v in theme.split('/')))
        if parents == {parent} and (themes == {theme} or not themes and dimensions == {expected}):
            confirmed.append(name)
        elif parents == {parent} and not themes and not dimensions:
            missing.append(name)
        else:
            conflicting.append(name)
    status = 'confirmed' if len(confirmed) == len(skus) else 'mismatch' if conflicting else 'pending'
    label = f'父子关系已回读：{len(confirmed)}/{len(skus)} 个子规格关联 {parent}'
    if status == 'pending':
        label = f'规格关系等待平台回读：已确认 {len(confirmed)}/{len(skus)} 个子规格'
    elif status == 'mismatch':
        label = '平台规格关系与本次方案不一致：' + '、'.join(conflicting)
    return {'status': status, 'label': label, 'parent_sku': parent, 'marketplace_id': marketplace,
            'confirmed': confirmed, 'missing': missing, 'conflicting': conflicting,
            'material_version_submitted': not pending_version}


def filter_issues(issues, listing, target):
    """A fresh matched family resolves only the relationship warning, never other draft edits."""
    if CHANGE_ISSUE not in issues or not (listing.get('draft') or {}).get('variation_theme'):
        return issues
    if current_scope(listing, target) and family_readback(listing, target)['status'] == 'confirmed':
        return [issue for issue in issues if issue != CHANGE_ISSUE]
    return issues
