"""Persist Amazon's observed SKU-to-ASIN identity without rewriting a draft."""

from __future__ import annotations



import hashlib

import json

import re

import time



from fastapi import HTTPException



from . import store as s





ASIN = re.compile(r"[A-Z0-9]{10}\Z")





def target_scope(target):

    """Freeze account/marketplace identity independently of merchandising values."""

    return {'target_id': target.get('id'), **{key: target.get(key) for key in

            ('account_id', 'seller_id', 'marketplace_id', 'environment', 'region')}}





def assert_target_scope(target, draft):

    """Use frozen identity for mutations, independently of current product facts."""

    frozen = (draft.get('source') or {}).get('target_scope')

    if target.get('environment') == 'production' and frozen is None:

        raise HTTPException(409, '旧刊登资料未冻结店铺作用域，请先核对已发布版本；历史资料仍保留')

    if frozen is not None and frozen != target_scope(target):

        raise HTTPException(409, '刊登资料的账号、卖家、站点或环境已变化，请核对已发布店铺')





def assert_published_target(listing, target, material):

    """Inventory updates belong to the published version, not the editable draft."""

    if (listing.get('target_id') != target.get('id') or

            material.get('listing_id') != listing.get('id') or

            material.get('version') != listing.get('published_version')):

        raise HTTPException(409, '已发布商品版本或店铺已变化，请重新核对库存任务')

    assert_target_scope(target, material.get('draft') or {})





_OFFER_FIELD = re.compile(r'price|cost|currency|stock|inventory|采购价|成本|供货价|售价|销售价|单价|报价|价格|币种|库存', re.I)

_FACT_FIELDS = {'id', 'product_id', 'business_id', 'name', 'brand', 'brand_status', 'brand_logo_text',

                'facts', 'structured_facts', 'normalized_fields', 'source_fields', 'source_conflicts',

                'source_parse_issues', 'components', 'gtin', 'source_gtin', 'gtin_issue', 'asin', 'source_asin', 'asin_issue',

                'origin', 'manufacturer', 'model_name', 'model_number', 'country_of_origin',

                'supplier', 'supplier_sku', 'sku', 'variant_key', 'local_variant_id', 'color', 'size',

                'material', 'fill_material', 'package_quantity', 'item_shape', 'pattern',

                'item_weight', 'package_weight', 'product_type', 'category_id', 'attributes'}

_FACT_METADATA = {'revision', 'fact_revision', 'created_at', 'updated_at', 'checked_at', 'fetched_at',

                  'registered_at', 'source', 'actor', 'import_id', 'sheet', 'row', 'source_row',

                  'timestamp', 'production_days', 'packing_days', 'handling_days', 'sale_mode',

                  'production_capacity', 'on_hand', 'reserved', 'safety', 'quantity_source'}





def _fact_value(value):

    """Canonical semantic facts; commercial values and provenance clocks are independent."""

    if isinstance(value, dict):

        return {key: _fact_value(item) for key, item in value.items()

                if key not in _FACT_METADATA and not _OFFER_FIELD.search(str(key))}

    if isinstance(value, list):

        return [_fact_value(item) for item in value]

    return value





def _archive_record(record, target, product_type):

    from . import archive_fields

    facts = {key: _fact_value(value) for key, value in record.items() if key in _FACT_FIELDS}

    for key in ('source_conflicts', 'source_parse_issues'):

        if key in facts:

            facts[key] = [item for item in facts[key] if not isinstance(item, dict) or

                          not _OFFER_FIELD.search(str(item.get('field', '')))]

    if 'facts' in facts:

        facts['facts'] = '\n'.join(line.strip() for line in str(facts['facts'] or '').splitlines()

                                 if not _OFFER_FIELD.search(re.split(r'[:：]', line, maxsplit=1)[0]))

    # Other accounts' attributes never certify or invalidate this account's facts.

    saved = (record.get('platform_attributes') or {}).get(archive_fields.scope_key(target, product_type)) or {}

    if saved.get('scope') == archive_fields.scope(target, product_type):

        attributes = _fact_value(saved.get('attributes') or {})

        cleared = sorted(key for key in (saved.get('cleared_fields') or []) if not _OFFER_FIELD.search(key))

        facts['platform_attributes'] = {'attributes': attributes, 'cleared_fields': cleared} if attributes or cleared else {}

    else:

        facts['platform_attributes'] = {}

    return {key: value for key, value in facts.items() if value not in (None, '', {}, [])}





def archive_signature(c, product, target, selected, *, historical_projection=None, historical_localization=None):

    """Freeze all listing facts for one seller scope, separately from visual facts."""

    from . import categories, category_rules, product_archive, supplier_declarations

    found = categories.resolve(c, product, target)

    mapping = found.get('mapping') or {}

    product_type = mapping.get('product_type') or product.get('product_type') or ''

    classification = {'product_type': product_type,

                      'attributes': _fact_value(mapping.get('attributes') or {}),

                      'selected_attributes': category_rules.attributes(mapping,target),

                      'selected_values': category_rules.selected_for(mapping,target)} if mapping else {}

    payload = {'scope': target_scope(target), 'product_type': product_type,

               'product': _archive_record(product, target, product_type),

               'skus': [_archive_record(sku, target, product_type)

                        for sku in product_archive.selected_skus(c, product, selected)],

               'classification': classification}

    from . import attribute_localization,locales

    payload['attribute_localization']=historical_localization or attribute_localization.identity(locales.resolve(target))

    # Decode a recorded historical fingerprint only. The retired projection
    # cannot run or produce new values through this compatibility reader.
    if historical_projection:payload['size_presentation']=historical_projection

    declarations=supplier_declarations.for_product(product)

    if declarations:payload['supplier_declarations']=declarations

    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,

                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def historical_signature_matches(c, product, target, selected, source):
    proofs=source.get('size_presentation')
    protocol='explicit-dimensions-without-domestic-fit-note-v1'
    from . import locales
    language=locales.resolve(target)
    projections=[None]
    if isinstance(proofs,dict) and proofs and all(isinstance(proof,dict) and proof.get('protocol')==protocol for proof in proofs.values()):
        projections.append({'protocol':protocol,'language':language,
            'implementation_sha256':'8fd0bffdb2182f8c4721eddb106c163db606e81ebea69ce2bdf8d603ee5c889b'})
    localizations=[None]
    for proof in (source.get('attribute_localization') or {}).values():
        recorded=proof.get('identity') if isinstance(proof,dict) else None
        if (isinstance(recorded,dict) and recorded.get('language')==language
                and recorded.get('protocol')=='source-destination-attributes-v2'
                and recorded.get('enabled') is True
                and re.fullmatch('[a-f0-9]{64}',str(recorded.get('implementation_sha256') or ''))
                and recorded not in localizations):localizations.append(recorded)
    # Match the complete current fact payload plus a recorded old fingerprint;
    # neither a matching code version nor an old translation can excuse changed facts.
    return any(source.get('archive_signature')==archive_signature(c,product,target,selected,
        historical_projection=projection,historical_localization=localization)
        for projection in projections for localization in localizations if projection or localization)





def assert_source_current(c, listing, target, draft):

    """Reject stale facts/designs in every submit path while retaining old assets."""

    source = draft.get('source') or {}

    product = s.get(c, 'product', listing['product_id'])

    assert_target_scope(target, draft)

    selected = source.get('selected_sku_ids') or [row.get('id') for row in source.get('skus', [])]

    actual_ids = [row.get('sku_id') for row in draft.get('skus', [])]

    if (not selected or len(set(selected)) != len(selected) or

            set(selected) != set(actual_ids)):

        raise HTTPException(409, '刊登规格与冻结的设计/商品规格不一致，请重新组装资料')

    active = {row['id']: row for row in s.all_rows(c, 'sku')

              if row.get('product_id') == product['id'] and row.get('active', True)}

    if any(ident not in active for ident in selected):

        raise HTTPException(409, '刊登引用的实际 SKU 已停用或不属于此商品，请重新组装资料')

    signature = source.get('archive_signature')

    if signature:

        if (signature != archive_signature(c, product, target, selected) and
                not historical_signature_matches(c,product,target,selected,source)):

            raise HTTPException(409, '当前店铺的商品档案或 SKU 刊登事实已变化，请重新组装资料')

    elif target.get('environment') == 'production':

        raise HTTPException(409, '旧资料没有当前店铺的完整刊登事实核验版本，请重新组装资料；历史资料仍保留')

    if (not signature and 'product_fact_revision' in source and

            (source['product_fact_revision'] or 0) != (product.get('fact_revision') or 0)):

        raise HTTPException(409, '商品事实字段已变化，请重新组装刊登资料')

    frozen_skus = {row.get('id'): row for row in source.get('skus', [])}

    if not signature and any('fact_revision' in frozen_skus.get(ident, {}) and

           (frozen_skus[ident]['fact_revision'] or 0) != (active[ident].get('fact_revision') or 0)

           for ident in selected):

        raise HTTPException(409, '实际 SKU 事实字段已变化，请重新组装刊登资料')

    digest = source.get('archive_digest')

    if digest:

        from . import product_archive

        if digest != product_archive.fact_digest(c, product, selected):

            raise HTTPException(409, '商品档案或所选规格事实已变化，请核验受影响设计并重新组装资料')

    elif target.get('environment') == 'production':

        raise HTTPException(409, '旧资料没有商品事实核验版本，请按新版档案重新核验；历史图与资料仍保留')

    else:

        # Compatibility for historical isolated/mock records. Production never

        # relies on a generic revision to certify factual design integrity.

        if source.get('product_revision') != product['revision']:

            raise HTTPException(409, '商品档案版本已变化，请重新组装资料')

        frozen = {row.get('id'): row.get('revision') for row in source.get('skus', [])}

        if any(frozen.get(ident) != active[ident]['revision'] for ident in selected):

            raise HTTPException(409, '实际 SKU 版本已变化，请重新组装资料')

    project = c.execute('SELECT simple_current_run FROM design_projects WHERE id=?',

                        (product.get('project_id'),)).fetchone()

    run_id = source.get('design_run_id')

    run = c.execute('SELECT project_id,status,snapshot FROM design_simple_runs WHERE id=?',

                    (run_id,)).fetchone() if run_id else None

    if (not project or project['simple_current_run'] != run_id or not run or

            run['project_id'] != product.get('project_id') or run['status'] != 'completed'):

        raise HTTPException(409, '刊登引用的设计版本不是当前已完成版本，请重新核验设计')

    if digest:

        snapshot = json.loads(run['snapshot'] or '{}')

        if (snapshot.get('archive_digest') != digest or

                set(snapshot.get('selected_sku_ids') or []) != set(selected)):

            raise HTTPException(409, '设计事实与商品档案版本不一致，请核验新版设计后组装资料')

    return product





def submission_intent(c, listing, sku):

    """A read-back ASIN must not turn a self-created listing into an offer."""

    published = (listing.get('published_skus') or {}).get(sku.get('seller_sku'))

    if published is None:

        published = listing.get('published_version')

    if published is not None:

        material = s.by_key(c, 'material_version', f"{listing['id']}:{published}")

        prior = next((row for row in (material or {}).get('draft', {}).get('skus', [])

                      if row.get('sku_id') == sku.get('sku_id') and

                      row.get('seller_sku') == sku.get('seller_sku')), None)

        if prior and (not prior.get('asin') or prior.get('submission_intent') == 'own_product_update'):

            return 'own_product_update'

    return 'offer_only' if sku.get('asin') else 'create_product'





def offer_only(sku):

    return bool(sku.get('asin') and sku.get('submission_intent') != 'own_product_update')


def pin_owned_update(listing, target, sku):
    """Bind an edit to its freshly verified existing ASIN without changing source identities."""
    from copy import deepcopy
    from . import amazon
    if target.get('environment') != 'production' or sku.get('submission_intent') != 'own_product_update':
        return deepcopy(sku)
    with s.main().db() as c:
        accepted=s.by_key(c,'submission',f"{listing['id']}:{listing.get('draft_version')}:{sku['seller_sku']}:False")
    from . import listing_release
    if accepted and accepted.get('status')=='accepted' and int(accepted.get('publication_cycle_id') or 0)==listing_release.cycle(listing):
        result=deepcopy(sku)
        attributes=(accepted.get('request') or {}).get('attributes') or {}
        recorded=attributes.get('merchant_suggested_asin') or []
        if recorded and ASIN.fullmatch(str(recorded[0].get('value') or '')):result['asin']=recorded[0]['value']
        # An accepted same-version mutation is reconciled, never submitted twice.
        # A new edit/version must still freshly confirm its original identity.
        return result
    with s.main().db() as c:
        old = s.by_key(c, 'listing_identity', listing_release.identity_key(target,sku['seller_sku'],listing))
        if (not old or old.get('status') != 'confirmed' or old.get('listing_id') != listing['id']
                or old.get('sku_id') != sku['sku_id']):
            raise HTTPException(409, '更新原商品前请先同步平台编号；不会重新创建商品')
    response = amazon.get_listing(target, sku['seller_sku'])
    if response.get('http_status') != 200 or not response.get('request_id'):
        raise HTTPException(409, '平台未返回可核实的原商品身份，请稍后同步；不会重新创建')
    with s.main().db() as c:
        confirmed = record(c, target, listing, sku, response.get('body') or {}, response['request_id'])
    if confirmed.get('status') != 'confirmed' or confirmed.get('stale') or not confirmed.get('asin'):
        raise HTTPException(409, confirmed.get('message') or '原商品编号尚未确认，请先核对平台状态')
    value = deepcopy(sku)
    value['asin'] = confirmed['asin']
    return value





def apply_catalog_matches(c, listing, target, proposed):

    """Version verified catalog observations without reopening factual draft edits."""

    import copy

    from . import catalog

    current = s.get(c, 'listing', listing['id'])

    if current['revision'] != listing['revision']:

        raise HTTPException(409, '目录核验期间刊登资料已变化，请重新核验')

    if target.get('environment') != 'production':

        raise HTTPException(422, '真实目录身份只能来自正式店铺只读核验')

    live_target = s.get(c, 'target', current['target_id'])

    if target_scope(live_target) != target_scope(target):

        raise HTTPException(409, '目录核验期间店铺作用域已变化')

    draft = copy.deepcopy(current.get('draft') or {})

    assert_source_current(c, current, target, draft)

    entries = {row['sku_id']: row for row in draft.get('skus', [])}

    changed = False

    for sku_id, asin, gtin, request_id in proposed:

        entry = entries.get(sku_id)

        if not entry or not ASIN.fullmatch(asin):

            raise HTTPException(422, '目录身份未对应当前资料的实际 SKU')

        actual = s.get(c, 'sku', sku_id)

        if gtin and (entry.get('gtin') != gtin or actual.get('gtin') != gtin):

            raise HTTPException(409, '目录核验条码与商品档案不一致')

        if entry.get('asin') and entry['asin'] != asin:

            raise HTTPException(409, '目录核验 ASIN 与资料的已有身份不一致')

        if not entry.get('asin') and not gtin:

            raise HTTPException(422, '无条码的新货不能直接认定为目录商品')

        s.put(c, 'catalog_match', f"{target['id']}:{sku_id}:{gtin or asin}",

              {'target_id': target['id'], 'target_scope': target_scope(target), 'sku_id': sku_id,

               'gtin': gtin, 'asin': asin, 'request_id': request_id, 'checked_at': time.time(),

               'source': 'catalog_items_api', 'verification': 'brand_variant_identifier'})

        if entry.get('asin') != asin:

            entry['asin'] = asin

            entry['submission_intent'] = submission_intent(c, current, entry)

            changed = True

    if not changed:

        return current

    current['draft'] = draft

    current['draft_version'] += 1

    current['issues'] = catalog.issues(draft, target)

    current['draft_status'] = 'needs_fields' if current['issues'] else 'ready'

    s.put(c, 'material_version', f"{current['id']}:{current['draft_version']}",

          {'listing_id': current['id'], 'version': current['draft_version'], 'draft': draft})

    s.audit(c, 'catalog_identity_matched', current['id'], '可信目录匹配，保留商品档案原始事实及历史版本')

    return s.save(c, 'listing', current)





def observed_asin(body: dict, marketplace_id: str, seller_sku: str) -> tuple[str, str]:

    """Return (ASIN, error) from the exact listing/marketplace response."""

    if not isinstance(body, dict):

        return "", "平台商品响应格式无效"

    if body.get("sku") and body["sku"] != seller_sku:

        return "", "平台返回了不同的 SKU"

    summaries = body.get("summaries") or []

    if not isinstance(summaries, list):

        return "", "平台商品摘要格式无效"

    matching = [row for row in summaries if isinstance(row, dict) and row.get("marketplaceId") == marketplace_id]

    values = {str(row.get("asin", "")) for row in matching if row.get("asin")}

    if any(not ASIN.fullmatch(value) for value in values):

        return "", "平台返回的 ASIN 格式无效"

    if len(values) > 1:

        return "", "平台为同一站点返回了多个 ASIN"

    return (next(iter(values)), "") if values else ("", "")





def record(c, target: dict, listing: dict, sku: dict, body: dict, request_id: str = "") -> dict:

    """Store only a verified production observation; surface identity conflicts."""

    if target.get("environment") != "production":

        return {"status": "not_applicable", "asin": ""}

    asin, error = observed_asin(body, target["marketplace_id"], sku["seller_sku"])

    if error:

        return {"status": "identity_conflict", "asin": "", "message": error}

    from . import listing_release
    key = listing_release.identity_key(target,sku['seller_sku'],listing)

    old = s.by_key(c, "listing_identity", key)

    if not asin:

        if old and old.get("status") == "confirmed":

            return {"status": "confirmed", "asin": old["asin"], "stale": True}

        return {"status": "pending", "asin": ""}

    expected = str(sku.get("asin") or "")

    if expected and expected != asin:

        return {"status": "identity_conflict", "asin": asin, "message": "平台 ASIN 与资料填写的已有 ASIN 不一致"}

    if old:

        if any(old.get(field) != target.get(source) for field, source in

               (("account_id", "account_id"), ("seller_id", "seller_id"), ("marketplace_id", "marketplace_id"))):

            return {"status": "identity_conflict", "asin": asin, "message": "SKU 身份记录的店铺作用域不一致"}

        if old.get("asin") != asin:
            from .listing_identity_review import resolved
            resolution = resolved(c, target, listing, sku, old, asin)
            if resolution:
                return {"status": "confirmed", "asin": asin, "record_id": old["id"],
                        "resolution_id": resolution["id"]}
            return {"status": "identity_conflict", "asin": asin, "confirmed_asin": old.get("asin", ""),

                    "message": "平台 ASIN 与此前确认的 SKU 身份不一致"}

        return {"status": "confirmed", "asin": asin, "record_id": old["id"]}

    saved = s.put(c, "listing_identity", key, {

        "target_id": target["id"], "account_id": target["account_id"], "seller_id": target["seller_id"],

        "marketplace_id": target["marketplace_id"], "seller_sku": sku["seller_sku"],

        "sku_id": sku["sku_id"], "listing_id": listing["id"], "asin": asin,

        "status": "confirmed", "source": "getListingsItem", "platform_request_id": request_id,

        "observed_at": time.time(),

    })

    s.audit(c, "amazon_asin_observed", listing["id"], f"{sku['seller_sku']}:{asin}")

    return {"status": "confirmed", "asin": asin, "record_id": saved["id"]}

