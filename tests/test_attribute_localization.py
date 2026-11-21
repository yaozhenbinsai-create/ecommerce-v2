"""Generic field translation, exact receipt reuse and no product-sentence rules."""
import copy
import unittest
from app.commerce import attribute_localization as a


class SourceMaterialProjectionTest(unittest.TestCase):
    def test_arbitrary_fields_languages_parts_and_units_use_exact_field_requests(self):
        for language in ('en_US','fr_FR','de_DE'):
            original={'outer_material':[{'value':'外层65%材料甲，内衬35%材料乙','language_tag':language}],
                      'care_instructions':[{'value':'只能手洗','language_tag':language}],
                      'manufacturer':[{'value':'真实厂家'}], 'model_number':[{'value':'型号-A'}],
                      'capacity':[{'value':2.5,'unit':'liters'}], 'quantity':0}
            before=copy.deepcopy(original);requested=a.translation_request(original,language)
            self.assertEqual({item['field'] for item,_ in requested},{'outer_material','care_instructions'})
            translated=[{**item,'value':'Translation '+str(i)} for i,(item,_) in enumerate(requested)]
            output,proof=a.project(original,language,translations=translated)
            self.assertEqual(output['outer_material'][0]['value'],'Translation 0')
            self.assertEqual(output['capacity'],original['capacity'])
            self.assertEqual(output['manufacturer'],original['manufacturer'])
            self.assertEqual(original,before);self.assertEqual(proof['unmapped'],[])
            stale=[{**item,'source_value':'其他材料'} for item in translated]
            output,proof=a.project(original,language,translations=stale)
            self.assertEqual(output,original);self.assertEqual(len(proof['unmapped']),2)

    def test_different_wording_is_not_mapped_to_a_known_product_sentence(self):
        original={'fabric_type':[{'value':'高克重针织棉（100%聚酯纤维）','language_tag':'en_US'}],
                  'fill_material':[{'value':'其他供应商材料','language_tag':'en_US'}]}
        output,proof=a.project(original,'en_US')
        self.assertEqual(output,original);self.assertEqual(len(proof['unmapped']),2)
        self.assertFalse(hasattr(a,'_FABRICS'));self.assertFalse(hasattr(a,'_FILLS'))

    def test_recorded_translation_reuses_only_the_same_field_source_and_language(self):
        original={'fabric_type':[{'value':'材料甲','language_tag':'en_US'}]}
        prior={'identity':{'protocol':a.PROTOCOL,'language':'en_US'},'changes':[
            {'field':'fabric_type/0/value','source_value':'材料甲','destination_value':'Material A'}]}
        output,proof=a.project(original,'en_US',prior_proof=prior)
        self.assertEqual(output['fabric_type'][0]['value'],'Material A');self.assertEqual(proof['unmapped'],[])
        for attributes,language in (({'fabric_type':[{'value':'材料乙','language_tag':'en_US'}]},'en_US'),
                                    ({'fill_material':[{'value':'材料甲','language_tag':'en_US'}]},'en_US'),
                                    (original,'fr_FR')):
            output,proof=a.project(attributes,language,prior_proof=prior)
            self.assertEqual(output,attributes);self.assertTrue(proof['unmapped'])

    def test_conflicting_old_translations_remain_unresolved(self):
        original={'material':[{'value':'材料甲','language_tag':'en_US'}]}
        prior={'identity':{'language':'en_US'},'changes':[
            {'field':'material/0/value','source_value':'材料甲','destination_value':value} for value in ('A','B')]}
        output,proof=a.project(original,'en_US',prior_proof=prior)
        self.assertEqual(output,original);self.assertTrue(proof['unmapped'])

    def test_code_fingerprint_does_not_change_product_fact_identity(self):
        self.assertEqual(a.identity('en_US'),{'protocol':a.PROTOCOL,'language':'en_US','enabled':True})
