"""Read supplier dimensions when adapting historical design archives."""
import re
from . import costing,store as s


def normalize(raw):
    text=str(raw).strip()
    match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*[*×xX]\s*(\d+(?:\.\d+)?)(?:\s*[*×xX]\s*(\d+(?:\.\d+)?))?\s*(cm|mm|m|厘米|毫米|米|英寸|inches|inch|in)',text,re.I)
    if not match:return text
    unit={'厘米':'cm','毫米':'mm','米':'m','英寸':'in','inch':'in','inches':'in'}.get(match[4].lower(),match[4].lower())
    return ' × '.join(v for v in match.groups()[:3] if v)+' '+unit


def resolve(evidence,name):
    if evidence is None:return {'status':'missing','message':'没有原表尺寸'}
    entries=evidence['entries']
    def values(rows):return [v|{'row':e['row']} for e in rows for v in e['values'] if v['raw'].strip() not in {'-','—','暂无','待补充','未填写'}]
    selected=values([e for e in entries if e['name']==name])
    if not selected:
        # A lone sibling value does not prove this SKU has the same dimensions.
        return {'status':'missing','message':'本规格没有明确尺寸来源；不借用其他规格'}
    unique={normalize(v['raw']) for v in selected}
    if len(unique)>1:return {'status':'ambiguous','message':'原表尺寸有冲突'}
    if not unique:return {'status':'missing','message':'原表未填写尺寸'}
    return selected[0]|{'status':'source','value':next(iter(unique)),'import_id':evidence['import_id'],'sheet':evidence['sheet']}


def apply(c,product):
    project=c.execute('SELECT * FROM design_projects WHERE id=?',(product.get('project_id'),)).fetchone()
    if not project:return product
    evidence=costing.source(c,dict(project),'size')
    if evidence is None:return product
    for sku in s.all_rows(c,'sku'):
        if sku['product_id']!=product['id'] or sku.get('size') or (sku.get('size_source') or {}).get('status')=='manual':continue
        found=resolve(evidence,sku['name'])
        updated=sku|{'size_source':found}
        if found['status']=='source':updated['size']=found['value']
        if updated!=sku:s.save(c,'sku',updated)
    return product
