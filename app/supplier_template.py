"""Keep our template's examples and instructions out of archive confirmations."""

from fastapi import HTTPException

TEMPLATE_TITLE = '供应商商品资料模板 v1'
GUIDE_TITLE = '模板要求与填写逻辑'
RECORD_HEADER = '记录类型'
REAL_RECORD = '正式商品'


def validate_template_plan(snapshot, plan):
    sheets = snapshot.get('sheets', [])
    template_sheets = {
        sheet['name'] for sheet in sheets
        if any((row.get('values') or [None])[0] == TEMPLATE_TITLE
               for row in sheet.get('rows', []))
    }
    if not template_sheets:
        return
    by_name = {sheet['name']: sheet for sheet in sheets}
    for region in plan.get('regions', []):
        if not isinstance(region, dict):
            continue  # The generic parser validates malformed regions.
        sheet = by_name.get(region.get('sheet_name'), {})
        rows = sheet.get('rows', [])
        if any((row.get('values') or [None])[0] == GUIDE_TITLE for row in rows):
            raise HTTPException(422, '模板“填写说明”是说明页，请在解析计划中排除，不能作为商品导入')
        if sheet.get('name') not in template_sheets:
            continue
        start, end = region.get('start_row'), region.get('end_row')
        if type(start) is not int or type(end) is not int:
            continue
        headers = [row for row in rows if (row.get('values') or [None])[0] == RECORD_HEADER]
        if not headers:
            raise HTTPException(422, '模板缺少“记录类型”表头，请保留 A 列并核对正式商品区域')
        for row in rows:
            values = row.get('values') or []
            if not start <= row['row'] <= end or not any(value not in (None, '') for value in values):
                continue
            if str(values[0] or '').strip() != REAL_RECORD:
                raise HTTPException(
                    422, f'模板 {sheet["name"]}!R{row["row"]} 不是正式商品行。'
                    '请排除示例与说明，只选择 A 列为“正式商品”的数据区域')
