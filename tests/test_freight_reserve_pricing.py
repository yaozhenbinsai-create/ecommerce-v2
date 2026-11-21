"""The final owner price is 3x real cost plus an explicit CNY200 reserve."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app.commerce import costing


class FreightReservePricingTests(unittest.TestCase):
    def test_quote_keeps_source_sale_unused_and_reserve_as_owner_estimate(self):
        rule={'policy':'supplier_cost_times_three_plus_freight_reserve','multiplier':'3',
              'freight_reserve':'200','freight_currency':'CNY','authorization':'user'}
        quote=costing.derived_sale({'value':'1','status':'source'},
              {'status':'source','amount':'25','currency':'CNY','unit':'只'},rule)
        self.assertEqual(quote['amount'],'275.00')
        self.assertEqual(quote['unused_source_sale']['value'],'1')
        self.assertEqual(quote['freight_reserve']['source'],'owner_authorized_estimate')
        self.assertEqual(costing.derived_sale(None,{'status':'source','amount':'190','currency':'CNY'},rule)['amount'],'770.00')
        for status,currency in [('fallback','CNY'),('source','USD')]:
            self.assertIsNone(costing.derived_sale(None,{'status':status,'amount':'25','currency':currency},rule)['amount'])

    def test_config_affects_exact_import_only(self):
        with tempfile.TemporaryDirectory() as folder:
            rules={'supplier_imports':{'4':{'policy':'supplier_cost_times_three_plus_freight_reserve',
                   'multiplier':'3','freight_reserve':'200','freight_currency':'CNY'}}}
            (Path(folder)/'supplier_sale_rules.json').write_text(json.dumps(rules))
            with patch.object(costing.s,'main',return_value=SimpleNamespace(DATA_ROOT=folder)):
                self.assertEqual(costing.sale_rule({'supplier_source':{'import_id':4}})['multiplier'],'3')
                self.assertEqual(costing.sale_rule({'supplier_source':{'import_id':5}})['multiplier'],'2')
