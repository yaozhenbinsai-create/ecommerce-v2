from __future__ import annotations
import copy
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from . import store as s, amazon, catalog, operations, reports, business_mapping, aftersales, aftersales_reports, detail_publication, support, main_images
from app import error_log

LOG=logging.getLogger(__name__)
STARTED=False
WORKER_THREAD=None


def authorize_preparation(c, job):
    """An explicit local step owns only its dependency, never its old workflow."""
    from . import execution_controls
    if job['kind'] not in execution_controls.DEPENDENT_KINDS or not job['payload'].get('listing_id'):
        return job
    if job['status'] == 'unknown':
        raise HTTPException(409, '原请求结果不明，不能重新发送')
    payload = job['payload'] | {'manual_preparation_pause_epoch': execution_controls.pause_epoch(c),
                                'manual_preparation_kind': job['kind']}
    if payload != job['payload']:
        c.execute('UPDATE commerce_jobs SET payload=? WHERE id=?', (s.dump(payload), job['id']))
    return s.job(c, job['id'])


def retry(c, ident):
    """Continue a definite paused job without losing its cumulative attempts."""
    row = s.job(c, ident)
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['status'] == 'unknown':
        raise HTTPException(409, '请先核对平台结果，不能直接重发未知写入')
    if row['status'] != 'paused':
        raise HTTPException(409, '任务当前不可重试')
    row = authorize_preparation(c, row)
    if row['kind'] == 'submit':
        from . import listing_release, execution_controls
        payload = dict(row['payload'])
        listing = s.get(c, 'listing', payload['listing_id'])
        listing_release.assert_job(listing, payload, 'preview' if payload.get('preview') else 'submit')
        if payload['version'] != listing['draft_version']:
            raise HTTPException(409, '原提交属于旧资料版本，请从当前商品资料提交')
        if not payload.get('preview'):
            listing_release.begin_first_publication(c, listing)
            payload['manual_pause_epoch'] = execution_controls.pause_epoch(c)
            c.execute('UPDATE commerce_jobs SET payload=? WHERE id=?', (s.dump(payload), ident))
    result = dict(row.get('result') or {})
    for key in ('model_retry_round', 'image_retry_round', 'image_model_role', 'image_role_attempts'):
        result.pop(key, None)
    s.audit(c, 'job_continuation_requested', ident, s.dump({'kind': row['kind'], 'prior_error': row['error'],
                                                       'attempts': row['attempts']}))
    c.execute("UPDATE commerce_jobs SET status='queued',role_attempts=0,model_role=0,next_at=0,error='',result=? WHERE id=?",
              (s.dump(result), ident))
    return s.job(c, ident)


def listing_response_status(response,preview=False):
    body=response.get('body') if isinstance(response,dict) else None
    status=body.get('status') if isinstance(body,dict) else None
    known={'VALID','INVALID'} if preview else {'ACCEPTED','INVALID'}
    if not isinstance(status,str) or status not in known:
        raise amazon.PlatformError('平台未返回可核实的商品提交状态，请核对平台结果',503,unknown=not preview)
    return status


def validate_attributes(schema,attributes):
    from jsonschema import Draft201909Validator,validators,ValidationError
    # Do not retrieve arbitrary external references from platform documents at validation time.
    from referencing import Registry
    def unique_count(instance,rule):
        selectors=rule.get('selectors')
        return len({s.dump({k:v.get(k) for k in selectors} if selectors and isinstance(v,dict) else v) for v in instance})
    def maximum(v,limit,instance,rule):
        if isinstance(instance,list) and unique_count(instance,rule)>limit:yield ValidationError('独立选择组合超过平台数量上限')
    def minimum(v,limit,instance,rule):
        if isinstance(instance,list) and unique_count(instance,rule)<limit:yield ValidationError('独立选择组合少于平台数量下限')
    def max_bytes(v,limit,instance,rule):
        if isinstance(instance,str) and len(instance.encode('utf-8'))>limit:yield ValidationError('文本UTF-8字节数超过平台上限')
    def min_bytes(v,limit,instance,rule):
        if isinstance(instance,str) and len(instance.encode('utf-8'))<limit:yield ValidationError('文本UTF-8字节数少于平台下限')
    def unique_items(v,enabled,instance,rule):
        if enabled and isinstance(instance,list) and unique_count(instance,rule)!=len(instance):yield ValidationError('平台选择组合重复')
    validator=validators.extend(Draft201909Validator,{'maxUniqueItems':maximum,'minUniqueItems':minimum,
              'maxUtf8ByteLength':max_bytes,'minUtf8ByteLength':min_bytes,'uniqueItems':unique_items})(schema,registry=Registry())
    try:return ['/'.join(map(str,e.absolute_path))+': '+e.message for e in validator.iter_errors(attributes)][:30]
    except Exception as exc:raise HTTPException(422,'平台规则校验未能完成：'+type(exc).__name__)


def save_listing_check(ident,version,field,value,*,job_id=None):
    """Keep full evidence even when a later preflight step stops the job."""
    with s.transaction() as c:
        report=s.by_key(c,'listing_checks',f'{ident}:{version}') or {}
        report.update(listing_id=ident,version=version)
        report[field]=copy.deepcopy(value)
        if field == 'preflight_results' and job_id is not None:
            report['preflight_job_id'] = job_id
        s.put(c,'listing_checks',f'{ident}:{version}',report)
        current=s.get(c,'listing',ident)
        if current['draft_version']==version:
            current[field]=copy.deepcopy(value)
            if field == 'preflight_results' and job_id is not None:
                current['preflight_job_id'] = job_id
            s.save(c,'listing',current)


def submit(job):
    from .targets import selected_targets
    from . import locales, shipping, identifiers, main_images, listing_identity, execution_controls, listing_release, merchant_terms
    ident=job['payload']['listing_id'];preview=job['payload'].get('preview',False);version=job['payload']['version']
    with s.main().db() as c:
        if not preview:execution_controls.assert_submission_allowed(c,job['payload'])
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        listing_release.assert_job(listing,job['payload'],'preview' if preview else 'submit')
        selected_targets(c,[target['id']])
        if listing['draft_version'] != version:
            raise HTTPException(409,'刊登资料版本已更新，请使用当前版本重新预检')
        draft=s.by_key(c,'material_version',f'{ident}:{version}')['draft']
        product=s.get(c,'product',listing['product_id'])
        source_variants=[s.get(c,'sku',row['sku_id']) for row in draft.get('skus',[])]
        listing_identity.assert_source_current(c,listing,target,draft)
        if ((product.get('brand_status') in {'branded','unbranded','logo_only'} or
             (product.get('brand_source') or {}).get('kind')=='owner_product_unknown') and
                str(draft.get('brand') or '').strip().casefold()!=
                str(product.get('brand') or '').strip().casefold()):
            raise HTTPException(409,'商品品牌状态已变化，请重新生成生产资料')
        locales.assert_current(target,draft)
        global_settings=s.by_key(c,'settings','global') or {}
        merchant_terms.validate_submission(c,target,draft)
    if target['environment']=='production' and not preview and not global_settings.get('live_writes_enabled'):
        raise HTTPException(422,'正式写入未启用，请在经营设置中配置后再提交')
    if not target.get('enabled'):raise HTTPException(409,'上架目标已停用')
    if target['environment']=='production':
        from . import sale_price_hold
        sale_price_hold.assert_released(product,preview=preview)
    effective_draft=copy.deepcopy(draft)
    with s.main().db() as c:
        for sku in effective_draft['skus']:
            sku['submission_intent']=listing_identity.submission_intent(c,listing,sku)
    effective_draft['skus']=[listing_identity.pin_owned_update(listing,target,sku)
                             for sku in effective_draft['skus']]
    is_offer=listing_identity.offer_only
    if target['environment']=='production' and not effective_draft.get('shipping_group'):
        shipping_type=(effective_draft.get('product_type') if any(not is_offer(row)
                       for row in effective_draft.get('skus',[])) else 'PRODUCT')
        selected=shipping.default_value(target,shipping_type)
        if selected:
            shipping.ensure_selected(target,shipping_type,selected)
            effective_draft['shipping_group']=selected
    identifier_check=None;identifier_validation=None
    media_exclusions={}
    if target['environment']=='production':
        source_skus={v.get('id'):v for v in (draft.get('source') or {}).get('skus',[])}
        for sku in effective_draft['skus']:
            source=source_skus.get(sku.get('sku_id')) or {}
            if source.get('gtin_issue') and not sku.get('gtin') and not sku.get('asin'):
                raise HTTPException(422,sku['seller_sku']+'：'+source['gtin_issue'])
            if source.get('asin_issue') and not sku.get('asin'):
                raise HTTPException(422,sku['seller_sku']+'：'+source['asin_issue'])
        missing_identifier=[sku for sku in effective_draft['skus'] if not sku.get('asin') and not sku.get('gtin')]
        for sku in effective_draft['skus']:
            # A legacy/UI boolean or arbitrary attributes are not approval evidence.
            sku['gtin_exempt']=False
            (sku.get('attributes') or {}).pop('supplier_declared_has_product_identifier_exemption',None)
            if sku.get('gtin') and not identifiers.valid_gtin(sku['gtin']):
                raise HTTPException(422,sku['seller_sku']+'：GTIN校验位错误，请填写属于该商品的有效UPC/EAN')
        (effective_draft.get('attributes') or {}).pop('supplier_declared_has_product_identifier_exemption',None)
        (effective_draft.get('offer_attributes') or {}).pop('supplier_declared_has_product_identifier_exemption',None)
        (effective_draft.get('parent_attributes') or {}).pop('supplier_declared_has_product_identifier_exemption',None)
        if missing_identifier:
            with s.main().db() as c:
                identifier_check=identifiers.require_no_withdrawal(c,target,effective_draft.get('brand',''),effective_draft.get('product_type',''))
            save_listing_check(ident,version,'identifier_check',identifier_check)
            if str(effective_draft.get('brand') or '').casefold()=='generic':
                # Match Seller Central's approved no-brand spelling without
                # changing the imported archive or its frozen source history.
                effective_draft['brand']='Generic'
            for sku in missing_identifier:sku['gtin_exempt']=True
    errors,deferred_issues=catalog.submission_issues(effective_draft,target,listing)
    if errors:raise HTTPException(422,'请补齐：'+'；'.join(errors))
    if listing.get('paused') and not preview:raise HTTPException(409,'商品已暂停销售，请先恢复或只做验证预览')
    definition=None;parent_definition=None;offer_definition=None
    if target['environment']!='mock':
        parentage='CHILD' if effective_draft.get('variation_theme') else 'NONE'
        has_new=any(not is_offer(v) for v in effective_draft['skus'])
        with s.main().db() as c:definition=s.by_key(c,'definition',f"{target['id']}:{draft['product_type']}:{parentage}") if has_new else None
        if has_new and ((target['environment']=='production' and (missing_identifier or effective_draft.get('variation_theme'))) or
                        not amazon.definition_matches(definition,target,draft['product_type'],parentage) or
                        time.time()-definition.get('fetched_at',0)>86400):
            definition=amazon.definition(target,draft['product_type'],parentage)
            amazon.assert_definition_scope(definition,target,draft['product_type'],parentage)
            with s.transaction() as c:s.put(c,'definition',f"{target['id']}:{draft['product_type']}:{parentage}",definition|{'target_id':target['id']})
        if draft.get('variation_theme'):
            parent_definition=amazon.definition(target,draft['product_type'],'PARENT')
            amazon.assert_definition_scope(parent_definition,target,draft['product_type'],'PARENT')
            with s.transaction() as c:s.put(c,'definition',f"{target['id']}:{draft['product_type']}:PARENT",parent_definition|{'target_id':target['id']})
        if any(is_offer(v) for v in effective_draft['skus']):
            offer_definition=amazon.definition(target,'PRODUCT','NONE','LISTING_OFFER_ONLY')
            amazon.assert_definition_scope(offer_definition,target,'PRODUCT','NONE','LISTING_OFFER_ONLY')
        if any(d and d.get('sample_only') for d in [definition,parent_definition,offer_definition]):
            raise HTTPException(422,'官方静态沙箱不提供真实商品属性表。请配置真实授权店铺后获取商品规则。')
    if target['environment']=='production':
        selected_definition=definition or offer_definition
        if selected_definition:
            shipping_rule=shipping.requirement(selected_definition['schema'])
            if shipping_rule['required'] is True and not effective_draft.get('shipping_group'):
                raise HTTPException(422,'当前店铺商品类型要求配送模板；请从平台模板列表中选择')
            if shipping_rule['status']=='absent' and effective_draft.get('shipping_group'):
                raise HTTPException(422,'当前商品类型不接受配送模板，请清空已选模板')
        if effective_draft.get('shipping_group'):
            shipping.ensure_selected(target,effective_draft.get('product_type') if definition else 'PRODUCT',
                                     effective_draft['shipping_group'])
        with s.main().db() as c:
            for sku in effective_draft['skus']:
                if is_offer(sku):continue
                selected=[img for img in sku.get('images',[]) if img.get('role')=='main']
                if len(selected)!=1:raise HTTPException(422,sku['seller_sku']+'：请选择一张主图')
                main_images.validate_selection(c,effective_draft,sku,selected[0]['asset_id'])
                excluded=main_images.validate_auxiliary(c,effective_draft,sku)
                if excluded['reasons']:media_exclusions[sku['seller_sku']]=excluded['reasons']
    results={};bodies={};packaging_records={}
    if (target['environment']=='production' and definition and
            (effective_draft.get('variation_theme') or effective_draft.get('source',{}).get('variation_family'))):
        from . import variation_families
        variation_families.assert_compatible(effective_draft,source_variants,definition['schema'],target)
    for sku in effective_draft['skus']:
        selected_definition=offer_definition if is_offer(sku) else definition
        packaging_records[sku['seller_sku']]={}
        a=catalog.attributes(target,effective_draft,sku,
                             selected_definition['schema'] if selected_definition else None,
                             packaging_provenance=packaging_records[sku['seller_sku']])
        if sku.get('asin') and target['environment']!='mock':
            from .listing_workflow import _verified_catalog_item
            catalog_item=amazon.get_catalog_item(target,sku['asin'])
            _verified_catalog_item(sku,effective_draft,target,catalog_item.get('body') or {},identifier=sku.get('gtin') or '')
            restricted=amazon.restrictions(target,sku['asin'])
            if restricted.get('restrictions'):raise HTTPException(422,sku['seller_sku']+'：已有ASIN存在销售限制，请在平台查看准入要求')
        if selected_definition:
            schema_errors=validate_attributes(selected_definition['schema'],a)
            if schema_errors:raise HTTPException(422,sku['seller_sku']+'：'+'；'.join(schema_errors))
        bodies[sku['seller_sku']]={'productType':'PRODUCT' if is_offer(sku) else effective_draft['product_type'],
                                 'requirements':'LISTING_OFFER_ONLY' if is_offer(sku) else 'LISTING','attributes':a}
    entries=list(effective_draft['skus'])
    if effective_draft.get('variation_theme'):
        a=catalog.parent_attributes(target,effective_draft,parent_definition['schema'] if parent_definition else None)
        if parent_definition:
            errors=validate_attributes(parent_definition['schema'],a)
            if errors:raise HTTPException(422,'父体：'+'；'.join(errors))
        listing_release.verify_parent(listing,target,effective_draft,a,parent_definition)
        bodies[effective_draft['parent_sku']]={'productType':effective_draft['product_type'],'requirements':'LISTING','attributes':a}
        entries.insert(0,{'seller_sku':effective_draft['parent_sku'],'parent':True})
    release_snapshot=listing_release.snapshot(listing,target,draft,bodies) | {'job_id': job.get('id')}
    if not preview:
        # Inspect every durable receipt before the first write, including a
        # receipt for a later child in a partially completed batch.
        with s.main().db() as c:
            for sku in entries:
                receipt=s.by_key(c,'submission',f"{ident}:{version}:{sku['seller_sku']}:False")
                if receipt and int(receipt.get('publication_cycle_id') or 0)!=listing_release.cycle(listing):
                    raise HTTPException(409,'历史回执不属于当前刊登批次')
                listing_release.assert_accepted_request(receipt,bodies[sku['seller_sku']])
                if receipt and receipt.get('status') in {'unknown','sending'}:
                    raise amazon.PlatformError('之前提交结果待核实，请先刷新平台状态',0,unknown=True)
    save_listing_check(ident,version,'publication_snapshot',release_snapshot)
    save_listing_check(ident,version,'packaging_projection',packaging_records)
    if target['environment'] == 'production':
        from .media import verify_submission
        verify_submission(bodies, listing_id=ident, target_id=target['id'], account_id=target['account_id'],
                          marketplace_id=target['marketplace_id'], draft_version=version, job_id=job.get('id'))
        if not preview:
            # Preview every payload before the first live write. This is
            # especially important when an identifier is absent, because a
            # restrictions query alone cannot prove a GTIN exemption.
            preflight_results={}
            for sku in entries:
                response=amazon.put_listing(target,sku['seller_sku'],bodies[sku['seller_sku']],True)
                preflight_results[sku['seller_sku']]=response
                save_listing_check(ident,version,'preflight_results',preflight_results,job_id=job.get('id'))
                if missing_identifier:
                    with s.transaction() as c:
                        identifier_validation=identifiers.record_listing_validation(c,target,
                            effective_draft['brand'],effective_draft['product_type'],ident,version,bodies,preflight_results)
                    identifier_check['listing_validation']=identifier_validation
                    save_listing_check(ident,version,'identifier_check',identifier_check)
                status=listing_response_status(response,True)
                errors=[item for item in response['body'].get('issues',[])
                        if isinstance(item,dict) and item.get('severity')=='ERROR']
                if status!='VALID' or errors:
                    error_log.record('amazon.listing_preview_rejected',listing_id=ident,
                                     seller_sku=sku['seller_sku'],target_id=target['id'],
                                     draft_version=version,platform_request_id=response.get('request_id'),
                                     issues=errors)
                    raise HTTPException(422,sku['seller_sku']+'：亚马逊刊登预检未通过：'+
                                        '；'.join(str(item.get('message') or item.get('code') or '字段错误') for item in errors[:5])
                                        if errors else sku['seller_sku']+'：亚马逊刊登预检未返回 VALID')
    preview_responses={}
    # Validate all local payloads before the first remote mutation.
    for sku in entries:
        key=f"{ident}:{version}:{sku['seller_sku']}:{preview}"
        with s.transaction() as c:
            current_listing=s.get(c,'listing',ident)
            if not preview:execution_controls.assert_submission_allowed(c,job['payload'])
            current_target=s.get(c,'target',current_listing['target_id'])
            listing_release.assert_job(current_listing,job['payload'],'preview' if preview else 'submit')
            current_material=s.by_key(c,'material_version',f'{ident}:{version}')
            listing_release.assert_snapshot(current_listing,current_target,current_material['draft'],release_snapshot,bodies)
            listing_identity.assert_source_current(c,current_listing,current_target,draft)
            if not current_target.get('enabled') or (current_listing.get('paused') and not preview):
                raise HTTPException(409,'平台预检期间店铺或刊登已停用，请先核对当前状态')
            locales.assert_current(current_target,draft)
            merchant_terms.validate_submission(c,current_target,draft)
            if (target['environment']=='production' and not preview and
                    not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled')):
                raise HTTPException(409,'平台预检期间正式写入开关已关闭')
            if current_target!=target:
                # Changes made during remote preflight must not reach a stale write.
                if listing_identity.target_scope(current_target)!=listing_identity.target_scope(target):
                    raise HTTPException(409,'平台预检期间店铺作用域已变化，请重新预检')
            if current_listing['draft_version']!=version:
                raise HTTPException(409,'平台预检期间刊登资料已更新，请重新预检当前版本')
            if target['environment']=='production' and missing_identifier:
                if preview:
                    identifiers.require_no_withdrawal(c,current_target,effective_draft['brand'],effective_draft['product_type'])
                else:
                    identifiers.require_listing_validation(c,current_target,effective_draft['brand'],
                        effective_draft['product_type'],ident,version,bodies,identifier_validation)
            receipt=s.by_key(c,'submission',key)
            if receipt and int(receipt.get('publication_cycle_id') or 0)!=listing_release.cycle(current_listing):
                raise HTTPException(409,'历史回执不属于当前刊登批次')
            if receipt and receipt.get('status')=='accepted':
                if not preview:listing_release.assert_accepted_request(receipt,bodies[sku['seller_sku']])
                results[sku['seller_sku']]=receipt;continue
            if receipt and receipt.get('status') in {'unknown','sending'}:
                raise amazon.PlatformError('之前提交结果待核实，请先刷新平台状态',0,unknown=True)
            from . import offer_defaults
            old_allocation=offer_defaults.claim(c,current_listing,sku,target,draft,sku['quantity']) if not preview and not sku.get('parent') else None
            s.put(c,'submission',key,{'listing_id':ident,'version':version,'seller_sku':sku['seller_sku'],'preview':preview,'status':'sending','request':bodies[sku['seller_sku']],'previous_allocation':old_allocation,'publication_cycle_id':listing_release.cycle(current_listing)})
        try:
            r={'body':{'status':'VALID' if preview else 'ACCEPTED','issues':[]},'request_id':'MOCK','http_status':202} if target['environment']=='mock' else amazon.put_listing(target,sku['seller_sku'],bodies[sku['seller_sku']],preview)
            if preview and target['environment']=='production' and missing_identifier:
                preview_responses[sku['seller_sku']]=r
                with s.transaction() as c:
                    identifier_validation=identifiers.record_listing_validation(c,target,
                        effective_draft['brand'],effective_draft['product_type'],ident,version,bodies,preview_responses)
                identifier_check['listing_validation']=identifier_validation
                save_listing_check(ident,version,'identifier_check',identifier_check)
            failures=[v for v in r['body'].get('issues',[]) if isinstance(v,dict) and v.get('severity')=='ERROR']
            status='accepted' if listing_response_status(r,preview) in {'VALID','ACCEPTED'} and not failures else 'rejected'
            if status=='rejected':
                error_log.record('amazon.listing_rejected',listing_id=ident,seller_sku=sku['seller_sku'],
                                 target_id=target['id'],draft_version=version,preview=preview,
                                 platform_request_id=r.get('request_id'),http_status=r.get('http_status'),issues=failures)
            with s.transaction() as c:
                receipt=s.by_key(c,'submission',key);receipt.update(status=status,response=r)
                results[sku['seller_sku']]=s.save(c,'submission',receipt)
                if status=='rejected' and not preview and not sku.get('parent'):restore_allocation(c,ident,sku,target,old_allocation)
                if status=='accepted' and not preview:
                    current=s.get(c,'listing',ident);current.setdefault('published_skus',{})[sku['seller_sku']]=version;s.save(c,'listing',current)
                current=s.get(c,'listing',ident)
                current['preview_results' if preview else 'sku_results']=results
                if status=='rejected':current.update(status='partial_error',last_error='平台返回字段问题，请查看SKU结果')
                s.save(c,'listing',current)
            if sku.get('parent') and status=='rejected':raise HTTPException(422,'父体未通过，子体保留待提交，请先修正父体属性')
        except amazon.PlatformError as exc:
            with s.transaction() as c:
                receipt=s.by_key(c,'submission',key);receipt.update(status='unknown' if exc.unknown and not preview else 'failed',error=str(exc));s.save(c,'submission',receipt)
                if not preview and not exc.unknown and not sku.get('parent'):restore_allocation(c,ident,sku,target,old_allocation)
            if preview and exc.unknown:exc.unknown=False
            raise
    accepted=all(x['status']=='accepted' for x in results.values())
    with s.transaction() as c:
        current=s.get(c,'listing',ident)
        current['last_error']='' if accepted else '平台返回字段问题，请查看SKU结果'
        if target['environment']=='production':current['media_exclusions']=media_exclusions
        if target['environment']=='mock':current['sample_unverified_issues']=deferred_issues
        if not draft.get('detail_page'):current.pop('detail_publication_warning',None)
        if preview:current['preview_results']=results
        else:
            current['sku_results']=results;current['status']='mock_submitted' if target['environment']=='mock' and accepted else ('processing' if accepted else 'partial_error')
            if accepted:current['published_version']=version
        s.save(c,'listing',current)
        if not preview and listing_release.cycle(current):
            publication=s.get(c,'listing_publication_cycle',listing_release.cycle(current))
            publication.update(status='submitted' if accepted else 'partial_submitted',version=version,request_sha256=release_snapshot['request_sha256'])
            s.save(c,'listing_publication_cycle',publication)
        if accepted and not preview and draft.get('detail_page'):
            try:
                detail_publication.ready(c,current,version,
                    **({'manual_pause_epoch':execution_controls.submission_epoch(c,job['payload'])}
                       if execution_controls.submission_epoch(c,job['payload']) is not None else {}))
                latest=s.get(c,'listing',ident)
                if latest.pop('detail_publication_warning',None):s.save(c,'listing',latest)
            except HTTPException as exc:
                latest=s.get(c,'listing',ident)
                latest['detail_publication_warning']=str(exc.detail)
                s.save(c,'listing',latest)
        if accepted and not preview and target['environment']!='mock':s.enqueue(c,'refresh',f'refresh:{ident}:{version}:{int(time.time())}',listing_release.bind_payload(current,{'listing_id':ident}))
    return {'listing_id':ident,'accepted':accepted,'preview':preview,'environment':target['environment'],
            'identifier_check':identifier_check,'media_exclusions':media_exclusions}


def restore_allocation(c,ident,sku,target,old):
    s.put(c,'allocation',f"{ident}:{sku['sku_id']}",old or {'listing_id':ident,'sku_id':sku['sku_id'],'environment':target['environment'],'quantity':0})


def refresh(job):
    from . import listing_identity, listing_release
    ident=job['payload']['listing_id']
    with s.main().db() as c:
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        if listing.get('retirement_id'):
            retirement=s.get(c,'listing_retirement',listing['retirement_id'],required=False)
            if retirement and retirement.get('status') in {'removing','parents_processing','completed'}:
                return {'listing_id':ident,'status':listing['status'],'retired':True}
        listing_release.assert_job(listing,job['payload'],'refresh')
        version=listing.get('published_version') or listing['draft_version']
        draft=s.by_key(c,'material_version',f'{ident}:{version}')['draft']
    states={};responses={}
    for sku in draft['skus']:
        if target['environment']=='mock':
            with s.main().db() as c:allocation=s.by_key(c,'allocation',f"{ident}:{sku['sku_id']}") or {}
            states[sku['seller_sku']]={'status':'mock_buyable' if allocation.get('quantity',0)>0 else 'mock_not_buyable'};continue
        try:response=amazon.get_listing(target,sku['seller_sku'])
        except amazon.PlatformError as exc:
            if exc.status!=404 or not exc.request_id or exc.unknown:raise
            states[sku['seller_sku']]={'status':'not_found','response':{'http_status':404,
                'request_id':exc.request_id,'body':{'sku':sku['seller_sku']}}}
            continue
        body=response['body']
        listing_errors = [v for v in body.get('issues', []) if isinstance(v, dict) and v.get('severity') == 'ERROR']
        if listing_errors:
            error_log.record('amazon.listing_processing_error', listing_id=ident, seller_sku=sku['seller_sku'],
                             target_id=target['id'], platform_request_id=response.get('request_id'), issues=listing_errors)
        if target['environment']=='sandbox':
            states[sku['seller_sku']]={'status':'sandbox_sample','sample_only':True,'response':response};continue
        summaries=[v for v in body.get('summaries',[]) if isinstance(v,dict) and v.get('marketplaceId')==target['marketplace_id']]
        statuses={v for summary in summaries for v in summary.get('status',[]) if isinstance(v,str)}
        status=('buyable' if 'BUYABLE' in statuses else
                'platform_error' if listing_errors else
                'search_suppressed' if 'SEARCH_SUPPRESSED' in statuses else
                'processing' if not summaries else 'not_buyable')
        warnings = [v for v in body.get('issues', []) if isinstance(v, dict) and v.get('severity') == 'WARNING']
        states[sku['seller_sku']]={'status':status,'response':response,'issue_count':len(listing_errors),
            'warning_count':len(warnings),'improvement_count':sum('MISSING_ATTRIBUTE' in v.get('categories',[]) for v in warnings)}
        responses[sku['seller_sku']]=(sku,body,response.get('request_id',''))
    parent_response=None
    if target['environment']=='production' and draft.get('variation_theme'):
        try:parent_response=amazon.get_listing(target,draft['parent_sku'])
        except amazon.PlatformError as exc:
            if exc.status!=404 or not exc.request_id or exc.unknown:raise
            parent_response={'http_status':404,'request_id':exc.request_id,'body':{}}
    with s.transaction() as c:
        current=s.get(c,'listing',ident)
        if current.get('retirement_id'):return {'retired':True,'status':current['status']}
        listing_release.assert_job(current,job['payload'],'refresh')
        actual_target=s.get(c,'target',current['target_id'])
        if ((current.get('published_version') or current['draft_version'])!=version
                or listing_identity.target_scope(actual_target)!=listing_identity.target_scope(target)):
            return {'listing_id':ident,'stale':True,'status':current['status']}
        current['platform_observed']=states;current['checked_at']=time.time()
        if target['environment']=='production':
            for seller_sku,(sku,body,request_id) in responses.items():
                identity=listing_identity.record(c,target,current,sku,body,request_id)
                states[seller_sku]['identity_status']=identity['status']
                if identity.get('asin'):states[seller_sku]['confirmed_asin']=identity['asin']
                if identity.get('message'):states[seller_sku]['identity_message']=identity['message']
                if identity['status']=='identity_conflict':states[seller_sku]['status']='identity_conflict'
        count=sum(v['status'] in {'buyable','mock_buyable'} for v in states.values())
        parent_state=listing_release.parent_observation(c,current,target,parent_response) if parent_response and parent_response.get('http_status')==200 else {'status':'processing'} if parent_response else {}
        if parent_state:current['platform_parent_observed']=parent_state
        failed=any(v['status'] in {'platform_error','identity_conflict'} for v in states.values()) or parent_state.get('status')=='identity_conflict'
        missing=any(v['status']=='not_found' for v in states.values())
        pending=any(v['status']=='processing' for v in states.values()) or (missing and not current.get('published_version') and bool(current.get('published_skus')))
        current['status']='paused' if current.get('paused') else ('partial_error' if failed else
            'buyable' if count==len(states) else 'partial_buyable' if count else 'processing' if pending else 'not_buyable')
        if not count and not current.get('published_version') and not current.get('published_skus') and states and all(v['status']=='not_found' for v in states.values()):
            current['status']='not_submitted'
        if target['environment']=='mock' and not current.get('paused'):current['status']='mock_buyable' if count else 'mock_not_buyable'
        if target['environment']=='sandbox':current['status']='sandbox_sample'
        if count==len(states) and count and time.time()-current.get('previous_observed_at',time.time())<3600:
            current['buyable_since']=current.get('buyable_since') or time.time()
        else:current['buyable_since']=None
        current['previous_observed_at']=time.time()
        s.save(c,'listing',current)
    return states


def reconcile_shipment(job):
    ident=job['payload']['shipment_id']
    with s.main().db() as c:
        ship=s.get(c,'shipment',ident);target=s.get(c,'target',ship['target_id'])
    if ship.get('fulfillment_mode')=='offline':
        from . import order_logistics
        try:return order_logistics.reconcile(ident)
        except Exception as exc:
            with s.transaction() as c:
                current=s.get(c,'shipment',ident)
                current['last_error']=str(exc.detail) if isinstance(exc,HTTPException) else str(exc);s.save(c,'shipment',current)
            raise
    if ship['status']=='confirmed':return ship
    raw=amazon.get_order(target,ship['external_order_id'])
    match=next((p for p in raw.get('packages',[]) if p.get('packageReferenceId')==ship['platform_package_id'] and p.get('trackingNumber')==ship['tracking']),None)
    if not match:raise amazon.PlatformError('平台暂未找到对应包裹，保留待核实状态；稍后再次核对',0,unknown=True)
    expected={i['item_id']:i['quantity'] for i in ship['items']}
    observed={i.get('orderItemId'):i.get('quantity') for i in match.get('packageItems',[])}
    if observed!=expected:raise HTTPException(409,'平台包裹数量与本地不一致，请核对')
    result=operations.finalize_shipment(ident,{'source':'getOrder','package':match})
    with s.transaction() as c:
        c.execute("UPDATE commerce_jobs SET status='completed',error='',result=? WHERE kind='shipment' AND status='unknown' AND json_extract(payload,'$.shipment_id')=?",(s.dump({'reconciled':True}),ident))
    return result


def reconcile_listing(job):
    from . import listing_identity,listing_release
    ident=job['payload']['listing_id']
    with s.main().db() as c:
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        listing_release.assert_job(listing,job['payload'],'reconcile')
        receipts=[v for v in s.all_rows(c,'submission') if v['listing_id']==ident and not v['preview'] and v['status'] in {'sending','unknown'} and int(v.get('publication_cycle_id') or 0)==listing_release.cycle(listing)]
        stock_receipts=[v for v in s.all_rows(c,'availability_submission') if v['listing_id']==ident and v['status'] in {'sending','unknown'}]
    results=[]
    for receipt in receipts:
        raw=amazon.get_listing(target,receipt['seller_sku'])
        observed=raw['body'].get('attributes',{});expected=receipt['request']['attributes']
        if not all(observed.get(k)==v for k,v in expected.items()):
            raise amazon.PlatformError('平台返回属性尚不能确认本次版本，保留待核实状态',0,unknown=True)
        with s.transaction() as c:
            receipt['status']='accepted';receipt['response']=raw;s.save(c,'submission',receipt);results.append(receipt['seller_sku'])
            current=s.get(c,'listing',ident)
            current.setdefault('published_skus',{})[receipt['seller_sku']]=receipt['version']
            s.save(c,'listing',current)
            # Re-enter the same immutable submission; accepted SKU receipts are skipped.
            c.execute("UPDATE commerce_jobs SET status='queued',error='',next_at=0 WHERE kind='submit' AND status='unknown' AND json_extract(payload,'$.listing_id')=? AND json_extract(payload,'$.version')=?",(ident,receipt['version']))
    for receipt in stock_receipts:
        with s.main().db() as c:
            current=s.get(c,'listing',ident);target=s.get(c,'target',current['target_id'])
            material=s.by_key(c,'material_version',f"{ident}:{receipt.get('published_version')}")
            if not material:raise HTTPException(409,'旧库存回执未冻结已发布版本，请先人工核对；原记录保留')
            listing_identity.assert_published_target(current,target,material)
            if receipt.get('target_scope')!=listing_identity.target_scope(target):
                raise HTTPException(409,'库存回执的已发布店铺作用域已变化，请先人工核对')
        raw=amazon.get_listing(target,receipt['seller_sku'])
        availability=raw['body'].get('fulfillmentAvailability',[]) or raw['body'].get('attributes',{}).get('fulfillment_availability',[])
        observed=next((v.get('quantity') for v in availability if v.get('fulfillmentChannelCode',v.get('fulfillment_channel_code'))=='DEFAULT'),None)
        if observed!=receipt['quantity']:raise amazon.PlatformError('平台库存尚不能确认本次修改，保留待核实状态',0,unknown=True)
        with s.transaction() as c:
            current=s.get(c,'listing',ident);current_target=s.get(c,'target',current['target_id'])
            listing_identity.assert_published_target(current,current_target,material)
            if receipt.get('target_scope')!=listing_identity.target_scope(current_target):
                raise HTTPException(409,'核对期间库存回执的店铺作用域已变化')
            receipt['status']='accepted';receipt['response']=raw;s.save(c,'availability_submission',receipt)
            c.execute("UPDATE commerce_jobs SET status='queued',error='',next_at=0 WHERE id=? AND status='unknown'",(receipt['job_id'],))
        results.append(receipt['seller_sku'])
    return {'reconciled':results}


def sync(job):
    from .environment import require
    with s.main().db() as c:target=s.get(c,'target',job['payload']['target_id'])
    require(target['environment'])
    kind=job['payload'].get('scope','connect')
    if target['environment']=='mock' and kind!='orders':return {'environment':'mock','message':'模拟环境无需平台网络连接'}
    if kind=='sandbox' or target['environment']=='sandbox':raise HTTPException(422,'沙箱自检已移除，请配置真实授权店铺')
    if kind=='connect':
        result=amazon.connect(target)
        available={x.get('marketplace',{}).get('id') for x in result['marketplaces'] if x.get('participation',{}).get('isParticipating')}
        with s.transaction() as c:
            latest=s.get(c,'target',target['id']);latest['connection']=result;latest['verified']=target['marketplace_id'] in available;latest['last_sync_at']=time.time();s.save(c,'target',latest)
        return result
    if kind=='definition':
        ptype=job['payload'].get('product_type','');result=amazon.definition(target,ptype)
        with s.transaction() as c:s.put(c,'definition',f"{target['id']}:{ptype}:NONE",result|{'target_id':target['id']})
        return {'product_type':ptype,'fetched_at':result['fetched_at']}
    if kind=='orders':
        from . import order_logistics
        return order_logistics.sync(target)
    if kind=='finance':
        rows=amazon.transactions(target,target.get('finance_since') or (datetime.now(timezone.utc)-timedelta(days=7)).isoformat())
        with s.transaction() as c:
            for row in rows:
                key=str(target['account_id'])+':'+target['environment']+':'+str(row.get('transactionId',''))
                if not row.get('transactionId'):continue
                old=s.by_key(c,'transaction',key) or {}
                reconciled=old.get('status')=='reconciled' and old.get('raw')==row
                s.put(c,'transaction',key,old|{'target_id':target['id'],'environment':target['environment'],'external_id':row['transactionId'],'raw':row,'status':'reconciled' if reconciled else 'unreconciled'})
        return {'transactions':len(rows),'message':'平台交易已同步，待核对费用分类'}
    raise HTTPException(422,'未知同步类别')


def shipment(job):
    ident=job['payload']['shipment_id']
    with s.main().db() as c:offline=s.get(c,'shipment',ident).get('fulfillment_mode')=='offline'
    if offline:
        from . import order_logistics
        try:return order_logistics.submit(ident)
        except Exception as exc:
            with s.transaction() as c:
                current=s.get(c,'shipment',ident)
                if current['status'] in {'draft','queued'}:
                    current.update(status='failed',last_error=str(exc.detail) if isinstance(exc,HTTPException) else str(exc));s.save(c,'shipment',current)
            raise
    with s.transaction() as c:
        ship=s.get(c,'shipment',ident);target=s.get(c,'target',ship['target_id']);order=s.get(c,'order',ship['order_id'])
        if ship['status']=='confirmed':return ship
        if ship['status'] in {'sending','unknown'}:raise amazon.PlatformError('发货结果待核实，请先检查平台包裹',0,unknown=True)
        settings=s.by_key(c,'settings','global') or {}
        if target['environment']=='production' and not settings.get('live_writes_enabled'):raise HTTPException(422,'正式写入未启用')
        if order['platform_status'] not in ({'ready','shipped'} if ship.get('inventory_applied') else {'ready'}) or order.get('sync_issues'):raise HTTPException(409,'平台订单当前不可发货，请先核对差异')
        ship['status']='sending';s.save(c,'shipment',ship)
    package={'packageReferenceId':ship['platform_package_id'],'carrierName':ship['carrier'],'trackingNumber':ship['tracking'],'shipDate':ship['ship_date'],
      'orderItems':[{'orderItemId':x['item_id'],'quantity':x['quantity']} for x in ship['items']]}
    if ship.get('service'):package['shippingMethod']=ship['service']
    try:
        if order.get('fulfillment_mode')=='manual_replacement':
            return operations.finalize_shipment(ident,{'source':'manual_replacement','message':'补发物流已登记，需在平台售后记录对应物流；未向原订单重复回填'})
        r={'http_status':204,'request_id':'MOCK','body':{}} if target['environment']=='mock' else amazon.confirm_shipment(target,ship['external_order_id'],package)
        return operations.finalize_shipment(ident,r)
    except amazon.PlatformError as exc:
        with s.transaction() as c:
            current=s.get(c,'shipment',ident);current['status']='unknown' if exc.unknown else 'failed';current['last_error']=str(exc);s.save(c,'shipment',current)
        raise


def availability(job):
    from . import listing_identity,listing_release
    ident=job['payload']['listing_id'];action=job['payload']['action']
    if action not in {'pause','resume','sync'}:raise HTTPException(422,'库存操作无效')
    def current_context(c,material):
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        listing_release.assert_job(listing,job['payload'],action)
        listing_identity.assert_published_target(listing,target,material)
        if not target.get('enabled') and action!='pause':raise HTTPException(409,'上架目标已停用')
        if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):raise HTTPException(422,'正式写入未启用')
        return listing,target
    with s.main().db() as c:
        listing=s.get(c,'listing',ident)
        listing_release.assert_job(listing,job['payload'],action)
        version=listing.get('published_version')
        if 'published_version' in job['payload'] and job['payload']['published_version']!=version:
            raise HTTPException(409,'排队期间已发布版本已变化，请重新核对库存任务')
        material=s.by_key(c,'material_version',f'{ident}:{version}') if version else None
        if not material:raise HTTPException(422,'尚无已提交的商品版本')
        listing,target=current_context(c,material)
    if action=='pause':
        with s.transaction() as c:
            current,target=current_context(c,material)
            current['paused']=True;current['status']='pause_pending';s.save(c,'listing',current)
    states={}
    for sku in material['draft']['skus']:
        key=f"{job['id']}:{sku['sku_id']}"
        with s.transaction() as c:
            current,target=current_context(c,material)
            receipt=s.by_key(c,'availability_submission',key)
            if receipt and (receipt.get('published_version')!=version or
                            receipt.get('target_scope')!=listing_identity.target_scope(target) or
                            receipt.get('action')!=action):
                raise HTTPException(409,'库存任务回执未对应当前已发布店铺及版本，请先核对历史回执')
            if receipt and receipt['status']=='accepted':states[sku['seller_sku']]=receipt['response'];continue
            if receipt and receipt['status'] in {'unknown','sending'}:raise amazon.PlatformError('库存更新结果待核实，请使用核对未知提交',0,unknown=True)
            available=s.sales_capacity(c,sku['sku_id'],target['environment'])
            other=sum(int(x.get('quantity',0)) for x in s.all_rows(c,'allocation') if x.get('policy')!='amazon_max' and x['sku_id']==sku['sku_id'] and x['listing_id']!=ident and x.get('environment','production')==target['environment'])
            count=0 if action=='pause' or (action=='sync' and current.get('paused')) else max(0,available-other)
            from . import offer_defaults
            configured=offer_defaults.available(c,current,sku,target,material['draft'])
            if configured is not None and action!='pause' and not (action=='sync' and current.get('paused')):count=configured
            old=offer_defaults.claim(c,current,sku,target,material['draft'],count)
            s.put(c,'availability_submission',key,{'listing_id':ident,'job_id':job['id'],'seller_sku':sku['seller_sku'],'quantity':count,'status':'sending','previous_allocation':old,
                  'published_version':version,'target_scope':listing_identity.target_scope(target),'action':action})
        try:
            r={'body':{'status':'ACCEPTED'},'request_id':'MOCK'} if target['environment']=='mock' else amazon.patch_listing(target,sku['seller_sku'],material['draft']['product_type'],[{'op':'replace','path':'/attributes/fulfillment_availability','value':[{'fulfillment_channel_code':'DEFAULT','quantity':count,'lead_time_to_ship_max_days':material['draft']['handling_days']}]}])
            if listing_response_status(r)!='ACCEPTED':raise amazon.PlatformError('平台拒绝库存更新，请查看商品问题',422)
        except amazon.PlatformError as exc:
            with s.transaction() as c:
                receipt=s.by_key(c,'availability_submission',key);receipt.update(status='unknown' if exc.unknown else 'failed',error=str(exc));s.save(c,'availability_submission',receipt)
                if not exc.unknown:restore_allocation(c,ident,sku,target,old)
            raise
        states[sku['seller_sku']]=r
        with s.transaction() as c:
            receipt=s.by_key(c,'availability_submission',key);receipt.update(status='accepted',response=r);s.save(c,'availability_submission',receipt)
    with s.transaction() as c:
        current,_=current_context(c,material)
        if action!='sync':current['paused']=action=='pause'
        current['status']='paused' if current.get('paused') else 'processing';current['availability_result']=states;current['stock_synced_at']=time.time();s.save(c,'listing',current)
    return states


HANDLERS={'aftersale_sync':aftersales.sync,'aftersale_submit':aftersales.submit,'aftersale_check':aftersales.check,'aftersale_import':aftersales_reports.import_report,
          **support.HANDLERS,
          'detail_submit':detail_publication.submit,'detail_check':detail_publication.check,
          'business_mapping':business_mapping.run,'materials':catalog.generate_materials,'main_image':main_images.run_job,'submit':submit,'refresh':refresh,'sync':sync,'shipment':shipment,'availability':availability,
          'reconcile_shipment':reconcile_shipment,'reconcile_listing':reconcile_listing,'report':reports.run,'reconcile_report':reports.reconcile_report,'report_discover':reports.discover}
from . import listing_workflow
HANDLERS['listing_workflow']=listing_workflow.run
from . import import_classification
HANDLERS['import_classification']=import_classification.run
from . import attribute_completion
HANDLERS['listing_fields']=attribute_completion.run


def tick():
    with s.transaction() as c:
        # After an interrupted mutation, handlers see their sending receipt and reconcile first.
        c.execute("UPDATE commerce_jobs SET status='queued' WHERE status='running' AND lease_until<?",(time.time(),))
        from . import execution_controls
        rows=c.execute("SELECT id,kind,payload FROM commerce_jobs WHERE status IN ('queued','retry_wait') AND next_at<=? ORDER BY id",(time.time(),))
        try:row=next((row for row in rows if execution_controls.job_allowed(c,{'id':row['id'],'kind':row['kind'],'payload':json.loads(row['payload'])})),None)
        finally:rows.close()
        if not row:return False
        ident=row[0];c.execute("UPDATE commerce_jobs SET status='running',attempts=attempts+1,role_attempts=role_attempts+1,lease_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(time.time()+180,ident))
        job=s.job(c,ident)
    context_reset = error_log.CONTEXT.set({'job_id':ident, 'job_kind':job['kind'], 'attempt':job['attempts'],
                                         **{k:v for k,v in job['payload'].items() if k in {'listing_id','target_id','publication_id','version'}}, 'logged':False})
    from app.model_retry import CURRENT_ROLE
    model_context=CURRENT_ROLE.set(job['model_role'])
    stop=threading.Event()
    def heartbeat():
        while not stop.wait(30):
            with s.main().db() as c:c.execute("UPDATE commerce_jobs SET lease_until=? WHERE id=? AND status='running'",(time.time()+180,ident))
    threading.Thread(target=heartbeat,daemon=True).start()
    try:
        from .environment import check_job
        with s.main().db() as c:check_job(c,job['payload'])
        result=HANDLERS[job['kind']](job)
        with s.transaction() as c:c.execute("UPDATE commerce_jobs SET status='completed',result=?,error='',lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",(s.dump(result),ident))
    except Exception as exc:
        status='paused';delay=0;role=job['model_role'];role_attempts=job['role_attempts']
        updated_result=None
        from app.model_retry import ModelFailure,decide
        if isinstance(exc,ModelFailure):
            from app import design_transport
            image_failure=job['kind']=='main_image' and getattr(exc,'model_stage','')=='image'
            try:model_count=len(design_transport.candidates(s.main(),image=image_failure))
            except Exception:model_count=0
            has_backup=model_count>1
            with s.main().db() as c:
                updated_result=copy.deepcopy((s.job(c,ident) or {}).get('result') or {})
            if image_failure:
                with s.main().db() as c:
                    updated_result=copy.deepcopy((s.job(c,ident) or {}).get('result') or {})
                image_role=int(updated_result.get('image_model_role',0))
                image_attempts=int(updated_result.get('image_role_attempts',0))+1
                decision=decide(image_role,image_attempts,has_backup,exc,model_count=model_count,
                    retry_round=updated_result.get('image_retry_round',1))
                updated_result.update(image_model_role=decision.role,
                                      image_role_attempts=decision.attempts,image_retry_round=decision.retry_round)
            else:
                decision=decide(role,role_attempts,has_backup,exc,model_count=model_count,
                    retry_round=updated_result.get('model_retry_round',1))
                role=decision.role;role_attempts=decision.attempts
                updated_result['model_retry_round']=decision.retry_round
            status=decision.status;delay=decision.delay
        elif isinstance(exc,amazon.PlatformError):
            if exc.unknown:status='unknown'
            elif job['kind']=='submit' and exc.preview_media_timeout and job['attempts']<5:
                # Only the non-mutating preview adapter sets this evidence.
                # Re-enter the full validation flow after a bounded cooldown.
                status='retry_wait';delay=max(exc.retry_after,min(300,60*2**(job['attempts']-1)))
            elif (exc.status in {408,429} or exc.status>=500) and job['attempts']<5:status='retry_wait';delay=max(exc.retry_after,min(300,5*2**job['attempts']))
            with s.main().db() as c:
                updated_result=copy.deepcopy((s.job(c,ident) or {}).get('result') or {})
            updated_result['platform_error']=error_log.sanitize({
                'http_status':exc.status,'request_id':exc.request_id,
                'errors':exc.errors,'result_unknown':exc.unknown,
                'non_mutating_media_timeout':exc.preview_media_timeout})
        error=str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,(ModelFailure,amazon.PlatformError)) else '内部处理失败：'+type(exc).__name__
        if isinstance(exc,amazon.PlatformError):
            details='；'.join(error_log.clean_text(row.get('message') or row.get('details') or '')
                             for row in exc.errors[:5])
            if details:error+='：'+details[:1800]
            if exc.request_id:error+='（Amazon请求号 '+error_log.clean_text(exc.request_id)[:100]+'）'
        error_log.record('job.failed', exc, next_status=status, retry_delay_seconds=delay,
                         http_status=getattr(exc, 'status', getattr(exc, 'status_code', None)),
                         result_unknown=getattr(exc, 'unknown', False))
        if not isinstance(exc,(HTTPException,ModelFailure,amazon.PlatformError)):LOG.exception('Commerce job %s failed',ident)
        with s.transaction() as c:
            c.execute('UPDATE commerce_jobs SET status=?,error=?,next_at=?,model_role=?,role_attempts=?,result=COALESCE(?,result),lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                      (status,error[:2500],time.time()+delay,role,role_attempts,
                       s.dump(updated_result) if updated_result is not None else None,ident))
            operations.notify(c,'job:'+str(ident),'经营任务需处理：'+error[:200],ident)
    finally:
        stop.set()
        CURRENT_ROLE.reset(model_context)
        error_log.CONTEXT.reset(context_reset)
    return True


def start():
    global STARTED,WORKER_THREAD
    if STARTED:return
    STARTED=True
    def loop():
        last_schedule=0;last_workflow_schedule=0;last_retention=0
        while True:
            try:
                if time.time()-last_workflow_schedule>10:
                    with s.transaction() as c:listing_workflow.schedule(c)
                    last_workflow_schedule=time.time()
                if time.time()-last_schedule>60:
                    from .customer_pii import cleanup
                    cleanup()
                    with s.transaction() as c:
                        from . import credential_maintenance
                        credential_maintenance.schedule(c)
                        detail_publication.schedule(c)
                        support.schedule(c)
                        active_accounts={a[0] for a in c.execute('SELECT id FROM platform_accounts WHERE enabled=1')}
                        for target in s.all_rows(c,'target'):
                            if target['account_id'] in active_accounts and target.get('enabled') and target.get('auto_sync_orders') and target['environment']=='production':
                                pending=c.execute("SELECT 1 FROM commerce_jobs WHERE kind='sync' AND json_extract(payload,'$.target_id')=? AND json_extract(payload,'$.scope')='orders' AND status IN ('queued','running','retry_wait') LIMIT 1",(target['id'],)).fetchone()
                                if not pending:s.enqueue(c,'sync',f"auto-orders:{target['id']}:{int(time.time()//900)}",{'target_id':target['id'],'scope':'orders'})
                        for listing in s.all_rows(c,'listing'):
                            target=s.get(c,'target',listing['target_id'])
                            if not target.get('enabled') or not listing.get('published_version') or target['environment']!='production':continue
                            if time.time()-listing.get('checked_at',0)>900:
                                pending=c.execute("SELECT 1 FROM commerce_jobs WHERE kind='refresh' AND json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait') LIMIT 1",(listing['id'],)).fetchone()
                                if not pending:s.enqueue(c,'refresh',f"auto-refresh:{listing['id']}:{int(time.time()//900)}",{'listing_id':listing['id']})
                        for order in s.all_rows(c,'order'):
                            if order['status'] in {'shipped','cancelled'} or not order.get('deadline'):continue
                            try:due=datetime.fromisoformat(order['deadline'].replace('Z','+00:00')).timestamp()
                            except ValueError:continue
                            if due-time.time()<86400:operations.notify(c,'deadline:'+str(order['id']),'订单临近或超过发货期限：'+order['external_id'],order['id'])
                    last_schedule=time.time()
                    from .routes import evaluate_rules
                    evaluate_rules()
                if time.time()-last_retention>60:
                    from . import poll_retention
                    with s.transaction() as c:poll_retention.maintain(c)
                    last_retention=time.time()
                if not tick():time.sleep(2)
            except Exception:
                LOG.exception('Commerce worker');time.sleep(5)
    WORKER_THREAD=threading.Thread(target=loop,name='commerce-worker',daemon=True)
    WORKER_THREAD.start()
