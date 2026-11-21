"""Design-only projections and source-bound visual selections; never source edits."""
import copy
import re

from app import simple_design_prompts as facts

POLICY = 'sku-subject-and-buyer-facts-v1'
SELECTION_FIELDS = ('selected_subjects', 'excluded_subjects', 'location')
RECORD_NOTE = re.compile(r'^(?:来源|导入来源|原表位置|source(?:_row|_sheet|_record)?|import(?:_row|_sheet)?)\s*[:：]', re.I)
RAW_NOTE = re.compile(r'(?:栏原文|原文)$')
RECORD_FIELD = re.compile(r'^(?:sku_id|variant_key|source_(?:row|sheet|record|field)|import_(?:row|sheet|id)|fact_revision|revision)\s*[:：]', re.I)


def prose(value):
    """Read scalar prose without printing arbitrary model JSON as buyer copy."""
    if isinstance(value, str):
        return value.strip()
    if type(value) in (int, float):
        return str(value)
    return ''


def description(value):
    lines = []
    for line in facts.clean_brief(value).splitlines():
        content = re.sub(r'^(?:description|special_feature|商品描述|产品描述|产品说明)\s*[:：]\s*', '', line, flags=re.I)
        if not RECORD_NOTE.match(content.strip()) and not RECORD_FIELD.match(line.strip()):
            lines.append(line)
    return '\n'.join(lines)


def product_data(product):
    """Keep buyer facts/conditions; original provenance remains in the snapshot."""
    result = copy.deepcopy(facts.product_facts(product))
    result.pop('key', None)
    result.pop('sku_id', None)
    groups = result.get('parameters')
    if isinstance(groups, dict):
        for group in list(groups):
            for field in list(groups[group]):
                source = ((product.get('design_facts') or {}).get(field) or {}).get('source') or {}
                if (RAW_NOTE.search(field) and source.get('kind') == 'fact_text') or RECORD_NOTE.match(field + ':'):
                    del groups[group][field]
                    continue
                record = groups[group][field]
                for key in ('scope', 'status', 'source_field'):
                    record.pop(key, None)
                evidence = record.pop('qualification_evidence', [])
                if not record.get('qualifiers') and not record.get('measurement_condition'):
                    conditions = [description(value) for value in evidence]
                    if any(conditions):
                        record['qualification_evidence'] = [value for value in conditions if value]
            if not groups[group]:
                del groups[group]
    elif isinstance(groups, str):
        result['parameters'] = description(groups)
    notes = [description(value) for value in result.pop('source_description', [])]
    if any(notes):
        result['source_description'] = [value for value in notes if value]
    return result


def identity(product):
    """Explicit labels are anchors; missing colors are never inferred in code."""
    return {'product_name': product.get('canonical_product_name') or product.get('name', ''),
            'variant_name': product.get('variant_name') or product.get('name', ''),
            'declared_color': product.get('color') or ''}


def subject_binding(product, selection=None, previous=None):
    """Bind a planning selection to exact inputs, without making new facts.

    Locations describe original visible subjects. No crops, masks, generated
    originals or inferred physical attributes. Invalid optional selections stay
    identity-only, without a critic, rejection or new request.
    """
    sources = list(dict.fromkeys(product.get('source_images') or []))
    hashes = product.get('source_hashes') or []
    scope = {'sku_id': product.get('sku_id'), 'product_key': product.get('key'),
             'identity': identity(product), 'source_images': sources, 'source_hashes': hashes,
             'product_facts': product_data(product)}
    input_key = facts.fingerprint(scope)
    if not isinstance(selection, dict) and isinstance(previous, dict) and previous.get('input_key') == input_key:
        selection = {'references': previous.get('references')}
    entries = selection.get('references', []) if isinstance(selection, dict) else []
    selected = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        index = entry.get('source_index')
        if type(index) is not int or not 1 <= index <= len(sources) or index in selected:
            continue
        values = {key: prose(entry.get(key)) for key in SELECTION_FIELDS}
        if not values['selected_subjects']:
            continue
        selected[index] = {'source_index': index,
                           'source_sha256': hashes[index - 1] if index <= len(hashes) else '', **values}
    return {'policy': POLICY, 'input_key': input_key, 'sku_id': scope['sku_id'],
            'product_key': scope['product_key'], 'identity': scope['identity'],
            'basis': 'planning_visual_selection' if selected else 'declared_identity_only',
            'references': list(selected.values())}


def subject_context(product, binding=None):
    bound = subject_binding(product, previous=binding)
    positions = {key: value for key, value in bound.items() if key != 'identity'}
    return ('本件实物与原图的对应：' + facts.dump(positions) + '\n'
            '商品身份、已声明颜色和销售组成只取本SKU事实；未声明的属性不当作已确认资料。'
            'references是同一次规划对原图可见实物的定位，source_index从有效原图1开始计数，不含母稿。'
            '沿用selected_subjects与location定位本件，excluded_subjects中的陈列物不进入场景；'
            '套装定位其真实全部组成，不把不同视图算成额外件数。定位描述不证明材料、功能、结构或参数，'
            '与商品事实冲突时保留事实，不从定位补造。此段仅用于实物识别，绝不作为顾客文案。')


def correction_context(correction, *, master_only=False):
    if not isinstance(correction, dict) or not prose(correction.get('instruction')):
        return ''
    reference = ('该修正已绑定本次母稿，适用部位按明确说明处理；未涉及部位仍取本次母稿。' if master_only else
                 '该修正与本次母稿一起用于后续方案和成图，适用部位按明确说明处理，不被原图中同处旧表现覆盖；未涉及部位仍取当前原图。')
    return ('本件当前明确外观修正：' + facts.dump({'instruction': prose(correction['instruction']),
            'source_task_id': correction.get('task_id'), 'source_sha256': correction.get('sha256')}) + '\n'
            + reference + '修正记录不是新增参数、材料或功效，也不是可印刷文字。')
