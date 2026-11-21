import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from app.codex_bridge import CodexAppServer
from app import codex_image_models


class OfficialClientEnvironmentTests(unittest.TestCase):
    def test_official_model_choices_require_a_discovered_image_model(self):
        self.assertEqual(codex_image_models.catalog([]), [])
        self.assertEqual(codex_image_models.catalog([{'id': 'text-only'}]), [])
        items = codex_image_models.catalog([{'id': 'signed-in-vision', 'outputModalities': ['image']}])
        self.assertEqual([item['id'] for item in items], ['signed-in-vision'])
        self.assertFalse(items[0]['is_default'])

    def test_legacy_model_key_is_not_a_supported_environment_source(self):
        from app import main
        expected = secrets.token_urlsafe(24)
        with patch.dict(os.environ, {'OPENAI_API_KEY': secrets.token_urlsafe(24), 'ECOMMERCE2_MODEL_KEY_LOCAL': expected}):
            self.assertEqual(main.model_environment_key('OPENAI_API_KEY'), '')
            self.assertEqual(main.model_environment_key('ECOMMERCE2_MODEL_KEY_LOCAL'), expected)

    def test_inherited_profile_keys_and_legacy_proxy_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            data.mkdir()
            (data / 'codex_connection.json').write_text(json.dumps({'proxy_url': 'https://unused.example.invalid'}))
            other = root / 'different-client'
            other.mkdir()
            (other / 'config.toml').write_text('model_provider = "unrelated"')
            with patch.dict(os.environ, {'CODEX_HOME': str(other), 'OPENAI_API_KEY': secrets.token_hex(24),
                                         'HTTPS_PROXY': 'https://unused.example.invalid'}):
                client = CodexAppServer(root)
                env = client._process_environment()
                self.assertEqual(Path(env['CODEX_HOME']), (data / 'official-client').resolve())
                self.assertNotIn('OPENAI_API_KEY', env)
                self.assertNotIn('HTTPS_PROXY', env)
                self.assertEqual(list(Path(env['CODEX_HOME']).iterdir()), [])
