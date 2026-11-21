"""Source-backed, seller-scoped official fields and real variation assembly."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from . import store as s, archive_fields, amazon, locales, listing_issues

POLICY = 'source-official-attributes-v5'
READY_POLICIES = {POLICY, 'source-official-attributes-v4'}


def signature(draft):
    source = draft.get('source') or {}
    data = {key: source.get(key) for key in ('archive_signature', 'archive_digest', 'target_scope')}
    data.update(product_type=draft.get('product_type'), language=draft.get('language'),
                parent_attributes=draft.get('parent_attributes'), variation_theme=draft.get('variation_theme'),
                skus=[{key: row.get(key) for key in ('sku_id', 'color', 'size', 'attributes', 'variation_attributes')}
                      for row in draft.get('skus', [])])
    return hashlib.sha256(s.dump(data).encode()).hexdigest()


def current(draft, listing=None):
    proof = draft.get('source', {}).get('attribute_completion') or {}
    requested = listing_issues.completion_scope(listing) if listing is not None else {}
    covered = proof.get('platform_missing') or {}
    return (proof.get('policy') in READY_POLICIES and proof.get('status') == 'ready'
            and proof.get('signature') == signature(draft)
            and (listing is None or all(set(names) <= set(covered.get(sku, [])) for sku, names in requested.items()))
            and time.time() - proof.get('checked_epoch', 0) < 86400)


def ensure(c, listing):
    target = s.get(c, 'target', listing['target_id'])
    if target.get('environment') != 'production' or not listing.get('draft') or current(listing['draft'], listing):
        return None
    proof=listing['draft'].get('source',{}).get('attribute_completion') or {}
    existing = c.execute("""SELECT id FROM commerce_jobs WHERE kind='listing_fields'
        AND json_extract(payload,'$.listing_id')=?
        AND (status='unknown' OR (json_extract(payload,'$.version')=?
            AND status IN ('queued','running','retry_wait')))
        ORDER BY (status='unknown') DESC,id DESC LIMIT 1""",
        (listing['id'], listing['draft_version'])).fetchone()
    if existing:return s.job(c, existing[0])
    if (proof.get('policy') == POLICY and proof.get('status')=='needs_fields'
            and proof.get('signature')==signature(listing['draft'])):
        pending=s.job(c,proof['job_id']) if proof.get('job_id') else None
        if pending:return pending
        raise HTTPException(422,'仍有真实资料缺项，请先补充来源后继续：'+s.dump(proof.get('remaining',{})))
    scope_hash = hashlib.sha256(s.dump(listing_issues.completion_scope(listing)).encode()).hexdigest()[:16]
    return s.enqueue(c, 'listing_fields', f"listing-fields:{listing['id']}:{listing['draft_version']}:{POLICY}:{scope_hash}",
                     {'listing_id': listing['id'], 'version': listing['draft_version']})


def missing(schema, attributes):
    """Use conditional official requirements, including nested required values."""
    from jsonschema import Draft201909Validator
    from referencing import Registry
    result = set()
    for error in Draft201909Validator(schema, registry=Registry()).iter_errors(attributes):
        if error.validator != 'required':
            continue
        path = list(error.absolute_path)
        if path:
            result.add(str(path[0]))
        else:
            result.update(name for name in error.validator_value if name not in error.instance)
    return sorted(name for name in result if name in schema.get('properties', {}) and archive_fields.is_fact(name))


def continue_job(c, listing):
    """The existing continue action resumes only a definite field failure."""
    job = ensure(c, listing)
    if not job:
        return None
    if job['kind'] != 'listing_fields' or job['payload'].get('listing_id') != listing['id']:
        raise HTTPException(409, '字段任务与当前商品不一致，请核对原任务')
    if job['status'] == 'unknown':
        raise HTTPException(409, '原字段请求结果不明，保留已有任务，不重复生成')
    from . import jobs
    if job['status'] in {'queued', 'running', 'retry_wait'}:
        return jobs.authorize_preparation(c, job)
    if job['status'] == 'cancelled':
        raise HTTPException(409, '原字段任务已取消，请从当前资料重新开始补齐')
    if job['status'] != 'paused':
        return job
    proof = listing['draft'].get('source', {}).get('attribute_completion') or {}
    if (proof.get('policy') == POLICY and proof.get('status') == 'needs_fields'
            and proof.get('signature') == signature(listing['draft'])):
        raise HTTPException(422, '仍缺少真实来源，不能重复生成代替实物资料：' + s.dump(proof.get('remaining', {})))
    if job['payload'].get('version') != listing['draft_version']:
        raise HTTPException(409, '原字段任务属于旧资料版本，请按当前版本补齐')
    return jobs.retry(c, job['id'])


def requested_fields(schema, names):
    """Include fields made conditional by a value being requested, e.g. batteries."""
    result = set(names)
    def related(node):
        if not isinstance(node, dict):
            return
        condition = node.get('if')
        if isinstance(condition, dict):
            used = set(condition.get('properties', {})) | set(condition.get('required', []))
            if used & result:
                def required(branch):
                    if isinstance(branch, dict):
                        result.update(name for name in branch.get('required', [])
                                      if name in schema.get('properties', {}) and archive_fields.is_fact(name))
                        for value in branch.values():required(value)
                    elif isinstance(branch, list):
                        for value in branch:required(value)
                required(node.get('then')); required(node.get('else'))
        for key in ('allOf', 'anyOf', 'oneOf'):
            for value in node.get(key, []):related(value)
    while True:
        old = set(result); related(schema)
        if old == result:break
    return sorted(result)


def improvement_fields(schema, product_type, listing, sku):
    """Use actual platform requests and the category's own subtype enum.

    Optional fields never become invented mandatory facts. Category subtypes
    use the fetched schema (e.g. pillow_type or towel_form_type), not a default
    pillow value applied to unrelated goods. This adds bounded rules to the
    existing source call instead of another planning/review task.
    """
    props = schema.get('properties') or {}
    names = set(listing_issues.missing_fields(listing, sku))
    prefix = str(product_type).lower() + '_'
    for name, rule in props.items():
        value = (rule.get('items') or {}).get('properties', {}).get('value', {})
        if name.startswith(prefix) and name.endswith(('_type', '_form')) and value.get('enum'):
            names.add(name)
    return sorted(name for name in names if name in props and archive_fields.is_fact(name))


def compact_rule(rule, root, seen=()):
    if isinstance(rule, list):return [compact_rule(value, root, seen) for value in rule]
    if not isinstance(rule, dict):return rule
    reference = rule.get('$ref')
    if reference and reference.startswith('#/') and reference not in seen:
        value = root
        for part in reference[2:].split('/'):value = value[part.replace('~1', '/').replace('~0', '~')]
        return compact_rule(value, root, seen + (reference,))
    keep = {'type', 'properties', 'items', 'required', 'enum', 'enumNames', 'const', 'anyOf', 'oneOf',
            'allOf', 'if', 'then', 'else', 'not', 'minItems', 'maxItems', 'minLength', 'maxLength',
            'minimum', 'maximum', 'description', 'additionalProperties', 'pattern', 'maxUtf8ByteLength'}
    return {key: ({name: compact_rule(value, root, seen) for name, value in child.items()}
                  if key == 'properties' else compact_rule(child, root, seen))
            for key, child in rule.items() if key in keep}


def cleared(record, target, product_type):
    saved = record.get('platform_attributes', {}).get(archive_fields.scope_key(target, product_type), {})
    return set(saved.get('cleared_fields') or [])


def restore(draft, previous):
    """Keep derived official values across image-only work; never override an archive edit."""
    proof = previous.get('source', {}).get('attribute_completion') or {}
    source = draft.get('source') or {}; old_source = previous.get('source') or {}
    if any(source.get(key) != old_source.get(key) for key in ('archive_digest', 'archive_signature', 'target_scope')):
        return
    if draft.get('language') != previous.get('language') or draft.get('product_type') != previous.get('product_type'):
        return
    for row in draft.get('skus', []):
        for name, evidence in proof.get('values', {}).get(str(row['sku_id']), {}).items():
            row.setdefault('attributes', {}).setdefault(name, copy.deepcopy(evidence['value']))
    for name, evidence in proof.get('values', {}).get('parent', {}).items():
        draft.setdefault('parent_attributes', {}).setdefault(name, copy.deepcopy(evidence['value']))
    if proof:draft.setdefault('source', {})['attribute_completion'] = copy.deepcopy(proof)


def parse_values(raw, requests, schemas, *, unresolved=None):
    """Only schema-typed requested data is executable; no extra AI review."""
    from . import jobs
    from app.model_retry import ModelFailure
    try:
        text = raw.strip()
        if text.startswith('```'):text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        content = json.loads(text)
        rows = content['rows']
        if not isinstance(rows, list):raise ValueError()
        found = {}
        for row in rows:
            request = next((item for item in requests if item['key'] == row.get('key')), None)
            if request is None or row['key'] in found:raise ValueError()
            values = row.get('attributes', {})
            evidence = row.get('evidence', {})
            if not isinstance(values, dict) or not isinstance(evidence, dict):raise ValueError()
            schema = schemas[request['role']]
            accepted = {}
            for name, value in values.items():
                if name not in request['fields'] or name not in evidence or not isinstance(evidence[name], str) or not evidence[name].strip():raise ValueError()
                partial = {'type': 'object', '$defs': schema.get('$defs', {}),
                           'properties': {name: schema['properties'][name]}, 'required': [name]}
                if jobs.validate_attributes(partial, {name: value}):raise ValueError()
                accepted[name] = {'value': value, 'basis': evidence[name]}
                candidate = request.get('source_candidates', {}).get(name)
                if candidate:
                    if not any(str(item.get('value', '')).strip() for item in value):raise ValueError()
                    accepted[name]['source_projection'] = copy.deepcopy(candidate['source'])
            # A known source projection is a translation/formatting task, not
            # a missing physical fact. An omitted answer must use normal bounded
            # failover instead of silently becoming a permanent source pause.
            if set(request.get('source_candidates', {})) - set(accepted):
                raise ValueError()
            reasons = row.get('unresolved') or {}
            if not isinstance(reasons, dict):raise ValueError()
            if unresolved is not None:
                unresolved[row['key']] = {name: reason for name, reason in reasons.items()
                                         if name in request['fields'] and name not in accepted
                                         and isinstance(reason, str) and reason.strip()}
            found[row['key']] = accepted
        if set(found) != {item['key'] for item in requests}:raise ValueError()
        return found
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ModelFailure('官方属性响应缺少可执行的字段、来源说明或不符合当前字段格式', 'invalid') from exc


def run(job):
    from . import catalog, listing_identity, variation_families, product_archive
    from app import design_transport, simple_design_flow
    from app.model_retry import ModelFailure
    from app.simple_design_prompts import clean_model_brief
    ident = job['payload']['listing_id']
    with s.main().db() as c:
        listing = s.get(c, 'listing', ident); target = s.get(c, 'target', listing['target_id'])
        if current(listing.get('draft') or {}, listing):return {'listing_id': ident, 'already_complete': True}
        if listing['draft_version'] != job['payload']['version']:raise HTTPException(409, '资料版本已更新，请对当前版本自动补齐')
        listing_identity.assert_source_current(c, listing, target, listing['draft'])
        product = s.get(c, 'product', listing['product_id'])
        sources = [s.get(c, 'sku', row['sku_id']) for row in listing['draft']['skus']]
        family_sources = product_archive.family_skus(c, product)
    draft = copy.deepcopy(listing['draft']); product_type = draft['product_type']
    language = locales.resolve(target)
    family = len(draft['skus']) > 1 and draft.get('source', {}).get('variation_preference') != 'manual_independent'
    family = family and not any(listing_identity.offer_only(row) for row in draft['skus'])
    role = 'CHILD' if family else 'NONE'
    definitions = {role: amazon.definition(target, product_type, role)}
    if family:definitions['PARENT'] = amazon.definition(target, product_type, 'PARENT')
    for label, definition in definitions.items():amazon.assert_definition_scope(definition, target, product_type, label)
    schemas = {label: definition['schema'] for label, definition in definitions.items()}
    if family:
        eligible=copy.deepcopy(schemas[role])
        common=variation_families.active_themes(eligible)&variation_families.active_themes(schemas['PARENT'])
        eligible.setdefault('properties',{})['variation_theme']={'items':{'properties':{'name':{'enum':sorted(common)}}}}
        plan = variation_families.proposal(draft, sources, eligible, target)
        if plan.get('status') not in {'ready','independent'}:raise HTTPException(422, plan.get('reason', '实际规格差异尚不能建立关系'))
        if listing.get('published_version') and (draft.get('variation_theme') or '')!=(plan.get('theme') or ''):
            plan['existing_relationship_change']={'parent_sku':draft.get('parent_sku'),
                'previous_theme':draft.get('variation_theme'),'published_version':listing['published_version'],
                'status':'requires_platform_change','reason':'已发布父子关系须单独解除并回读，不能只省略字段'}
        variation_families.apply(draft, plan, target, product['id'])
        if plan['status']=='independent':
            family=False;role='NONE'
            definitions[role]=amazon.definition(target,product_type,role)
            amazon.assert_definition_scope(definitions[role],target,product_type,role)
            schemas[role]=definitions[role]['schema']
    requests = []; paths = []; images = {}; facts = {}
    for source in sources:
        indexes = []
        for url in product_archive.source_images(product, source, len(family_sources)):
            path = simple_design_flow.local_path(url, source=True)
            if url not in images:
                paths.append(path)
                images[url] = {'index': len(paths), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            indexes.append(images[url]['index'])
        facts[str(source['id'])] = {'name': source.get('name'), 'facts': clean_model_brief(product_archive.facts_text(source)), 'original_image_indexes': indexes}
    attachments = {}
    for source in family_sources:
        for url in product_archive.source_images(product, source, len(family_sources)):
            if url in images:
                image_info = images[url]
                attachments.setdefault(image_info['sha256'], []).append(source['id'])
    values = copy.deepcopy(draft.get('source', {}).get('attribute_completion', {}).get('values', {}))
    requested_improvements = {}
    required_preflight = listing_issues.preflight_missing(listing)
    def request(key, label, attributes, records, seller_sku=''):
        blocked = set().union(*(cleared(record, target, product_type) for record in records))
        needed = sorted(set(missing(schemas[label], attributes)) | set(required_preflight.get(seller_sku, [])))
        extra = improvement_fields(schemas[label], product_type, listing, seller_sku)
        requested_improvements[key] = extra
        names = [name for name in requested_fields(schemas[label], sorted(set(needed) | set(extra)))
                 if name in schemas[label].get('properties', {}) and name not in blocked and not attributes.get(name)]
        if names:
            provenance = {}
            source = records[-1] if len(records) > 1 else {'source_fields': {}}
            with s.main().db() as c:
                projected = archive_fields.encode(c, product, source, target, schemas[label], product_type,
                                                 provenance=provenance)
            candidates = {name: {'attributes': projected[name], 'source': provenance[name]}
                          for name in names if name in provenance and name in projected}
            requests.append({'key': key, 'role': label, 'required_now': needed, 'fields': names,
                             'improvement_fields': extra,
                             'source_candidates': candidates,
                             'rules': {name: compact_rule(schemas[label]['properties'][name], schemas[label]) for name in names}})
    for row, source in zip(draft['skus'], sources):
        if listing_identity.offer_only(row):continue
        effective = copy.deepcopy(row); effective['gtin_exempt'] = not effective.get('gtin') and not effective.get('asin')
        request(str(row['sku_id']), role, catalog.attributes(target, draft, effective, schemas[role]), [product, source], row['seller_sku'])
    if family:request('parent', 'PARENT', catalog.parent_attributes(target, draft, schemas['PARENT']), [product], draft.get('parent_sku', ''))
    unresolved = {}
    generated = {}
    if requests:
        models = design_transport.candidates(s.main()); model = models[min(job.get('model_role', 0), len(models)-1)]
        prompt = '''根据本商品完整原始供应商资料与附件原图，补齐本店铺/地区当前类目实际要求的字段。不是审核或评分，不重写文案、报价或图片。
逐SKU使用其文字资料和original_image_indexes指定的原图。可读出主体清楚可见的形状、图案和配色。附件对应多个规格时，只采用画面中可明确归于该SKU或这些规格共有的特征；多件或多色且无法定位本SKU时，对该字段说明原因。材料、数值、销售组成和功能以对应文字为准。
严格使用各字段给出的当前官方结构、枚举和目标语言。条件字段只在实际条件成立时填写。保留所有已有字段和商家清空值，输出只含本次请求的缺项。
improvement_fields是平台完善信息或类目细分类型，虽不一定阻止销售，也要用本SKU真实来源尽量补齐；无法证明则留在unresolved，不能套用默认类型、硬度、风格或使用场所。自然高度与睡下高度分开，范围、约数不改成精确单值，不把8–9写成89。
source_candidates是已有来源的显示字段补充，保留其部位与含义，按当前字段规则翻译和组织表达；不把产品名称说成厂家分配的编号，不把材料特点扩展为未经证实的性能。
型号名称按真实产品系列名称表达，优先来源中明确的型号名；本商家确认可用真实产品名作显示名称，不能把颜色、尺寸、内部SKU或虚构编号当型号。厂家型号编号如资料未提供而当前规则明确允许空字符串，可用空字符串表达“资料未提供”，不能声称厂家分配了编号或编造代码。
危险品没有适用性证据时优先当前规则允许的unknown；没有unknown时保持待补，不冒充已证明不适用。证书适用性要理解规则描述和本产品真实组成，不把“不适用”当作所有商品默认值，不编造证书或认证编号。普通纺织品不能被写成有CARB木制板材证书。电池、供电、年龄等依据本商品来源，不用其他商品声明。
父体仅表达全部子体共享事实，不能拿某一个子体的材质、重量或功能代表全家。各字段evidence写对应源句或原图附件编号；未知而允许的unknown/空值明确说明未知，不伪称实物证实。确实缺乏依据的字段不输出值，在unresolved中说明。
只返回JSON {"rows":[{"key":"请求key","attributes":{"官方字段":[{"value":"实际值"}]},"evidence":{"官方字段":"具体来源或如实未知"},"unresolved":{"缺项":"缺失原因"}}]}。每个请求key都要返回，当前必填字段逐项给出值及依据，或在unresolved中写出具体原因。
'''
        prompt += '\n目标：' + s.dump({'scope': listing_identity.target_scope(target), 'language': language, 'product_type': product_type})
        shared = product_archive.shared_design_record(product, family_sources)
        prompt += '\n系列真实资料：' + clean_model_brief(product_archive.facts_text(shared))
        prompt += '\n原图对应规格：' + s.dump({image_info['index']: sorted(set(attachments[image_info['sha256']]))
                                              for image_info in images.values()})
        prompt += '\nSKU真实资料：' + s.dump(facts) + '\n父体覆盖上述全部SKU；父体key为parent。'
        prompt += '\n本次缺项与官方规则：' + s.dump(requests)
        generated = parse_values(design_transport.text(s.main(), model, prompt, paths), requests, schemas,
                                 unresolved=unresolved)
        for key, fields in generated.items():
            attributes = draft.setdefault('parent_attributes', {}) if key == 'parent' else next(row for row in draft['skus'] if str(row['sku_id']) == key)['attributes']
            for name, evidence in fields.items():
                attributes[name] = copy.deepcopy(evidence['value']); values.setdefault(key, {})[name] = copy.deepcopy(evidence)
    remaining = {}
    for row in draft['skus']:
        if listing_identity.offer_only(row):continue
        effective = copy.deepcopy(row); effective['gtin_exempt'] = not effective.get('gtin') and not effective.get('asin')
        encoded = catalog.attributes(target, draft, effective, schemas[role])
        absent = sorted(set(missing(schemas[role], encoded)) | {
            name for name in required_preflight.get(row['seller_sku'], []) if not encoded.get(name)})
        if absent:remaining[row['seller_sku']] = absent
    if family:
        encoded = catalog.parent_attributes(target, draft, schemas['PARENT'])
        absent = sorted(set(missing(schemas['PARENT'], encoded)) | {
            name for name in required_preflight.get(draft.get('parent_sku', ''), []) if not encoded.get(name)})
        if absent:remaining['父体'] = absent
    request_fields = {row['key']: set(row['fields']) for row in requests}
    sku_keys = {row['seller_sku']: str(row['sku_id']) for row in draft['skus']} | {'父体': 'parent'}
    omissions = {}
    for label, fields in remaining.items():
        key = sku_keys[label]
        answered = generated.get(key, {}).keys() | unresolved.get(key, {}).keys()
        unanswered = (set(fields) & request_fields.get(key, set())) - answered
        if unanswered:
            omissions[label] = sorted(unanswered)
    if omissions:
        raise ModelFailure('官方属性响应漏填必填字段且未说明原因：' + s.dump(omissions), 'invalid')
    improvements = {}
    for row in draft['skus']:
        absent = [name for name in requested_improvements.get(str(row['sku_id']), [])
                  if not row.get('attributes', {}).get(name)]
        if absent:improvements[row['seller_sku']] = absent
    draft.setdefault('source', {})['attribute_completion'] = {'policy': POLICY, 'status': 'needs_fields' if remaining else 'ready',
        'platform_missing': listing_issues.completion_scope(listing), 'improvements_remaining': improvements,
        'values': values, 'remaining': remaining, 'unresolved': unresolved, 'scope': listing_identity.target_scope(target),
        'schemas': {key: value.get('schema_sha256') for key, value in definitions.items()}, 'original_images': images,
        'checked_at': datetime.now(timezone.utc).isoformat(), 'checked_epoch': time.time(), 'job_id':job.get('id')}
    draft['source']['attribute_completion']['signature'] = signature(draft)
    with s.transaction() as c:
        live = s.get(c, 'listing', ident)
        if live['revision'] != listing['revision']:raise HTTPException(409, '补齐期间资料已编辑，请重新操作；不覆盖商家更新')
        listing_identity.assert_source_current(c, live, s.get(c, 'target', target['id']), draft)
        for label, definition in definitions.items():s.put(c, 'definition', f"{target['id']}:{product_type}:{label}", definition | {'target_id': target['id']})
        live.update(draft=draft, draft_version=live['draft_version']+1)
        live.pop('preview_results', None)
        live['issues'] = catalog.issues(draft, target); live['draft_status'] = 'needs_fields' if live['issues'] or remaining else 'ready'
        s.put(c, 'material_version', f"{ident}:{live['draft_version']}", {'listing_id': ident, 'version': live['draft_version'], 'draft': draft})
        s.save(c, 'listing', live); s.audit(c, 'official_attributes_completed', ident, s.dump({'version': live['draft_version'], 'remaining': remaining, 'policy': POLICY}))
    if remaining:raise HTTPException(422, '仍缺少有依据的官方字段：' + '；'.join(key+'：'+ '、'.join(fields) for key, fields in remaining.items()))
    return {'listing_id': ident, 'version': live['draft_version'], 'completed': True, 'variation_theme': draft.get('variation_theme')}
