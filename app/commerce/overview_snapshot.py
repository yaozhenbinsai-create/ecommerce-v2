"""Small list snapshots; full evidence is fetched when a record is opened."""
from __future__ import annotations


from fastapi import HTTPException
from PIL import Image


def archive_readiness(product, family, image_checks):
    """Read-only list availability, checking shared originals once per snapshot.

    Starting a design still uses product_archive.readiness and verifies its exact
    selected inputs. This display optimization never changes a frozen executor.
    """
    from app import simple_design_flow as flow
    from app.supplier_fact_notes import notices
    from .product_archive import source_images
    selected=[sku for sku in family if sku.get('active',True)]
    missing=[];reports=[]
    if not selected:
        missing.append({'field':'skus','label':'商品 SKU','reason':'没有可执行设计的 SKU','sku_id':None})
    for sku in selected:
        errors=[];images=source_images(product,sku,len(family))
        if not images:
            errors.append({'field':'source_images','label':'本款原图','reason':'没有可发送的本款原图','sku_id':sku['id']})
        for url in images:
            if url not in image_checks:
                try:
                    with Image.open(flow.local_path(url,source=True)) as image:image.verify()
                    image_checks[url]=True
                except (HTTPException,OSError,ValueError):image_checks[url]=False
            if not image_checks[url]:
                errors.append({'field':'source_images','label':'本款原图','reason':'原图文件无法读取','sku_id':sku['id']})
                break
        missing.extend(errors)
        reports.append({'sku_id':sku['id'],'ready':not errors,'missing':errors,'source_notices':notices(sku)})
    return {'design_ready':not missing,'ready':not missing,'status':'ready' if not missing else 'needs_supplement',
            'missing':missing,'skus':reports,'selected_sku_ids':[sku['id'] for sku in selected]}


def revision(c):
    from .snapshot_clock import read
    return read(c)


def compact(data):
    # Remove evidence copies from the wire only. Stored records and detail GETs
    # remain complete, and editors always fetch a fresh full record before save.
    for kind in ('product', 'sku'):
        for row in data[kind]:
            for key in ('structured_facts','normalized_fields','source_fields','cost_pricing'):
                row.pop(key,None)
            for key in ('price_source','cost_source'):
                if isinstance(row.get(key),dict):
                    row[key]={name:value for name,value in row[key].items() if name in {'status','message'}}
            for sku in row.get('design_readiness',{}).get('skus',[]):
                sku['source_notices']=[{key:value for key,value in note.items() if key not in {'source','evidence'}}
                                       for note in sku.get('source_notices',[])]
            row['_summary']=True
    for row in data['listing']:
        for key in ('preview_results','preflight_results','identifier_check','platform_observed','sku_results'):
            row.pop(key,None)
        if row.get('draft'):
            draft=row['draft']
            row['draft']={key:draft[key] for key in ('brand','product_type','currency','handling_days','images',
                         'skus','variation_theme','parent_sku') if key in draft}
            row['draft']['skus']=[{key:value for key,value in sku.items() if key not in
                                  {'attributes','variation_attributes','source','parameters'}}
                                 for sku in draft.get('skus',[])]
        row['_summary']=True
    return data


def records(c,kind,*,summary=False):
    from . import store as s
    if summary and kind=='listing':
        # Transport/preflight evidence is only needed by the detail editor.
        # Preserve current draft, identity/readback and relationship inputs until progress is calculated.
        paths=','.join("'$."+key+"'" for key in ('preview_results','preflight_results','identifier_check','sku_results','publication_snapshot','packaging_projection'))
        query=f"SELECT id,kind,natural_key,revision,created_at,updated_at,json_remove(data,{paths}) AS data FROM commerce_records WHERE kind=? ORDER BY id DESC"
        return [s.decode(row) for row in c.execute(query,(kind,))]
    return s.all_rows(c,kind)
