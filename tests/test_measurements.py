import copy
from decimal import Decimal
import unittest

from app.commerce import measurements, source_attributes


class MeasurementTests(unittest.TestCase):
    def test_explicit_measurements_support_products_units_and_three_axes(self):
        cases=[('20 x 40 cm',{'length':40,'width':20}),
               ('200mm × 40cm',{'length':40,'width':20}),
               ('0.2m*0.4m',{'length':40,'width':20}),
               ('2in x 4in',{'length':Decimal('10.16'),'width':Decimal('5.08')}),
               ('20*40*10cm',{'length':40,'width':20,'height':10})]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(measurements.dimensions(text),expected)

    def test_any_nonnumeric_source_note_is_retained_while_dimensions_are_encoded(self):
        for note in ('适合收纳使用','cover included','handle attached','哑光表面'):
            source='20×40cm（'+note+'）'
            self.assertEqual(source_attributes._dimensions(source),{'length':40,'width':20})
            self.assertIsNone(measurements.dimensions(source))

    def test_uncertain_missing_units_and_multiple_measurements_are_not_collapsed(self):
        for text in ('20x40','约20x40cm','20-25x40cm','20x40cm (30x50cm)','20x40cm ±2cm','20x40cm / height 7cm','0x40cm'):
            with self.subTest(text=text):
                self.assertIsNone(measurements.dimensions(text,annotations=True))

    def test_official_encoding_cannot_drop_a_dimension_or_change_source(self):
        field={'items':{'properties':{key:{'properties':{'value':{'type':'number'},'unit':{'enum':['centimeters']}}}
                                    for key in ('length','width','height')}}}
        field['items']['required']=['length','width','height']
        source={'structured_facts':{'size':{'value':'10x20x30cm','status':'source'}}}
        before=copy.deepcopy(source)
        result=source_attributes.from_sku(source,{'properties':{'item_dimensions':field}},'SITE','en_US')
        self.assertEqual(result['item_dimensions'][0]['height']['value'],30)
        self.assertEqual(source,before)
