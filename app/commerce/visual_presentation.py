"""Sales-cover ordering and honest per-SKU display inspection.

No image/offer mutations occur here. A platform MAIN and a commercial cover are
different roles; this module never promotes an internal master into either.
"""
import copy
import json

from . import main_images, store as s

PROTOCOL = 'sku-sales-cover-story-v1'


def order_generated(photos, skus, pages):
    """Lead auxiliary storytelling with its planned cover, then story order.

    This is applied to newly generated materials only. A manual saved gallery is
    not rewritten by opening the inspector or by reading an old material.
    """
    positions = {page['page_key']:index for index,page in enumerate(pages)}
    keys = [sku['variant_key'] for sku in skus]
    ordered = []
    for key in keys:
        own = [photo for photo in photos if photo.get('variant_key') == key]
        own.sort(key=lambda photo:(photo.get('display_role') != 'sales_cover',
                                   positions.get(photo.get('page_key'), len(positions))))
        ordered.extend(own)
    ordered.extend(photo for photo in photos if photo.get('variant_key') not in keys)
    return ordered


def view(c, listing):
    """Describe exact saved per-SKU galleries and same-SKU detail segments."""
    draft = listing['draft']
    run_id = draft.get('source', {}).get('design_run_id')
    records = {}
    for row in c.execute("SELECT payload,result FROM design_simple_tasks WHERE run_id=? AND kind='image' AND status='completed'", (run_id,)):
        payload, result = json.loads(row['payload']), json.loads(row['result'])
        for digest in (result.get('sha256'),):
            if digest:
                records[digest] = {'page':payload.get('page', {}),
                                   'sku_id':payload.get('product', {}).get('sku_id')}
    plan = {'protocol':PROTOCOL, 'listing_id':listing['id'], 'revision':listing['revision'],
            'draft_version':listing['draft_version'], 'read_only':True, 'skus':[],
            'message':'逐规格核对实际首图、图库顺序与完整详情。新设计以场景封面作为指定首图；历史版本保留原选择。此处展示已保存资料，目标类目要求、平台接受与买家实际显示另行核实。'}
    sections = (draft.get('detail_page') or {}).get('sections', [])
    all_refs = list(draft.get('images', [])) + [ref for sku in draft['skus'] for ref in sku.get('images', [])]
    blocked = main_images.reference_board_ids(c, all_refs)
    for sku in draft['skus']:
        refs = [ref for ref in sku.get('images', []) if ref.get('role') == 'main']
        refs += [ref for ref in draft.get('images', []) if ref.get('variant_key') == sku['variant_key']]
        gallery, seen = [], set()
        for ref in refs:
            ident = ref.get('asset_id')
            if not ident or ident in seen or ident in blocked:
                continue
            asset = s.get(c, 'media', ident, required=False)
            if not asset or asset.get('sku_id') not in (None, sku['sku_id']):
                continue
            record = records.get(asset.get('original_sha256')) or records.get(asset.get('sha256')) or {}
            if record and record.get('sku_id') != sku['sku_id']:
                continue
            page = record.get('page', {})
            role = ref.get('display_role') or page.get('display_role')
            label = ('平台首图' if ref.get('role') == 'main' else
                     '销售封面 · 场景与价值' if role == 'sales_cover' else
                     '场景与使用' if page.get('shot_type') in {'context','use'} else
                     '可见细节' if page.get('shot_type') == 'visible_detail' else
                     '规格与销售组成' if page.get('shot_type') == 'specification' else
                     '介绍图' if ref.get('role') == 'detail' else '效果图')
            gallery.append({'asset_id':ident, 'url':asset['url'], 'label':label,
                            'display_role':role or '', 'page_key':page.get('page_key', '')})
            seen.add(ident)
        detail = [copy.deepcopy(section) for section in sections if section.get('sku_id') == sku['sku_id']]
        message = ('当前保存的图库不足7张，需要补足有独立内容价值的设计；不能用母稿、重复图或其他规格凑数。'
                   if len(gallery) < 7 else '图库数量已达到7张；仍需核对内容、规格、顺序以及实际显示。')
        plan['skus'].append({'sku_id':sku['sku_id'], 'seller_sku':sku['seller_sku'],
                             'gallery_count':len(gallery), 'detail_count':len(detail),
                             'detail_unit':'张完整详情图' if (draft.get('detail_page') or {}).get('presentation_protocol')=='one-detail-per-sku-v1' else '段',
                             'gallery':gallery, 'detail':detail, 'message':message})
    return plan
