"""Use intact, same-SKU completed detail sections in sparse product galleries."""
import copy
import hashlib

from fastapi import HTTPException

from . import media, store as s


def digest(value):
    return hashlib.sha256(s.dump(value).encode()).hexdigest()


def matches_section_record(draft, sku, record):
    from app import detail_page
    detail = draft.get('detail_page') or {}
    if (record['kind'] != 'image' or record['run_id'] != detail.get('run_id') or
            record['status'] != 'completed' or record['slot'] != 'result' or
            record['product'].get('sku_id') != sku.get('sku_id') or
            not detail_page.manifest_ready(detail)):
        return False
    result = record['result']
    return any(section.get('sku_id') == sku['sku_id'] and
               section.get('sha256') == result.get('sha256') and
               section.get('url') == result.get('url') and
               section.get('page_key') == record['page'].get('page_key')
               for section in detail.get('sections', []))


def _available(c, listing, target):
    from . import listing_release
    if listing.get('paused'):
        raise HTTPException(409, '商品已暂停，请先核对本商品流程')
    listing_release.assert_preparation_available(c, listing, target)


def augment(draft):
    """Append complete matching sections, preserving existing gallery order."""
    from app import detail_page
    detail = draft.get('detail_page') or {}
    if not detail_page.manifest_ready(detail):
        return []
    added = []
    for sku in draft['skus']:
        refs = sku.get('images', []) + [r for r in draft.get('images', [])
                                       if r.get('variant_key') == sku['variant_key']]
        ids = {r['asset_id'] for r in refs}
        # Existing complete galleries and every manual image/order are preserved.
        if len(ids) >= 7:
            continue
        for section in detail['sections']:
            if section['sku_id'] != sku['sku_id'] or len(ids) >= 9:
                continue
            path = s.main().simple_design_flow.local_path(section['url'], pid=detail['project_id'])
            asset = media.ingest(path.read_bytes(), path.name, sku['sku_id'], source_path=path)
            if asset['id'] in ids:
                continue
            ref = {'asset_id': asset['id'], 'variant_key': sku['variant_key'], 'role': 'detail',
                   'source': 'design_detail_section', 'design_run_id': detail['run_id'],
                   'page_key': section['page_key'], 'original_sha256': section['sha256']}
            draft['images'].append(ref)
            added.append({'sku_id': sku['sku_id'], **ref})
            ids.add(asset['id'])
    return added


def attach(ident, revision, actor=''):
    from . import catalog, detail_publication, main_images
    with s.main().db() as c:
        listing = s.get(c, 'listing', ident)
        if listing['revision'] != revision:
            raise HTTPException(409, '资料已变化，请刷新后整理图库')
        target = s.get(c, 'target', listing['target_id'])
        _available(c, listing, target)
        draft = copy.deepcopy(listing['draft'])
        detail_publication.validate_detail(c, listing, draft)
    added = augment(draft)
    if not added:
        return listing
    certificate = {'protocol': 'same-sku-detail-gallery-v1', 'base_version': listing['draft_version'],
                   'base_draft_sha256': digest(listing['draft']), 'added': added}
    draft['source']['gallery_assembly'] = certificate
    with s.transaction() as c:
        current = s.get(c, 'listing', ident)
        if current['revision'] != revision:
            raise HTTPException(409, '整理图库期间资料已编辑，保留原版本，请刷新重试')
        _available(c, current, target)
        detail_publication.validate_detail(c, current, draft)
        for sku in draft['skus']:
            excluded = set(main_images.validate_auxiliary(c, draft, sku)['excluded_asset_ids'])
            if any(r['asset_id'] in excluded for r in added if r['sku_id'] == sku['sku_id']):
                raise HTTPException(422, '详情图未通过当前规格及来源对应核对，原图库保留')
        current.update(draft=draft, draft_version=current['draft_version'] + 1)
        current.pop('preview_results', None)
        current['issues'] = catalog.issues(draft, target)
        current['draft_status'] = 'needs_fields' if current['issues'] else 'ready'
        s.put(c, 'material_version', f"{ident}:{current['draft_version']}",
              {'listing_id': ident, 'version': current['draft_version'], 'draft': draft})
        result = s.save(c, 'listing', current)
        s.audit(c, 'same_sku_detail_added_to_gallery', ident,
                s.dump({'actor': actor, 'version': result['draft_version'], 'added': added}))
        return result


def reusable_detail(c, listing, draft, target):
    """Reuse unchanged A+ receipts only for a certified gallery-only revision."""
    from . import detail_publication
    proof = draft.get('source', {}).get('gallery_assembly') or {}
    if proof.get('protocol') != 'same-sku-detail-gallery-v1':
        return None
    material = s.by_key(c, 'material_version', f"{listing['id']}:{proof.get('base_version')}")
    if not material or digest(material['draft']) != proof.get('base_draft_sha256'):
        return None
    old = material['draft']
    restored = copy.deepcopy(draft)
    restored['source'] = copy.deepcopy(old['source'])
    restored['images'] = copy.deepcopy(old['images'])
    if restored != old or draft['images'][:len(old['images'])] != old['images']:
        return None
    added = [{k: v for k, v in r.items() if k != 'sku_id'} for r in proof.get('added', [])]
    if not added or draft['images'][len(old['images']):] != added:
        return None
    pub = s.get(c, 'detail_publication', listing.get('detail_publication_id'), required=False)
    publication_material = (s.by_key(c, 'material_version', f"{listing['id']}:{pub['version']}")
                            if pub else None)
    if (not pub or not publication_material or publication_material['draft'] != old or pub.get('write_step') or
            pub['status'] not in {'submitted', 'approved', 'published', 'mock_published', 'grouped'} or
            pub.get('destination') != detail_publication.identity(c, target)):
        return None
    if pub['status'] == 'grouped':
        children = [s.get(c, 'detail_publication', ident) for ident in pub.get('children', [])]
        if not children or any(child.get('write_step') or child['status'] not in
                {'submitted', 'approved', 'published'} for child in children):
            return None
    return pub
