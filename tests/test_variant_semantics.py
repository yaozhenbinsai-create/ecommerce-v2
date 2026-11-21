import copy
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app.commerce import variant_semantics as semantics, variation_families as families


class VariationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.target={'marketplace_id':'SITE','content_language':'en_US'}
        self.locale=patch.object(families.locales,'resolve',return_value='en_US')
        self.locale.start();self.addCleanup(self.locale.stop)

    def schema(self,*themes):
        return {'properties':{'variation_theme':{'items':{'properties':{'name':{'enum':list(themes)}}}}}}

    def source(self, ident, suffix, **facts):
        return {'id':ident,'name':'系列--'+suffix,'size':'45×70cm','components':[{'name':'枕芯','quantity':1}],
                'structured_facts':{key:{'value':value,'status':'source'} for key,value in
                                    {'product_name':'系列',**facts}.items()}}

    def draft(self, count=2):
        return {'product_type':'EXAMPLE','skus':[{'sku_id':i,'size':'45×70cm','color':'White',
            'price':0,'quantity':0,'images':[{'asset_id':i,'role':'main'}]} for i in range(1,count+1)]}

    def test_distinct_structures_in_same_supplier_family_remain_independent(self):
        draft=self.draft();draft.update(variation_theme='SIZE',parent_sku='EXISTING-PARENT')
        for row in draft['skus']:row['variation_attributes']={'size':[{'value':'old'}]}
        sources=[self.source(1,'单边低款',natural_height=10),self.source(2,'立体高款',natural_height=12)]
        before=copy.deepcopy(draft);original=copy.deepcopy(sources)
        plan=families.proposal(draft,sources,self.schema('SIZE','COLOR/SIZE'),self.target)
        self.assertEqual(plan['status'],'independent');self.assertEqual(plan['identity']['product_count'],2)
        families.apply(draft,plan,self.target,10)
        self.assertNotIn('variation_theme',draft);self.assertNotIn('parent_sku',draft)
        for old,new in zip(before['skus'],draft['skus']):
            for key in ('sku_id','price','quantity','images','size'):self.assertEqual(old[key],new[key])
        self.assertEqual(sources,original)

    def test_height_variants_use_actual_measurements_in_any_product_type(self):
        draft=self.draft(3)
        sources=[self.source(i,name,natural_height=height) for i,name,height in
                 [(1,'低款',9),(2,'中款',11),(3,'高款',13)]]
        for source in sources:source['structured_facts']['natural_height']['qualifiers']=['approximate']
        plan=families.proposal(draft,sources,self.schema('SIZE'),self.target)
        self.assertEqual(plan['status'],'ready');self.assertEqual(plan['theme'],'SIZE')
        self.assertEqual([r['size'] for r in plan['rows']],
            [f'45×70cm / Unloaded height approx. {n} cm' for n in (9,11,13)])
        families.apply(draft,plan,self.target,20)
        families.assert_compatible(draft,sources,self.schema('SIZE'),self.target)

    def test_color_and_height_combination_keeps_every_actual_combination(self):
        draft=self.draft(4);sources=[]
        for i,(color,height) in enumerate([('Red',9),('Red',13),('Blue',9),('Blue',13)],1):
            draft['skus'][i-1]['color']=color
            sources.append(self.source(i,color+('低款' if height==9 else '高款'),color=color,natural_height=height))
        plan=families.proposal(draft,sources,self.schema('SIZE','COLOR','SIZE/COLOR'),self.target)
        self.assertEqual(plan['theme'],'SIZE/COLOR')
        self.assertEqual(len({(r['size'],r['color']) for r in plan['rows']}),4)

    def test_function_and_model_differences_cannot_be_disguised_as_color(self):
        for key in ('function','model_number','construction_type'):
            with self.subTest(key=key):
                draft=self.draft();draft['skus'][1]['color']='Black'
                sources=[self.source(1,'同款',**{key:'A'}),self.source(2,'同款',**{key:'B'})]
                self.assertEqual(families.proposal(draft,sources,self.schema('COLOR'),self.target)['status'],'independent')

    def test_unknown_identity_on_one_sku_is_not_inherited(self):
        draft=self.draft();draft['skus'][1]['size']='50×80cm'
        plan=families.proposal(draft,[self.source(1,'同款',function='Heated'),self.source(2,'同款')],self.schema('SIZE'),self.target)
        self.assertEqual(plan['status'],'independent')

    def test_height_suffix_is_one_measurement_even_when_item_count_is_one(self):
        draft = self.draft(3)
        sources = []
        for i, (height, suffix) in enumerate(((6, '6cm'), (8, '8 CM'), (10, '10厘米')), 1):
            sources.append(self.source(i, suffix, natural_height=height, number_of_items='1'))
            draft['skus'][i-1]['attributes'] = {'number_of_items': [{'value': 1}]}
        plan = families.proposal(draft, sources, self.schema('SIZE', 'SIZE/NUMBER_OF_ITEMS'), self.target)
        self.assertEqual((plan['status'], plan['theme']), ('ready', 'SIZE'))
        self.assertEqual([v['remainder'] for v in plan['identity']['name_evidence']], ['', '', ''])
        for unsupported in ('100cm', '10mm', '10 mm', '10毫米', 'A10cm'):
            with self.subTest(unsupported=unsupported):
                sources[-1]['name'] = '系列--' + unsupported
                plan = families.proposal(draft, sources, self.schema('SIZE', 'SIZE/NUMBER_OF_ITEMS'), self.target)
                self.assertEqual(plan['status'], 'needs_action')
                self.assertEqual(plan['identity']['unresolved_names'][0]['remainder'], unsupported)

    def test_manual_size_clear_is_not_refilled_from_height(self):
        draft=self.draft()
        for row in draft['skus']:row.update(size='',size_source={'status':'cleared'})
        before=copy.deepcopy(draft)
        plan=families.proposal(draft,[self.source(1,'低款',natural_height=9),self.source(2,'高款',natural_height=13)],self.schema('SIZE'),self.target)
        self.assertEqual(plan['status'],'needs_action');self.assertEqual(draft,before)

    def test_numeric_typed_dimensions_keep_units_and_zero(self):
        draft=self.draft()
        for row,value in zip(draft['skus'],(0,12)):
            row['attributes']={'thickness':[{'value':value,'unit':'centimeters','marketplace_id':'SITE'}]}
        plan=families.proposal(draft,[self.source(1,'同款'),self.source(2,'同款')],self.schema('THICKNESS'),self.target)
        self.assertEqual(plan['theme'],'THICKNESS');families.apply(draft,plan,self.target,30)
        self.assertEqual(draft['skus'][0]['variation_attributes']['thickness'][0]['value'],0)
        self.assertEqual(draft['skus'][1]['variation_attributes']['thickness'][0]['unit'],'centimeters')

    def test_unsupported_or_deprecated_theme_falls_back_to_independent(self):
        draft=self.draft();draft['skus'][1]['color']='Black'
        schema=self.schema('COLOR');schema['properties']['variation_theme']['items']['properties']['name']['$lifecycle']={'enumDeprecated':['COLOR']}
        plan=families.proposal(draft,[self.source(1,'同款'),self.source(2,'同款')],schema,self.target)
        self.assertEqual(plan['status'],'independent');self.assertNotIn('theme',plan)

    def test_identical_undistinguished_skus_are_not_created_as_duplicate_products(self):
        plan=families.proposal(self.draft(),[self.source(1,'同款'),self.source(2,'同款')],self.schema('SIZE'),self.target)
        self.assertEqual(plan['status'],'needs_action')

    def test_range_and_approximation_remain_visible_in_chinese(self):
        sources=[self.source(1,'低款',natural_height='8–10',fill_weight=800),self.source(2,'高款',natural_height='12–14',fill_weight=1000)]
        sources[0]['structured_facts']['natural_height']['qualifiers']=['approximate']
        view=semantics.display({'name':'系列'},sources)
        self.assertEqual(view['product_count'],1)
        self.assertEqual(next(r['value'] for r in view['rows'][0]['details'] if r['field']=='natural_height'),'约8–10 cm')
        self.assertEqual(next(r['value'] for r in view['rows'][1]['details'] if r['field']=='fill_weight'),'1000 g')

    def test_legacy_or_manually_changed_theme_cannot_hide_different_products(self):
        draft=self.draft();draft['variation_theme']='SIZE'
        with self.assertRaises(HTTPException):
            families.assert_compatible(draft,[self.source(1,'Structure A'),self.source(2,'Structure B')],self.schema('SIZE'),self.target)

    def test_same_numeric_dimension_in_different_units_is_not_two_unique_options(self):
        draft=self.draft()
        for row,value,unit in zip(draft['skus'],(1,10),('centimeters','millimeters')):
            row['attributes']={'thickness':[{'value':value,'unit':unit,'marketplace_id':'SITE'}]}
        plan=families.proposal(draft,[self.source(1,'同款'),self.source(2,'同款')],self.schema('THICKNESS'),self.target)
        self.assertEqual(plan['status'],'needs_action')

    def test_numeric_pack_count_uses_real_sales_units_without_redundant_set_axis(self):
        draft=self.draft();sources=[]
        for i,count in ((1,1),(2,2)):
            row=self.source(i,'同款',number_of_items=count)
            row['components']=[{'name':'枕芯','quantity':count}];sources.append(row)
        plan=families.proposal(draft,sources,self.schema('NUMBER_OF_ITEMS'),self.target)
        self.assertEqual(plan['theme'],'NUMBER_OF_ITEMS')
        families.apply(draft,plan,self.target,40)
        self.assertEqual([r['variation_attributes']['number_of_items'][0]['value'] for r in draft['skus']],[1,2])

    def test_template_composition_drives_set_variations_with_identical_production_parts(self):
        sources = [self.source(1, '同款', components='枕芯 × 1'),
                   self.source(2, '同款', components='枕芯 × 1；枕套 × 1')]
        before = copy.deepcopy(sources)
        plan = families.proposal(self.draft(), sources, self.schema('SET_NAME'), self.target)
        self.assertEqual((plan['status'], plan['theme']), ('ready', 'SET_NAME'))
        self.assertEqual([row['set_name'] for row in plan['rows']], ['1 Pillow', '1 Pillow + 1 Pillowcase'])
        self.assertEqual(sources, before)

    def test_missing_or_unstructured_composition_is_not_filled_from_a_production_default(self):
        kit = self.source(2, '同款', components='枕芯 × 1；枕套 × 1')
        plan = families.proposal(self.draft(), [self.source(1, '同款'), kit],
                                 self.schema('SET_NAME'), self.target)
        self.assertEqual(plan['status'], 'needs_action')
        self.assertEqual([row['set_name'] for row in plan['rows']], ['', '1 Pillow + 1 Pillowcase'])
        for raw in ('枕芯；枕套', '枕芯 × 1；枕套 × 0'):
            with self.subTest(raw=raw):
                kit['structured_facts']['components']['value'] = raw
                plan = families.proposal(self.draft(), [self.source(1, '同款'), kit],
                                         self.schema('SET_NAME'), self.target)
                self.assertEqual(plan['status'], 'needs_action')
                self.assertIn('按模板', plan['reason'])

    def test_different_core_product_names_cannot_merge_after_prefix_stripping(self):
        draft=self.draft();draft['skus'][1]['color']='Black'
        sources=[self.source(1,'同款'),self.source(2,'同款')]
        sources[1]['structured_facts']['product_name']['value']='另一款商品'
        self.assertEqual(families.proposal(draft,sources,self.schema('COLOR'),self.target)['status'],'independent')


if __name__=='__main__':unittest.main()
