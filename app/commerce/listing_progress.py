"""Read-only version-aware progress; an old buyable observation is not a new publish."""

import re
from . import listing_issues, listing_live


def validation_view(listing, jobs):
    """Expose the current preview without treating it as a publication receipt."""
    version = listing.get('draft_version') or 0
    from . import listing_release
    jobs=[job for job in jobs if int(job.get('payload',{}).get('publication_cycle_id') or 0)==listing_release.cycle(listing)]
    latest = max((job for job in jobs if job.get('kind') == 'submit'
                  and job.get('payload', {}).get('listing_id') == listing['id']
                  and job.get('payload', {}).get('version') == version
                  and job.get('payload', {}).get('preview')), key=lambda job: job['id'], default=None)
    if not latest:
        return {'status': 'not_checked', 'label': '当前资料尚未完成平台检查', 'error': ''}
    status = latest.get('status')
    labels = {'queued': '等待平台检查', 'running': '正在按平台要求检查',
              'retry_wait': '平台检查等待重试', 'paused': '平台检查未通过，需补充或修正',
              'unknown': '平台检查结果未确认'}
    if status == 'completed':
        status = 'passed' if latest.get('result', {}).get('accepted') else 'failed'
        label = '平台检查通过，尚不代表已发布' if status == 'passed' else '平台检查未通过'
    else:
        label = labels.get(status, '平台检查尚未完成')
    raw_error = latest.get('error', '')
    schema_text = raw_error.split('：', 1)[-1]
    required = [re.fullmatch(r"\s*:?\s*'([^']+)' is a required property\s*", part)
                for part in schema_text.split('；')]
    names = {'item_shape': '形状', 'model_name': '型号名称', 'model_number': '制造商型号编号',
             'pattern': '图案', 'special_feature': '产品特点',
             'required_product_compliance_certificate': '商品合规证明要求',
             'supplier_declared_dg_hz_regulation': '供应商危险品法规声明'}
    error = raw_error
    if required and all(required):
        fields = [match.group(1) for match in required]
        prefix = raw_error.split('：', 1)[0] + '：' if '：' in raw_error else ''
        error = prefix + '缺少' + '、'.join(dict.fromkeys(names.get(field, field) for field in fields))
    return {'status': status, 'label': f'当前资料 v{version}：{label}',
            'error': error, 'raw_error': raw_error, 'job_id': latest['id']}


def variation_readback(listing, target=None):
    draft=listing.get('draft') or {}
    if not draft.get('variation_theme'):
        change=(draft.get('source',{}).get('variation_family') or {}).get('existing_relationship_change') or {}
        if change.get('status')=='requires_platform_change':
            return {'status':'needs_relationship_update','label':'新方案改为分别上架；历史已发布合并关系仍需解除并回读'}
        if len(draft.get('skus',[]))>1:
            return {'status':'independent','label':'各实际SKU分别上架，不建立父子关系'}
        return {'status':'independent','label':'独立商品，无需父子关系'}
    from .listing_relationships import family_readback
    return family_readback(listing, target)


def view(listing, jobs, *, published_draft=None, target=None):
    version = listing.get('draft_version') or 0
    published = listing.get('published_version')
    from . import listing_release
    jobs=[job for job in jobs if int(job.get('payload',{}).get('publication_cycle_id') or 0)==listing_release.cycle(listing)]
    current_jobs = [job for job in jobs if job.get('kind') == 'submit'
                    and job.get('payload', {}).get('listing_id') == listing['id']
                    and job.get('payload', {}).get('version') == version
                    and not job.get('payload', {}).get('preview')]
    latest = max(current_jobs, key=lambda job: job['id'], default=None)
    pending_version = not published or version != published
    state = 'waiting' if pending_version else 'submitted'
    label = f'当前资料 v{version} 尚未发布' if pending_version else f'当前资料 v{version} 已提交，见平台回读'
    error = ''
    if latest and latest['status'] in {'paused', 'unknown', 'queued', 'running', 'retry_wait'}:
        state = latest['status']
        labels = {'paused': '提交暂停，需处理', 'unknown': '提交结果未知，需核对原请求',
                  'queued': '等待完整校验与提交', 'running': '正在完整校验与提交', 'retry_wait': '等待重试'}
        label = f'当前资料 v{version}：{labels[state]}'
        error = latest.get('error', '')
    observed = listing_issues.observed(listing)
    errors = [issue for issue in observed if issue['severity'] == 'ERROR']
    warnings = [issue for issue in observed if issue['severity'] == 'WARNING']
    improvements = [issue for issue in warnings if 'MISSING_ATTRIBUTE' in issue['categories']]
    qualification = any('QUALIFICATION_REQUIRED' in issue['categories'] for issue in errors)
    from . import listing_relationships
    platform_issues = listing_relationships.filter_issues(listing.get('platform_issues') or [], listing, target)
    incomplete = bool(pending_version or errors or state in {'paused', 'unknown', 'queued', 'running', 'retry_wait'}
                      or listing.get('status') not in {'buyable', 'mock_submitted'}
                      or listing.get('paused') or platform_issues or listing.get('material_issues'))
    variation=variation_readback(listing, target)
    incomplete=incomplete or variation['status'] not in {'independent','confirmed'}
    validation = validation_view(listing, jobs)
    incomplete = incomplete or validation['status'] in {'paused', 'unknown', 'failed', 'queued', 'running', 'retry_wait'}
    detail=listing.get('detail_publication') or {}
    detail_required=bool((listing.get('draft') or {}).get('detail_page'))
    detail_complete=bool(detail.get('complete') or detail.get('status')=='published')
    detail_pending=detail_required and not detail_complete
    incomplete=incomplete or detail_pending
    if detail_pending and not pending_version and listing.get('status')=='buyable' and not error:
        label='商品接口可售，图文详情尚未发布完成'
    live = listing_live.live_view(listing, published_draft, target)
    local = listing_live.local_view(listing, published_draft, live, {'state': state, 'error': error}, validation)
    needs_attention = bool(live['needs_attention'] or local['needs_attention'] or detail_pending
                           or listing.get('material_issues') or platform_issues
                           or variation['status'] not in {'independent', 'confirmed'})
    return {'draft_version': version, 'published_version': published, 'pending_version': pending_version,
            'state': state, 'label': label, 'error': error, 'incomplete': incomplete,
            'job_id': latest['id'] if latest else None,
            'can_retry': bool(latest and latest['status'] == 'paused'
                              and not latest.get('result', {}).get('platform_error', {}).get('result_unknown')),
            'platform_errors': errors, 'local_platform_issues': platform_issues, 'platform_warnings': warnings,
            'information_incomplete': bool(improvements), 'information_improvements': improvements,
            'qualification_required': qualification,
            'buyer_verification': 'not_recorded', 'variation': variation, 'validation': validation,
            'live': live, 'local': local, 'needs_attention': needs_attention,
            'detail_required':detail_required,'detail_complete':detail_complete,'detail_pending':detail_pending}
