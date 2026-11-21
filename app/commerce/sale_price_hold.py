"""Scoped owner-requested price holds, independent of design and live switches."""
import json
from pathlib import Path

from fastapi import HTTPException
from . import store as s


def assert_released(product, *, preview=False):
    if preview:
        return
    path=Path(s.main().DATA_ROOT)/'sale_price_release_holds.json'
    if not path.exists():
        return
    try:
        holds=json.loads(path.read_text(encoding='utf-8'))
        imports=holds['supplier_imports']
        if not isinstance(imports,dict):raise ValueError()
        entry=imports.get(str((product.get('supplier_source') or {}).get('import_id')))
        if entry is None:return
        if not isinstance(entry,dict) or type(entry.get('held')) is not bool:raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):
        raise HTTPException(422,'售价核对暂缓配置无效，请核实后再提交') from None
    if entry['held']:
        raise HTTPException(422,str(entry.get('reason') or '本批最终售价尚未核对，设计和预检可继续，正式提交暂缓'))
