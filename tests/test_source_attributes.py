import unittest

from app.commerce import source_attributes


class SourceAttributeTest(unittest.TestCase):
    def weight_schema(self, units=('pounds',)):
        rule = {'items': {'required': ['value', 'unit'], 'properties': {
            'value': {'type': 'number'}, 'unit': {'enum': list(units)}, 'marketplace_id': {}}}}
        return {'properties': {'item_weight': rule, 'item_package_weight': rule}}

    def test_explicit_supplier_headers_encode_each_actual_weight(self):
        record = {'structured_facts': {
            'weight': {'value': '950', 'status': 'source', 'source': {'column': '商品总重(g)'}},
            'package_weight': {'value': '1150', 'status': 'source', 'source': {'column': '包装重量（克）'}}}}
        encoded = source_attributes.from_sku(record, self.weight_schema(), 'US', 'en_US')
        self.assertAlmostEqual(encoded['item_weight'][0]['value'], 950 / 453.59237)
        self.assertAlmostEqual(encoded['item_package_weight'][0]['value'], 1150 / 453.59237)
        self.assertEqual(encoded['item_weight'][0]['unit'], 'pounds')
        self.assertEqual(record['structured_facts']['weight']['value'], '950')

    def test_manual_clear_conflict_and_ambiguous_units_never_borrow_source_unit(self):
        for status in ('manual', 'cleared', 'conflict', 'not_applicable', 'inherited'):
            record = {'source_fields': {'商品总重(g)': '950'}, 'structured_facts': {
                'weight': {'value': '950', 'status': status, 'source': {'column': '商品总重(g)'}}}}
            self.assertNotIn('item_weight', source_attributes.from_sku(record, self.weight_schema(), 'US', 'en_US'))
        for column in ('商品总重', '商品总重(g/kg)', '商品总重(mL)'):
            record = {'structured_facts': {'weight': {'value': '950', 'status': 'source', 'source': {'column': column}}}}
            self.assertNotIn('item_weight', source_attributes.from_sku(record, self.weight_schema(), 'US', 'en_US'))
        record = {'structured_facts': {
            'weight': {'value': '950', 'status': 'source', 'source': {'column': '商品总重(g)'}},
            'item_weight': {'value': '950', 'status': 'source', 'source': {'column': '商品总重(kg)'}}}}
        self.assertNotIn('item_weight', source_attributes.from_sku(record, self.weight_schema(), 'US', 'en_US'))

    def test_real_units_convert_only_to_official_supported_mass_units(self):
        for raw, expected in (('1 kg', 1000 / 453.59237), ('16 oz', 1), ('2 pounds', 2)):
            record = {'structured_facts': {'item_weight': {'value': raw, 'status': 'manual'}}}
            encoded = source_attributes.from_sku(record, self.weight_schema(), 'US', 'en_US')
            self.assertAlmostEqual(encoded['item_weight'][0]['value'], expected)
        self.assertEqual(source_attributes.from_sku({'source_fields': {'商品总重(g)': '950'}},
            self.weight_schema(('liters',)), 'US', 'en_US'), {})

    def test_source_only_headers_work_without_using_filling_weight(self):
        record = {'source_fields': {'商品总重(g)': '950', '包装重量(g)': '960', '填充净重(g)': '700'}}
        encoded = source_attributes.from_sku(record, self.weight_schema(('grams',)), 'US', 'en_US')
        self.assertEqual(encoded['item_weight'][0]['value'], 950)
        self.assertEqual(encoded['item_package_weight'][0]['value'], 960)

    def test_zero_reference_price_requires_explicit_official_permission(self):
        rule={'items':{'required':['value','currency'],'properties':{
            'value':{'type':'number','minimum':0,'description':'If you are unable to provide a value, enter 0.'},
            'currency':{'enum':['USD']},'marketplace_id':{}}}}
        schema={'properties':{'list_price':rule}}
        result=source_attributes.from_sku({'price':'50','cost':'25'},schema,'US','en_US')
        self.assertEqual(result['list_price'],[{'value':0,'currency':'USD','marketplace_id':'US'}])
        for status in ('manual','cleared','conflict','not_applicable'):
            record={'structured_facts':{'list_price':{'value':'','status':status}}}
            self.assertNotIn('list_price',source_attributes.from_sku(record,schema,'US','en_US'))
        rule['items']['properties']['value']['description']='Provide a reference price'
        self.assertNotIn('list_price',source_attributes.from_sku({},schema,'US','en_US'))

    def test_unit_count_uses_real_piece_count_and_supported_nested_unit(self):
        rule={'items':{'required':['type','value'],'properties':{'value':{'type':'number'},
              'type':{'required':['value','language_tag'],'properties':{
                  'value':{'enum':['Count']},'language_tag':{}}},'marketplace_id':{}}}}
        schema={'properties':{'unit_count':rule}}
        record={'structured_facts':{'package_quantity':{'value':'2','status':'source'}}}
        result=source_attributes.from_sku(record,schema,'US','en_US')
        self.assertEqual(result['unit_count'],[{'value':2,'type':{'value':'Count','language_tag':'en_US'},'marketplace_id':'US'}])
        for status in ('missing','cleared','conflict','not_applicable'):
            blocked={'structured_facts':{'package_quantity':{'value':'','status':status}}}
            self.assertNotIn('unit_count',source_attributes.from_sku(blocked,schema,'US','en_US'))
        rule['items']['properties']['type']['properties']['value']['enum']=['Ounces']
        self.assertNotIn('unit_count',source_attributes.from_sku(record,schema,'US','en_US'))

    def test_structured_alias_edits_and_clears_never_restore_old_supplier_values(self):
        record = {'material': '棉', 'weight': '1 kg', 'size': '10x20cm', 'package_quantity': 1,
                  'normalized_fields': {'material': '棉', 'weight': '1 kg'},
                  'source_fields': {'面料': '棉', '总重': '1 kg', '销售件数': 1},
                  'structured_facts': {'fabric': {'value': '亚麻', 'status': 'manual'},
                                       'item_weight': {'value': '2 kilograms', 'status': 'manual'}}}
        self.assertEqual(source_attributes.fact(record, 'fabric', '面料'), '亚麻')
        self.assertEqual(source_attributes.fact(record, 'item_weight', '总重'), '2 kilograms')
        for field, name, pattern in [('fabric', 'fabric', '面料'), ('item_weight', 'weight', '总重'),
                                     ('dimensions', 'size', '尺寸'), ('number_of_items', 'package_quantity', '销售件数')]:
            for status in ('cleared', 'not_applicable', 'conflict'):
                with self.subTest(field=field, status=status):
                    value = dict(record, structured_facts={field: {'value': '', 'status': status}})
                    self.assertEqual(source_attributes.fact(value, name, pattern), '')
        rules = {'properties': {
            'number_of_items': {'items': {'properties': {'value': {'type': 'integer'}}}},
            'item_length_width': {'items': {'properties': {'length': {'properties': {'value': {}, 'unit': {}}},
                                                         'width': {'properties': {'value': {}, 'unit': {}}}}}}}}
        cleared = dict(record, structured_facts={
            'dimensions': {'value': '', 'status': 'cleared'},
            'number_of_items': {'value': '', 'status': 'cleared'}})
        self.assertEqual(source_attributes.from_sku(cleared, rules, 'US', 'en_US'), {})

    def test_multiple_unresolved_source_alias_values_are_not_encoded(self):
        record = {'source_fields': {'面料': '棉'}, 'structured_facts': {
            '面料': {'value': '棉', 'status': 'source'},
            'fabric': {'value': '亚麻', 'status': 'source'}}}
        self.assertEqual(source_attributes.fact(record, 'fabric', '面料'), '')

    def test_fabric_fill_and_item_weight_keep_distinct_source_meanings(self):
        sku={'source_fields':{'面料':'100% 棉','填充物':'上层柞蚕丝、下层羽丝','总重':'1700克',
                              '净重（填充重量）':'1500克'}}
        item={'properties':{'value':{'type':'string'},'language_tag':{'type':'string'},
                            'marketplace_id':{'type':'string'}}}
        weight={'properties':{'value':{'type':'number'},
                              'unit':{'enum':['grams','pounds']},'marketplace_id':{'type':'string'}},
                'required':['value','unit']}
        schema={'properties':{'material':{'items':item},'fill_material':{'items':item},
                              'item_weight':{'items':weight}}}
        result=source_attributes.from_sku(sku,schema,'US','en_US')
        self.assertEqual(result['material'][0]['value'],'100% 棉')
        self.assertEqual(result['fill_material'][0]['value'],'上层柞蚕丝、下层羽丝')
        self.assertEqual(result['item_weight'][0]['value'],1700)
        self.assertEqual(result['item_weight'][0]['unit'],'grams')

    def test_missing_unit_and_unmatched_enum_are_not_guessed(self):
        sku={'source_fields':{'总重':'1700','面料':'100% 棉'}}
        schema={'properties':{'item_weight':{'items':{'properties':{'value':{},'unit':{}}}},
                              'fabric_type':{'items':{'properties':{'value':{'enum':['Cotton']}}}}}}
        self.assertEqual(source_attributes.from_sku(sku,schema,'US','en_US'),{})

    def test_related_material_fallbacks_do_not_borrow_fill_composition(self):
        item={'items':{'properties':{'value':{'type':'string'}}}}
        schema={'properties':{name:item for name in ('material','fabric_type','fill_material')}}
        for source, expected in [('material','material'),('fabric','fabric_type'),('fill_material','fill_material')]:
            for status in ('source','manual','archive'):
                with self.subTest(source=source,status=status):
                    record={'structured_facts':{source:{'value':'Source composition','status':status}}}
                    self.assertEqual(set(source_attributes.from_sku(record,schema,'SITE','en_US')),
                                     {expected} if source=='fill_material' else {'material','fabric_type'})


if __name__=='__main__':unittest.main()
