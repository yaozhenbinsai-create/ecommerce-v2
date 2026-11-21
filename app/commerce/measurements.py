"""Parse explicit physical dimensions without treating source annotations as facts."""
import re
from decimal import Decimal


LENGTH_TO_CM = {'cm': Decimal(1), '厘米': Decimal(1), 'mm': Decimal('.1'), '毫米': Decimal('.1'),
                'm': Decimal(100), '米': Decimal(100), 'in': Decimal('2.54'),
                'inch': Decimal('2.54'), 'inches': Decimal('2.54'), '英寸': Decimal('2.54')}
UNIT = r'(?:centimeters|millimeters|meters|inches|inch|cm|mm|in|厘米|毫米|英寸|m|米)'
UNIT_ALIASES = {'centimeters': 'cm', 'millimeters': 'mm', 'meters': 'm'}
SIZE = re.compile(r'\s*(\d+(?:\.\d+)?)\s*(' + UNIT + r')?\s*[×xX*]\s*'
                  r'(\d+(?:\.\d+)?)\s*(' + UNIT + r')?'
                  r'(?:\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(' + UNIT + r')?)?\s*', re.I)


def dimensions(raw, *, annotations=False):
    text = str(raw).strip()
    match = SIZE.match(text)
    if not match:
        return None
    tail = text[match.end():].strip()
    # A separate nonnumeric note may qualify usage/composition. Retain it in
    # the source/display value; only the explicit dimensions are encoded here.
    if tail and (not annotations or not re.fullmatch(r'[（(][^()（）\d]*[）)]', tail)):
        return None
    final_unit = match[6] if match[5] else match[4]
    if not final_unit:
        return None
    units = [match[2] or final_unit, match[4] or final_unit]
    values = [match[1], match[3]]
    if match[5]:
        values.append(match[5]); units.append(match[6])
    result = [Decimal(value) * LENGTH_TO_CM[UNIT_ALIASES.get(unit.lower(), unit.lower())]
              for value, unit in zip(values, units)]
    if not all(value > 0 for value in result):
        return None
    return {'length': max(result[:2]), 'width': min(result[:2]),
            **({'height': result[2]} if len(result) == 3 else {})}
