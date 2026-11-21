"""Pure page/request contracts. No database, model, image editing or quality gate."""
import copy
import hashlib
import json

from app import design_inputs as inputs

PAGE_VERSION = 'design-page-v3'
SAVED_PAGE_VERSIONS = {'design-page-v2', PAGE_VERSION}
REQUEST_VERSION = 'design-request-v2'
STYLE_FIELDS = ('palette', 'lighting', 'typography', 'environment_materials')
EXECUTION_FIELDS = ('shot_type', 'camera', 'subject_view', 'scene', 'composition', 'lighting')
LEGACY_VISUAL_FIELDS = ('shot_type', 'visual_hook', 'camera', 'focus', 'reference_basis',
                        'scene', 'composition', 'lighting')
PLANNING_FIELDS = ('story_role', 'takeaway', 'transition', 'purpose')


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dump(value).encode('utf-8')).hexdigest()


def prompt_hash(prompt):
    return hashlib.sha256(prompt.encode('utf-8')).hexdigest()


def output_size(stage, page=None):
    if stage not in {'master', 'image'}:
        return None
    page = page or {}
    return ('1024x1536' if page.get('display_role') == 'detail_page' else
            '1024x1024' if page.get('type') == '主图' else '1536x1024')


def prose_list(values):
    values = values if isinstance(values, list) else [values] if values else []
    return [inputs.prose(v) for v in values if inputs.prose(v)]


def stable_style(direction):
    identity = (direction or {}).get('visual_identity') or {}
    return {key: inputs.prose(identity[key]) for key in STYLE_FIELDS
            if isinstance(identity, dict) and inputs.prose(identity.get(key))}


def validate_execution(execution, regions):
    if 'content_regions' in execution:
        raise ValueError('详情区域必须通过regions绑定，不能在execution中重复定义')
    if any('region_id' in region['execution'] for region in regions):
        raise ValueError('区域编号只能定义一次，不能被执行内容覆盖')


def validate_current(copy_text, execution, regions):
    """Validate field ownership, never judge copy meaning or image quality."""
    if set(copy_text) - {'title', 'lines'}:
        raise ValueError('顾客成稿只能放在copy.title和copy.lines')
    if set(execution) - set(EXECUTION_FIELDS):
        raise ValueError('执行字段只描述镜头与版式；文字要求必须直接写入copy')
    if any(not isinstance(v, str) for v in execution.values()):
        raise ValueError('镜头与版式字段必须是文字')
    for region in regions:
        if region['region_id'] == 'hero':
            raise ValueError('hero由顶部copy占用，详情regions只定义其余区域')
        if set(region['copy']) - {'heading', 'message'} or set(region['execution']) - {'visual'}:
            raise ValueError('详情区域只接受copy成稿和visual画面')
        if any(not isinstance(v, str) for v in region['execution'].values()):
            raise ValueError('详情区域画面必须是文字')


def normalize(value, product, page, binding=None, *, strict=False):
    """One canonical representation, with a recorded one-way legacy conversion.

    Saved v1/v2 execution is retained without rewriting past requirements.
    New v3 plans use explicit visual fields, with no second copy-instruction list.
    """
    if not isinstance(value, dict):
        value = {'scene': inputs.prose(value)}
    owner = {'page_key': page.get('page_key') or value.get('page_key') or 'unassigned',
             'product_key': product.get('key', 'unassigned'),
             'sku_id': product.get('sku_id'), 'role': page.get('display_role') or
             ('sales_cover' if page.get('type') == '主图' else 'story')}
    if value.get('schema') in SAVED_PAGE_VERSIONS:
        if strict and value['schema'] != PAGE_VERSION:
            raise ValueError('新规划必须使用当前单页职责，不能套用旧版方案')
        if any(value.get(k) != v for k, v in owner.items()):
            raise ValueError('单页规格与当前SKU或页面身份不匹配')
        if any(not isinstance(value.get(k), dict) for k in ('copy', 'execution', 'planning', 'provenance')):
            raise ValueError('单页规格内容结构损坏')
        regions=value.get('regions')
        if (not isinstance(regions,list) or any(not isinstance(r,dict) or not r.get('region_id') or
                not isinstance(r.get('copy'),dict) or not isinstance(r.get('execution'),dict) for r in regions)
                or len({r['region_id'] for r in regions})!=len(regions)):
            raise ValueError('详情区域编号或内容损坏')
        validate_execution(value['execution'], regions)
        if value['schema'] == PAGE_VERSION:
            validate_current(value['copy'], value['execution'], regions)
        result = copy.deepcopy(value)
        result['subject_scope'] = inputs.subject_binding(product, previous=binding or value.get('subject_scope'))
        return result
    structured = any(key in value for key in ('copy', 'execution', 'planning', 'regions'))
    if strict and (not structured or any(not isinstance(value.get(k), dict) for k in ('copy', 'execution'))):
        raise ValueError('新规划必须分别提供copy成稿与execution画面，不能使用旧平铺字段')
    routes = {}
    if structured:
        misplaced = set(value) & (set(LEGACY_VISUAL_FIELDS) | set(PLANNING_FIELDS) |
                                  {'title', 'lines', 'content_blocks', 'differentiator'})
        if misplaced:
            raise ValueError('单页方案混用平铺与分层字段，请将内容放入对应职责：' + ','.join(sorted(misplaced)))
        for key in ('copy', 'execution', 'planning'):
            if key in value and not isinstance(value[key], dict):
                raise ValueError('单页规格的' + key + '必须是对象')
        raw_copy = value.get('copy', {})
        execution = copy.deepcopy(value.get('execution', {}))
        planning = copy.deepcopy(value.get('planning', {}))
        regions = value.get('regions', [])
        routes.update(copy='copy', execution='execution', planning='planning', regions='regions')
    else:
        raw_copy = value
        execution = {k: copy.deepcopy(value[k]) for k in LEGACY_VISUAL_FIELDS if k in value}
        planning = {k: copy.deepcopy(value[k]) for k in PLANNING_FIELDS if k in value}
        # This field carried real instructions in saved v1 plans, not just prose.
        execution['constraints'] = prose_list(value.get('differentiator'))
        blocks = value.get('content_blocks') or []
        blocks = blocks if isinstance(blocks, list) else [blocks]
        regions = [{'copy': {k: block.get(k, '') for k in ('heading', 'message')},
                    'execution': {'visual': block.get('visual', '')}}
                   for block in blocks if isinstance(block, dict)]
        routes.update({k: 'execution.' + k for k in LEGACY_VISUAL_FIELDS if k in value})
        routes.update({k: 'planning.' + k for k in PLANNING_FIELDS if k in value})
        routes.update(title='copy.title', lines='copy.lines', content_blocks='regions',
                      differentiator='execution.constraints')
    if not isinstance(regions, list):
        raise ValueError('详情区域必须是列表')
    execution['shot_type'] = inputs.prose(execution.get('shot_type') or page.get('shot_type'))
    normalized_regions = []
    seen = set()
    for index, region in enumerate(regions, 1):
        if not isinstance(region, dict):
            continue
        region_id = inputs.prose(region.get('region_id')) or f'region-{index}'
        if region_id in seen:
            raise ValueError('详情区域编号重复，无法绑定文字与画面')
        seen.add(region_id)
        region_copy = region.get('copy') or {}
        region_execution = region.get('execution') or {}
        if not isinstance(region_copy, dict) or not isinstance(region_execution, dict):
            raise ValueError('详情区域的文字与执行必须是对象')
        if strict:
            validate_current({}, {}, [{'region_id': region_id, 'copy': region_copy, 'execution': region_execution}])
        normalized_regions.append({'region_id': region_id,
            'copy': {k: inputs.prose(region_copy.get(k)) for k in ('heading', 'message')},
            'execution': copy.deepcopy(region_execution)})
    validate_execution(execution, normalized_regions)
    if strict:
        validate_current(raw_copy, execution, normalized_regions)
    consumed = set(routes) | {'page_key', 'subject_binding'}
    return {'schema': PAGE_VERSION if strict else 'design-page-v2', **owner,
            'subject_scope': inputs.subject_binding(product, previous=binding or value.get('subject_binding')),
            'copy': {'title': inputs.prose(raw_copy.get('title')), 'lines': prose_list(raw_copy.get('lines'))},
            'execution': execution, 'regions': normalized_regions, 'planning': planning,
            'provenance': {'source_format': 'structured-page-v3' if strict else 'structured-page-v2' if structured else 'flat-page-v1',
                           'field_routes': routes,
                           'unmapped': {k: copy.deepcopy(v) for k, v in value.items() if k not in consumed}}}


def visual(page_spec):
    return copy.deepcopy(page_spec['execution']) | {
        'content_regions': [{'region_id': r['region_id'], **copy.deepcopy(r['execution'])}
                            for r in page_spec['regions']]}


def printable(page_spec, *, main=False, detail=True):
    if main:
        return {'title': '', 'lines': [], 'content_blocks': []}
    return copy.deepcopy(page_spec['copy']) | {'content_blocks': [
        {'region_id': r['region_id'], **r['copy']} for r in page_spec['regions']] if detail else []}


def render(page_spec, *, main=False, detail=True):
    """Send each region's copy beside its visual exactly once, including the hero."""
    validate_current(page_spec['copy'], page_spec['execution'], page_spec['regions'])
    return {'frame': copy.deepcopy(page_spec['execution']),
            'blocks': [{'region_id': 'hero', 'copy': {'title': '', 'lines': []} if main else
                        copy.deepcopy(page_spec['copy'])}] +
                      [{'region_id': r['region_id'], 'copy': copy.deepcopy(r['copy']),
                        'visual': r['execution'].get('visual', '')} for r in page_spec['regions'] if detail]}


def preview(page_spec):
    """Stable presentation fields for existing detail readers; never re-executed."""
    return copy.deepcopy(page_spec['copy']) | {'content_blocks': [
        {'region_id': r['region_id'], **r['copy'], 'visual': r['execution'].get('visual', '')}
        for r in page_spec['regions']]}


def page_summary(page_spec):
    return {'shot_type': page_spec['execution'].get('shot_type', ''),
            **{key: inputs.prose(page_spec['planning'].get(key)) for key in PLANNING_FIELDS},
            **{key: inputs.prose(page_spec['execution'].get(key)) for key in ('visual_hook', 'camera')},
            'subject_binding': copy.deepcopy(page_spec['subject_scope'])}


def reference_manifest(product, references, stage, *, editing=False, source_required=True):
    """The same ordered manifest drives prompt roles and the transport receipt."""
    roles = [r['role'] for r in references]
    sources = list(dict.fromkeys(product.get('source_images') or []))
    expected = (['master'] if stage != 'master' else []) + ['source'] * (len(sources) if source_required or stage == 'master' else 0)
    if editing:
        expected += ['edit_target']
    if roles != expected:
        raise ValueError('附件角色或顺序与当前任务不一致')
    source_hashes = dict(zip(product.get('source_images') or [], product.get('source_hashes') or []))
    hashes = [source_hashes[url] for url in sources if url in source_hashes]
    actual_sources = [r['sha256'] for r in references if r['role'] == 'source']
    if actual_sources and hashes and actual_sources != hashes:
        raise ValueError('原图附件与本SKU冻结来源不一致')
    source_index = 0
    manifest = []
    for index, ref in enumerate(references, 1):
        if ref['role'] == 'source':
            source_index += 1
        manifest.append({'attachment_index': index, 'role': ref['role'], 'sha256': ref['sha256'],
                         'source_index': source_index if ref['role'] == 'source' else None,
                         'sku_id': product.get('sku_id'), 'path': ref['path']})
    return manifest


def request(prompt, product, stage, references, *, page_spec=None, page=None, source_contract='', frozen=False, editing=False, source_required=True):
    manifest = reference_manifest(product, references, stage, editing=editing, source_required=source_required)
    body = {'schema': REQUEST_VERSION, 'stage': stage, 'sku_id': product.get('sku_id'),
            'product_key': product['key'], 'page_key': (page_spec or {}).get('page_key'),
            'source_contract': source_contract, 'prompt_sha256': prompt_hash(prompt),
            'output_size': output_size(stage, page),
            'fact_sha256': digest(inputs.product_data(product)), 'references': manifest,
            'page_spec': copy.deepcopy(page_spec),
            'compile_mode': 'retained_sent_prompt' if frozen else 'compiled'}
    body['sha256'] = digest(body)
    return body


def verify_request(packet, prompt, references):
    if not packet or packet.get('schema') != REQUEST_VERSION:
        raise ValueError('缺少可核对的应用请求记录')
    if packet.get('sha256') != digest({k: v for k, v in packet.items() if k != 'sha256'}):
        raise ValueError('应用请求记录内容已变化')
    if packet['prompt_sha256'] != prompt_hash(prompt):
        raise ValueError('实际提示词与冻结请求不一致')
    wanted = [(r['path'], r['sha256'], r['role']) for r in packet['references']]
    actual = [(r['path'], r['sha256'], r['role']) for r in references]
    if actual != wanted:
        raise ValueError('实际附件与冻结请求的内容或顺序不一致')
    return True
