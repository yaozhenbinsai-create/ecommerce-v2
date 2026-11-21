"""Explicit owner facts for one supplier import, separate from visual inputs."""
import copy
import json
from pathlib import Path

from fastapi import HTTPException
from . import store as s


def for_product(product):
    import_id=(product.get('supplier_source') or {}).get('import_id')
    if import_id is None:return {}
    path=Path(s.main().DATA_ROOT)/'supplier_fact_declarations.json'
    if not path.is_file():return {}
    try:
        raw=json.loads(path.read_text(encoding='utf-8'))
        declaration=raw['supplier_imports'].get(str(import_id),{})
        if not isinstance(declaration,dict):raise ValueError()
        models=declaration.get('manufacturer_models')
        if models is None:return {}
        if not isinstance(models,dict) or models.get('status')!='not_assigned' or not models.get('authorization'):
            raise ValueError()
        return {'import_id':import_id,'manufacturer_models':copy.deepcopy(models)}
    except (OSError,ValueError,TypeError,KeyError):
        raise HTTPException(422,'供应商批次事实声明无效，请核对；不猜测厂家型号')


def model_defaults(product,schema,marketplace_id,language_tag):
    if not for_product(product):return {}
    from . import source_attributes,jobs
    properties=schema.get('properties') or {};result={}
    for name in ('model_name','model_number'):
        rule=properties.get(name)
        if not isinstance(rule,dict):continue
        encoded=source_attributes._encode(rule,'',marketplace_id,language_tag)
        if not encoded:continue
        partial={key:copy.deepcopy(schema[key]) for key in ('$schema','$defs','definitions') if key in schema}
        partial.update(type='object',properties={name:copy.deepcopy(rule)},required=[name],additionalProperties=False)
        # Some destinations disallow an empty model. Keep that missing instead
        # of inventing a manufacturer code or weakening the current rule.
        if not jobs.validate_attributes(partial,{name:encoded}):result[name]=encoded
    return result
