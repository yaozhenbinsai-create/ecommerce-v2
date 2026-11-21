"""Read-only, source-backed listing parameter history; no submission claims."""
from __future__ import annotations

import copy
import json

from . import store as s


def parameters(draft):
    """Expose only merchant-facing listing parameters, never arbitrary metadata."""
    result = {}
    for key, label in [('currency', '销售币种'), ('handling_days', '备货天数'),
                       ('shipping_group', '配送模板')]:
        result[("", label)] = copy.deepcopy(draft.get(key))
    content = draft.get('content') or {}
    for key, label in [('title', '系列标题'), ('bullets', '商品要点'),
                       ('description', '商品描述'), ('search_terms', '搜索关键词')]:
        result[("", label)] = copy.deepcopy(content.get(key))
    for sku in draft.get('skus') or []:
        identity = str(sku.get('seller_sku') or sku.get('sku_id') or '')
        for key, label in [('title', '规格标题'), ('search_terms', '规格搜索关键词'), ('price', '商品价'), ('quantity', '可售数量')]:
            result[(identity, label)] = copy.deepcopy(sku.get(key))
    return result


def compare(before, after):
    previous, current = parameters(before), parameters(after)
    return [{'seller_sku': key[0], 'parameter': key[1],
             'before': previous.get(key), 'after': current.get(key)}
            for key in sorted(previous.keys() | current.keys())
            if previous.get(key) != current.get(key)]


def read(ident):
    with s.main().db() as c:
        listing = s.get(c, 'listing', ident)
        from .material_history import summary
        cleanup = summary(c, ident)
        rows = c.execute(
            "SELECT data,created_at FROM commerce_records WHERE kind='material_version' "
            "AND json_extract(data,'$.listing_id')=? "
            "ORDER BY CAST(json_extract(data,'$.version') AS INTEGER) DESC LIMIT 51",
            (ident,)).fetchall()
    versions = [(json.loads(row[0]), row[1]) for row in reversed(rows)]
    entries = []
    for (before, _), (after, timestamp) in zip(versions, versions[1:]):
        changes = compare(before.get('draft') or {}, after.get('draft') or {})
        if changes:
            entries.append({'before_version': before['version'], 'version': after['version'],
                            'saved_at': timestamp, 'changes': changes})
    message = '展示最近50次资料版本保存中的参数变化。保存时间为UTC；资料保存和亚马逊生效分别记录，请结合当前平台回查结果。'
    if cleanup:
        message = (f"已清理{cleanup['materials']}份旧草稿、{cleanup['checks']}份旧校验结果，"
                   f"本轮工作基线为v{cleanup['version']}。这里只显示从该基线开始的新改动；历史回执仍可回查。"
                   '保存时间为UTC；资料保存不代表亚马逊生效。')
    return {'listing_id': ident, 'name': listing.get('name', ''),
            'current_version': listing.get('draft_version'), 'entries': list(reversed(entries)),
            'cleanup': cleanup, 'message': message}
