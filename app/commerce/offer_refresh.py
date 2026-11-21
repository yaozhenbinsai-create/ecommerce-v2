"""Refresh local business inputs without rebuilding translated catalog facts."""
from __future__ import annotations

import copy

from fastapi import HTTPException

from . import catalog, costing, listing_identity, merchant_terms, offer_defaults, pricing, product_archive, shipping, store as s


def refresh(ident, payload):
    with s.transaction() as c:
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        if listing['revision']!=payload.get('revision'):
            raise HTTPException(409,'草稿已变化，请重新读取当前版本')
        if listing.get('published_version') is not None or listing.get('published_skus') or listing['status']!='not_submitted':
            raise HTTPException(409,'此操作只更新未上架草稿，不改动已发布商品')
        if c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(ident,)).fetchone():
            raise HTTPException(409,'草稿仍有活动任务，请先核对任务')
        current=listing['draft'];draft=copy.deepcopy(current)
        product=s.get(c,'product',listing['product_id'])
        selected=[row['sku_id'] for row in draft['skus']]
        digest=product_archive.fact_digest(c,product,selected)
        if not draft.get('source',{}).get('archive_digest') or digest!=draft['source']['archive_digest']:
            raise HTTPException(409,'实物档案已变化，不能仅刷新经营数据')
        listing_identity.assert_target_scope(target,draft)
        signature=listing_identity.archive_signature(c,product,target,selected)
        baseline_version=payload.get('projection_version')
        if baseline_version is not None:
            material=s.by_key(c,'material_version',f'{ident}:{baseline_version}')
            if (material or {}).get('_history_archived'):
                raise HTTPException(409,'旧版本已清理归档，请从当前工作版本调整')
            baseline=(material or {}).get('draft') or {}
            if (baseline.get('source',{}).get('archive_digest')!=digest
                    or (baseline.get('source',{}).get('archive_signature')!=signature
                        and not listing_identity.historical_signature_matches(c,product,target,selected,baseline.get('source',{})))
                    or baseline.get('source',{}).get('target_scope')!=draft.get('source',{}).get('target_scope')
                    or any(baseline.get(key)!=draft.get(key) for key in ['product_type','language','content','images','variation_theme','parent_sku'])):
                raise HTTPException(409,'历史属性投影与当前实物、店铺或内容不一致，不会覆盖')
            prior={row['sku_id']:row for row in baseline.get('skus',[])}
            if set(prior)!=set(selected) or any(prior[row['sku_id']]['seller_sku']!=row['seller_sku'] for row in draft['skus']):
                raise HTTPException(409,'历史规格身份与当前草稿不一致')
            for field in ['attributes','parent_attributes']:
                if field in baseline:draft[field]=copy.deepcopy(baseline[field])
                else:draft.pop(field,None)
            for row in draft['skus']:
                if 'attributes' in prior[row['sku_id']]:row['attributes']=copy.deepcopy(prior[row['sku_id']]['attributes'])
                else:row.pop('attributes',None)
            for field in ['attribute_localization','attribute_completion','attribute_fallbacks']:
                if field in baseline.get('source',{}):draft['source'][field]=copy.deepcopy(baseline['source'][field])
                else:draft['source'].pop(field,None)
        archive=product_archive.selected_skus(c,product,selected)
        source=draft.setdefault('source',{})
        source.update(skus=archive,product_revision=product['revision'],product_fact_revision=product.get('fact_revision',0),
                      archive_signature=signature)
        source['offer_data_refresh']={'policy':'business-only-v1','pricing_rule':costing.sale_rule(product,c),
                                      'projection_material_version':baseline_version}
        if payload.get('refresh_prices'):
            for row in draft['skus']:
                if (row.get('price_source') or {}).get('status')=='manual':continue
                result=pricing.quote(s.get(c,'sku',row['sku_id']),draft['currency'],connection=c)
                if result['price'] is None:raise HTTPException(422,result['price_source']['message'])
                row.update(result)
        if payload.get('maximum_handling'):
            days,basis=offer_defaults.handling_default(c,target,draft['product_type'])
            if days is None:raise HTTPException(422,basis['message'])
            if draft.get('handling_days')!=days:draft.update(handling_days=days,handling_source=basis)
        if payload.get('shipping_group'):draft['shipping_group']=payload['shipping_group']
        if payload.get('price_shipping_mode'):draft['price_shipping_mode']=payload['price_shipping_mode']
        shipping.validate_selected(target,draft['product_type'],draft.get('shipping_group'))
        merchant_terms.validate_submission(c,target,draft)
        if draft==current:return listing
        listing.update(draft=draft,draft_version=listing['draft_version']+1)
        listing['issues']=catalog.issues(draft,target,connection=c)
        listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
        listing.pop('preview_results',None)
        s.put(c,'material_version',f"{ident}:{listing['draft_version']}",
              {'listing_id':ident,'version':listing['draft_version'],'draft':draft,'change_reason':'business_data_refresh_preserving_catalog_projection'})
        s.audit(c,'listing_offer_data_refreshed',ident,s.dump({'projection_version':baseline_version,'refresh_prices':bool(payload.get('refresh_prices'))}))
        return s.save(c,'listing',listing)
