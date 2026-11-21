"""Price imported goods from source costs, preserving row-level evidence."""
import copy
import json
import re
from decimal import Decimal,InvalidOperation
from pathlib import Path
from fastapi import HTTPException
from . import store as s


def amount(raw):
    text=str(raw).strip()
    match=re.fullmatch(r'(?:(CNY|RMB|USD|EUR|GBP|JPY|CAD|AUD|人民币|美元|欧元|英镑|[¥￥$€£])\s*)?(\d+(?:\.\d{1,2})?)\s*(元|人民币|美元|欧元|英镑|CNY|RMB|USD|EUR|GBP|JPY|CAD|AUD)?\s*(?:[/／]\s*(只|件|套|个|对|包))?',text,re.I)
    if not match:return None
    aliases={'人民币':'CNY','美元':'USD','欧元':'EUR','英镑':'GBP','¥':'CNY','￥':'CNY','$':'USD','€':'EUR','£':'GBP','元':'CNY','RMB':'CNY'}
    currencies={aliases.get(mark,mark.upper()) for mark in [match[1],match[3]] if mark}
    if len(currencies)>1:return None
    currency=next(iter(currencies),'')
    return {'amount':s.money(match[2]),'currency':currency,'unit':match[4] or ''}


def source(c,project,field='cost_price'):
    match=re.search(r'导入#(\d+).*?·\s*([^·]+)!R(\d+)\s*$',project.get('source_file') or '')
    if not match:return None
    row=c.execute('SELECT workbook_snapshot,field_mapping FROM supplier_imports WHERE id=?',(int(match[1]),)).fetchone()
    if not row:return None
    snapshot=json.loads(row[0] or '{}');mappings=json.loads(row[1] or '[]');sheet=match[2].strip();number=int(match[3])
    from app.supplier_variants import region_for
    region=region_for(snapshot,mappings,sheet,number)
    if not region:return None
    rows=region['data_rows'];mappings=region['mappings']
    if not rows:return None
    headers=region['headers']
    columns=[m['source_column'] for m in mappings if m.get('standard_field')==field and m.get('source_column') in headers]
    aliases=({'成本价','成本','采购价','进货价','供货价','门市供货价','cost_price'} if field=='cost_price' else
             {'售价','销售价','零售价','sale_price'} if field=='sale_price' else {'尺寸','商品尺寸','产品尺寸','规格尺寸','size'})
    if field=='size':
        exact=[h for h in headers if h in aliases]
        columns=exact or [h for h in columns if not re.search(r'高度|重量|包装|包裹|填充|height|weight|package',h,re.I)]
    if not columns:columns=[h for h in headers if h in aliases]
    family=next((headers[h] for h in ['品名','商品名称','产品名称'] if h in headers),None)
    if family is None:family=next((headers[m['source_column']] for m in mappings if m.get('standard_field')=='product_name' and m.get('source_column') in headers),None)
    variant=next((headers[h] for h in ['SKU名','SKU名称','规格名称'] if h in headers),family)
    def cell(n,index):
        value=rows.get(n,[])[index] if index is not None and index<len(rows.get(n,[])) else None
        return '' if value is None else str(value).strip()
    start=number
    if family is not None:
        while start>min(rows)+1 and not cell(start,family):start-=1
    end=number
    if family is not None:
        while end<max(rows) and not cell(end+1,family):end+=1
    entries=[]
    for n in range(start,end+1):
        values=[{'raw':cell(n,headers[col]),'column':col} for col in columns if cell(n,headers[col])]
        entries.append({'row':n,'name':cell(n,variant),'values':values})
    return {'import_id':int(match[1]),'sheet':sheet,'project_row':number,'entries':entries}


def resolve(evidence,name,business,policy,project_only=False,field='cost_price'):
    entries=evidence['entries']
    matching=[e for e in entries if e['row']==evidence['project_row']] if project_only else [e for e in entries if e['name']==name]
    def values(entry):
        return [v|{'row':entry['row']} for v in entry['values']]
    selected=[v for entry in matching for v in values(entry)]
    meta={'import_id':evidence['import_id'],'sheet':evidence['sheet'],'multiplier':'1'}
    if not selected:
        mode=field
        value=business.get(field)
        if value is None:return meta|{'status':'missing','message':'原表无成本，所属业务也未设置保底价格'}
        currency=business.get('cost_currency' if mode=='cost_price' else 'sale_currency','')
        return meta|{'status':'fallback','amount':s.money(value),'currency':currency,
                     'fallback_field':mode,'message':'原表缺失，暂用业务'+('采购价' if mode=='cost_price' else '销售价')}
    parsed=[amount(v['raw']) for v in selected]
    if any(v is None for v in parsed) or len({(v['amount'],v['currency'],v['unit']) for v in parsed if v})!=1:
        return meta|{'status':'invalid','message':'原表成本格式不明确或有冲突，请核对','evidence':selected}
    value=parsed[0]
    return meta|selected[0]|{'status':'source','amount':value['amount'],'currency':value['currency'],
                            'unit':value['unit'],'message':'原表明确'+('采购价' if field=='cost_price' else '销售价')}


KNOWN_CURRENCIES = {'CNY','USD','EUR','GBP','JPY','CAD','AUD','NZD','CHF','HKD','SGD','SEK','NOK','DKK','INR','MXN','BRL','AED','SAR','PLN','TRY','KRW','ZAR'}


def resolve_fact(fact,business,field,currency_fact=None,unit_fact=None):
    """A bad source is never replaced with a preset; purchase and sales are independent."""
    currency_key='cost_currency' if field=='cost_price' else 'sale_currency'
    if any((value or {}).get('status')=='conflict' for value in [currency_fact,unit_fact]):
        return {'status':'invalid','message':'原表币种或计价单位存在冲突','evidence':copy.deepcopy(fact)}
    declared=(currency_fact or {}).get('value','')
    aliases={'人民币':'CNY','RMB':'CNY','美元':'USD','欧元':'EUR','英镑':'GBP'}
    declared=aliases.get(str(declared).strip(),str(declared).strip().upper())
    declared_unit=str((unit_fact or {}).get('value','')).strip()
    if declared and declared not in KNOWN_CURRENCIES or declared_unit and declared_unit not in {'只','件','套','个','对','包'}:
        return {'status':'invalid','message':'原表币种或计价单位无效','evidence':copy.deepcopy(fact)}
    if not fact or fact.get('value') in (None,'') and fact.get('status')!='conflict':
        value=business.get(field)
        if value is None:return {'status':'missing','message':'原表与业务预设均缺少'+('采购价' if field=='cost_price' else '销售价')}
        currency=business.get(currency_key,'')
        if currency not in KNOWN_CURRENCIES:return {'status':'missing_currency','message':'业务预设币种缺失或无效'}
        return {'status':'fallback','amount':s.money(value),'currency':currency,'unit':'',
                'business_id':business.get('id'),'fallback_field':field,'message':'原表缺失，暂用业务'+('采购价' if field=='cost_price' else '销售价')}
    if fact.get('status')=='conflict':return {'status':'invalid','message':'原表价格冲突','evidence':copy.deepcopy(fact)}
    parsed=amount(fact['value'])
    if not parsed:return {'status':'invalid','message':'原表价格格式、币种或计价单位不明确','evidence':copy.deepcopy(fact)}
    if declared and (declared not in KNOWN_CURRENCIES or parsed['currency'] and parsed['currency']!=declared):
        return {'status':'invalid','message':'原表币种无效或与金额冲突','evidence':copy.deepcopy(fact)}
    currency=parsed['currency'] or declared
    if not currency:return {'status':'missing_currency','amount':parsed['amount'],'message':'原表金额没有明确币种','evidence':copy.deepcopy(fact)}
    if declared_unit and (declared_unit not in {'只','件','套','个','对','包'} or parsed['unit'] and parsed['unit']!=declared_unit):
        return {'status':'invalid','message':'原表计价单位无效或与金额冲突','evidence':copy.deepcopy(fact)}
    unit=parsed['unit'] or declared_unit
    if not unit:return {'status':'missing_unit','amount':parsed['amount'],'currency':currency,
                        'message':'原表价格没有明确计价单位','evidence':copy.deepcopy(fact)}
    return {'status':'source','amount':parsed['amount'],'currency':currency,'unit':unit,
            'source':copy.deepcopy(fact.get('source',{})),'message':'原表明确'+('采购价' if field=='cost_price' else '销售价')}


def sale_rule(product, c=None):
    """Owner-configured import scope; never multiply a synthetic fallback cost."""
    path=Path(s.main().DATA_ROOT)/'supplier_sale_rules.json'
    default={'policy':'supplier_cost_times_two','multiplier':'2','freight_reserve':'0','freight_currency':''}
    if c is not None:
        saved=s.by_key(c,'supplier_pricing_rule',str((product.get('supplier_source') or {}).get('import_id')))
        if saved:
            return copy.deepcopy(saved['rule'])
    if not path.exists():return default
    try:
        config=json.loads(path.read_text(encoding='utf-8'))
        rule=config['supplier_imports'].get(str((product.get('supplier_source') or {}).get('import_id')))
        if rule is None:return default
        if not isinstance(rule,dict):raise ValueError()
        multiplier=Decimal(str(rule['multiplier']));freight=Decimal(str(rule['freight_reserve']))
        if not multiplier.is_finite() or multiplier<=0 or not freight.is_finite() or freight<0:raise ValueError()
        if freight and rule.get('freight_currency') not in KNOWN_CURRENCIES:raise ValueError()
        return copy.deepcopy(rule)
    except (OSError,ValueError,KeyError,TypeError,InvalidOperation):
        raise HTTPException(422,'供货倍率及物流预留规则无效，请核对后再重算') from None


def derived_sale(fact, cost, rule=None):
    """Apply an evidenced supplier cost and an explicitly configured reserve."""
    rule=rule or {'policy':'supplier_cost_times_two','multiplier':'2','freight_reserve':'0','freight_currency':''}
    multiplier=Decimal(str(rule['multiplier']));freight=Decimal(str(rule.get('freight_reserve','0')))
    evidence={'policy':rule['policy'],'multiplier':str(multiplier),
              'base_cost':copy.deepcopy(cost)}
    if freight:
        evidence['freight_reserve']={'amount':s.money(freight),'currency':rule['freight_currency'],
            'source':'owner_authorized_estimate','basis':rule.get('authorization',''),'scope':'per_sales_unit'}
    if fact and fact.get('value') not in (None,''):
        evidence['unused_source_sale']=copy.deepcopy(fact)
    currency=cost.get('currency','')
    if cost.get('status') not in {'source','manual'} or cost.get('amount') is None or currency not in KNOWN_CURRENCIES:
        return evidence|{'status':'missing_supplier_cost','amount':None,'currency':'',
                         'message':'缺少明确供货价，暂不能按供货倍率定价；'+cost.get('message','请核对成本和币种')}
    if freight and currency!=rule['freight_currency']:
        return evidence|{'status':'missing_supplier_cost','amount':None,'currency':'',
                         'message':'供货币种与物流预留币种不同，不能猜测汇率混加'}
    message=f'供货价 × {multiplier}'+(f' + {s.money(freight)} {currency} 物流预留（用户暂定）' if freight else '')
    return evidence|{'status':'derived','amount':s.money(Decimal(str(cost['amount']))*multiplier+freight),
                     'currency':currency,'unit':cost.get('unit',''),'message':message}


def apply(c,product):
    project=c.execute('SELECT * FROM design_projects WHERE id=?',(product.get('project_id'),)).fetchone()
    if not project and not product.get('supplier_source'):return product
    if not product.get('supplier_source'):
        records=[product]+s.product_skus(c,product['id'])
        sourced=any(any(field in (record.get('structured_facts') or {}) for field in ('cost_price','sale_price'))
                    for record in records)
        if not sourced and not source(c,dict(project)):
            # Ordinary merchant business prices are not an imported supplier cost.
            return product
    row=c.execute('SELECT * FROM businesses WHERE id=?',(product.get('business_id'),)).fetchone()
    business=dict(row) if row else {}
    old=copy.deepcopy(product);rule=sale_rule(product,c)
    def update(record,legacy):
        facts=record.get('structured_facts') or {}
        results={}
        for field,key,currency_key,meta_key in [('cost_price','cost','cost_currency','cost_source'),('sale_price','price','currency','price_source')]:
            previous=record.get(meta_key) or {}
            manual=previous.get('status')=='manual' or (not previous and (record.get('cost_pricing') or {}).get('status')=='manual')
            if previous.get('status') in {'source','fallback','derived','missing_supplier_cost'} and 'amount' in previous:
                manual=manual or record.get(key)!=previous['amount'] or record.get(currency_key,'')!=previous.get('currency','')
            if not previous and not record.get('cost_pricing'):
                if legacy and key in legacy:
                    manual=manual or any(record.get(k)!=legacy.get(k) for k in [key,currency_key])
                elif record.get(key) is not None:
                    # Missing legacy provenance cannot authorize replacing a merchant's existing value.
                    manual=True
            if manual:
                results[meta_key]=(copy.deepcopy(previous) if previous.get('status')=='manual' else
                                   {'status':'manual','amount':record.get(key),'currency':record.get(currency_key),
                                    'previous_source':copy.deepcopy(previous)})
                results[meta_key].setdefault('message','保留商家维护的采购价' if key=='cost' else '保留商家维护的销售价')
                results[meta_key].setdefault('amount',record.get(key))
                results[meta_key].setdefault('currency',record.get(currency_key,''))
                record[meta_key]=results[meta_key]
                continue
            fact=facts.get(field)
            source_currency_key='cost_currency' if field=='cost_price' else 'sale_currency'
            related_fields={field,currency_key,source_currency_key,'price_currency','price_unit'}
            issues=[issue for issue in list(record.get('source_parse_issues') or [])+list(record.get('source_conflicts') or [])
                    if issue.get('field') in related_fields and
                    (facts.get(issue.get('field')) or {}).get('status') not in {'manual','cleared','not_applicable'}]
            if issues:fact={'value':'','status':'conflict','raw':issues}
            if fact is None and project and not product.get('supplier_source'):
                evidence=source(c,dict(project),field)
                if evidence:
                    parsed=resolve(evidence,record['name'],business,{},record.get('id')==product.get('id'),field)
                    if parsed['status']=='source':
                        fact={'value':parsed.get('raw'),'source':{k:parsed[k] for k in ['import_id','sheet','row','column'] if k in parsed},'status':'source'}
                    elif parsed['status']=='invalid':fact={'value':'','status':'conflict','raw':parsed.get('evidence')}
            found=(derived_sale(fact,results['cost_source'],rule) if field=='sale_price' else
                   resolve_fact(fact,business,field,facts.get(source_currency_key) or facts.get(currency_key) or facts.get('price_currency'),facts.get('price_unit')))
            record[meta_key]=found;results[meta_key]=found
            if found['status'] in {'source','fallback','derived'}:
                record[key]=found['amount'];record[currency_key]=found['currency']
            else:
                # Retain an existing value as history and display the unresolved source.
                if record.get(key) is not None:found['previous_value']=record[key]
                record[key]=None;record[currency_key]=''
        record['cost_pricing']={'status':results['price_source'].get('status','unknown'),'message':results['price_source'].get('message','价格来源待核对'),
                                'cost':record.get('cost'),'cost_currency':record.get('cost_currency'),
                                'price':record.get('price'),'currency':record.get('currency'),**results['price_source']}
        return record
    product=update(product,product.get('business_defaults'))
    for sku in s.product_skus(c,product['id']):
        before=copy.deepcopy(sku)
        sku=update(sku,old)
        if sku!=before:
            s.put(c,'archive_revision',f"sku:{before['id']}:{before['revision']}",
                  {'kind':'sku','record_id':before['id'],'snapshot':before,
                   'reason':'按当前导入定价规则重算；保留原报价及来源'})
            s.save(c,'sku',sku)
    if product!=old:
        s.put(c,'archive_revision',f"product:{old['id']}:{old['revision']}",
              {'kind':'product','record_id':old['id'],'snapshot':old,
               'reason':'按当前导入定价规则重算；保留原报价及来源'})
        return s.save(c,'product',product)
    return product
